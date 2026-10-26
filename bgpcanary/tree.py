"""Server-side connectivity-tree images (PNG) for alert emails.

Same model as the dashboard tree (static/replay.js): your AS at the root, your upstreams, each upstream's
own transits/peers (always shown, so "not carrying you" is visible where RIS sees a neighbour carrying you
nowhere; neighbours that reach you another way are folded into one line; and for a month after one stops
being seen next to it at all, "no longer peered"), then the next hop out. Each image
shows one moment; colours compare it with a reference moment (normally the healthiest point before the
event).
"""
import asyncio
import ipaddress
import io
import logging
import math
import os
import time

from PIL import Image, ImageDraw, ImageFont

from . import db, ripe

log = logging.getLogger("bgpcanary.tree")

C = {"bg": (255, 255, 255), "text": (27, 31, 36), "muted": (95, 107, 122), "border": (210, 215, 222),
     "accent": (36, 99, 235), "crit": (200, 38, 43), "ok": (26, 138, 74), "warn": (199, 119, 0),
     "band": (240, 242, 245)}
FONT_DIRS = ["/usr/share/fonts/dejavu-sans-fonts", "/usr/share/fonts/dejavu", "/usr/share/fonts/truetype/dejavu",
             "/usr/share/fonts/TTF"]
_fonts = {}
_NBR = {}   # upstream -> (ts, [(asn, power)] worth showing, {every neighbour RIPEstat listed})
# Each upstream's neighbours are remembered, so one it stops being seen next to (a transit it lost, a session gone)
# stays on the tree as "no longer peered" for GONE_DAYS instead of vanishing. Recorded through HISTORY: the service's
# writer connection, which it sets (None in a process without one: nothing is recorded then).
NBR_KV = "upstream_neighbours"   # upstream -> {neighbour: {"first", "last", "peak", "peak_at"}} (RIPEstat data times)
NBR_DONE_KV = "upstream_neighbours_backfilled"   # upstreams whose past month has been looked up (backfill_neighbors)
GONE_DAYS = 30
GONE_MIN_PEAK = 20   # ...unless it was only seen on one day with fewer RIS adjacencies than this
HISTORY = None
# A big upstream has thousands of neighbours (Hurricane Electric: 8000+), which made trees too tall to draw.
# Keep MAX_NEIGHBORS of them, major transit networks first (RIPEstat no longer reports a size per neighbour);
# the rest still appear in trees when they carry your routes, just not as always-shown branches.
MAX_NEIGHBORS = 20
MAJOR_TRANSIT = [3356, 1299, 174, 2914, 6762, 6453, 3257, 6461, 3491, 5511, 701, 7018, 6830, 12956, 1239,
                 6939, 9002, 4637, 7922, 3320, 2828, 209, 7473, 4826, 6057, 37100, 7713, 4766, 2516, 9498]


def top_neighbors(lst, key=lambda n: n):
    """The neighbours worth always showing: major transit networks first, then in the order RIPEstat gave."""
    rank, seen, out = {a: i for i, a in enumerate(MAJOR_TRANSIT)}, set(), []
    for n in sorted(lst, key=lambda n: rank.get(key(n), len(rank))):
        if key(n) not in seen:   # RIPEstat can list a neighbour as both "left" and "uncertain"
            seen.add(key(n))
            out.append(n)
    return out[:MAX_NEIGHBORS]


def font(size, bold=False):
    k = (size, bold)
    if k not in _fonts:
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        for d in FONT_DIRS:
            p = os.path.join(d, name)
            if os.path.exists(p):
                _fonts[k] = ImageFont.truetype(p, size)
                break
        else:
            try:
                _fonts[k] = ImageFont.load_default(size=size)
            except TypeError:
                _fonts[k] = ImageFont.load_default()
    return _fonts[k]


# ---------------------------------------------------------------------- data
def _day(s, default):
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return default


def _left(d):
    return [(n["asn"], n.get("power", 0)) for n in d.get("neighbours", []) if n.get("type") in ("left", "uncertain")]


def remember_neighbors(conn, u, shown, at):
    """Record that RIPEstat showed `shown` [(asn, power)] next to upstream `u` in its data of `at`; forget what
    hasn't been seen for GONE_DAYS."""
    hist = db.kv_get(conn, NBR_KV, {}) or {}
    h = hist.setdefault(str(u), {})
    for a, power in shown:
        e = h.setdefault(str(a), {"first": at, "last": at, "peak": 0, "peak_at": at})
        e["first"], e["last"] = min(e["first"], at), max(e["last"], at)
        if (power or 0) > e["peak"]:
            e["peak"], e["peak_at"] = power or 0, at
    for a in [a for a, e in h.items() if e["last"] < time.time() - GONE_DAYS * 86400]:
        del h[a]
    db.kv_set(conn, NBR_KV, hist)
    conn.commit()


async def upstream_neighbors(sourceapp, upstreams):
    """{upstream: [(asn, power)]} - each upstream's own transits/peers (left neighbours), cached 6 h. Each fetch is
    also recorded in the neighbour history (see gone_neighbors)."""
    out = {}
    for u in upstreams:
        hit = _NBR.get(u)
        if not hit or time.time() - hit[0] > 6 * 3600:
            try:
                d = await ripe.stat("asn-neighbours", sourceapp, resource=f"AS{u}")
                lst = _left(d)
                hit = (time.time(), top_neighbors(lst, key=lambda x: x[0]), {a for a, _ in lst})
                _NBR[u] = hit
                if HISTORY is not None and lst:
                    remember_neighbors(HISTORY, u, hit[1], _day(d.get("query_endtime"), time.time()))
            except Exception as ex:
                log.warning("asn-neighbours AS%s: %s", u, ex)
                hit = hit or (0, [], set())
        out[u] = hit[1]
    return out


async def backfill_neighbors(sourceapp, u, days=(30, 21, 14, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1)):
    """Once per upstream: RIPEstat's asn-neighbours for some days of the past month (it keeps them by day), so a
    neighbour lost recently shows as "no longer peered" straight away. (Kept apart from the history itself, which
    a tree opened in the meantime may already have started.)"""
    done = (db.kv_get(HISTORY, NBR_DONE_KV, []) or []) if HISTORY is not None else None
    if done is None or u in done:
        return
    for n in days:
        at = (time.time() // 86400 - n) * 86400
        day = time.strftime("%Y-%m-%dT00:00:00", time.gmtime(at))
        try:
            d = await ripe.stat("asn-neighbours", sourceapp, resource=f"AS{u}", starttime=day, endtime=day)
        except Exception as ex:
            log.warning("asn-neighbours AS%s on %s: %s", u, day[:10], ex)
            continue
        lst = _left(d)
        if lst:
            remember_neighbors(HISTORY, u, top_neighbors(lst, key=lambda x: x[0]), at)
        await asyncio.sleep(1)
    db.kv_set(HISTORY, NBR_DONE_KV, sorted(set(done) | {u}))
    HISTORY.commit()
    log.info("neighbour history of AS%s loaded for the past %d days", u, max(days))


def gone_neighbors(conn, u):
    """[(asn, info)]: neighbours of upstream `u` seen in the last GONE_DAYS that RIPEstat's latest answer doesn't
    list at all, most recently seen first. [] until there is an answer (an empty one included: that says more
    about RIPEstat than about the upstream)."""
    hit = _NBR.get(u)
    if not hit or not hit[2]:
        return []
    h = (db.kv_get(conn, NBR_KV, {}) or {}).get(str(u), {})
    cutoff = time.time() - GONE_DAYS * 86400
    # (not a link seen on one day only, and small: RIS sees short-lived adjacencies all the time)
    gone = [(int(a), e) for a, e in h.items()
            if int(a) not in hit[2] and e["last"] >= cutoff and (e["last"] > e["first"] or e["peak"] >= GONE_MIN_PEAK)]
    return sorted(gone, key=lambda x: -x[1]["last"])


def state_paths(conn, prefix, ts):
    """AS paths (origin last) of every RIS peer for `prefix` at time `ts`."""
    return [[int(x) for x in r["path"].split()] for r in db.state_at(conn, prefix, ts)
            if r["kind"] == "A" and r["path"]]


# ---------------------------------------------------------------------- model
def is_x(k):
    """A tree node that groups peers rather than being a network: "x" (Exchanges) or "x:<exchange>"."""
    a = k[-1]
    return isinstance(a, str) and (a == "x" or a.startswith("x:"))


def group_peers(paths, asn, group):
    """Paths (origin last) with two virtual hops before your AS for peers, so the tree shows them on one
    "Exchanges" branch, split per exchange: [... peer, "x:<exchange>", "x", you]. `group`: peering.Peering.group."""
    out = []
    for p in paths:
        g = group(p[-2]) if len(p) >= 2 and p[-1] == asn else None
        out.append(p[:-1] + [f"x:{g}", "x", p[-1]] if g else p)
    return out


class Tree:
    def __init__(self, asn, states, upstreams, neighbors, depth=3, topn=6, gone=None, known=(), fold=False):
        """`known`: your configured upstreams and peers. An upstream's neighbour that carries none of your paths
        through it is "not carrying you" (red) only when it reaches you no other way either; one that's your own
        neighbour, or on your paths somewhere else, prefers that path: "reaches you another way" (faint blue), or
        with `fold`, one "reach you another way" line per upstream (as replay.js does with its fold choice)."""
        self.asn, self.depth = asn, depth
        root = (asn,)
        kids, maxc = {}, {}
        self.pinned, self.folds = {}, {}
        direct, anywhere = set(known) | set(upstreams), set()
        for paths in states:
            for p in paths:
                ints = [a for a in p[:-1] if isinstance(a, int)]
                if ints and p[-1] == asn:
                    direct.add(ints[-1])
                    anywhere.update(ints)

        def add(p, c):
            kids.setdefault(p, [])
            if c not in kids[p]:
                kids[p].append(c)

        def chain(path):
            r = path[::-1]
            return [tuple(r[:l + 1]) for l in range(min(depth, len(r) - 1) + 1)]

        for paths in states:
            cnt = {}
            for p in paths:
                ch = chain(p)
                for i, k in enumerate(ch):
                    cnt[k] = cnt.get(k, 0) + 1
                    if i:
                        add(ch[i - 1], k)
            for k, n in cnt.items():
                maxc[k] = max(maxc.get(k, 0), n)
        kids.setdefault(root, [])
        for u in upstreams:
            uk = (asn, u)
            add(root, uk)
            self.pinned[uk] = {"upstream": True}
            if depth >= 2:
                for n, power in neighbors.get(u, []):
                    if n == asn:
                        continue
                    nk = (asn, u, n)
                    kind = (None if maxc.get(nk) else "direct" if n in direct else "elsewhere" if n in anywhere
                            else "never")
                    if fold and kind in ("direct", "elsewhere"):
                        self.folds.setdefault(uk, []).append((n, kind))
                        continue
                    add(uk, nk)
                    self.pinned[nk] = {"of": u, "power": power, "kind": kind}
                for n, info in (gone or {}).get(u, []):   # "no longer peered" (gone_neighbors)
                    nk = (asn, u, n)
                    if nk not in self.pinned:
                        add(uk, nk)
                        self.pinned[nk] = {"of": u, "gone": info}
        self.maxc = maxc
        # collapse: pinned children always shown, otherwise top-N by peak count, the rest folded
        self.nodes, self.disp = {}, {}

        def visit(k, level, parent):
            self.nodes[k] = {"level": level, "parent": parent, "children": [], "others": 0, "folded": None}
            self.disp[k] = k
            if parent:
                self.nodes[parent]["children"].append(k)
            ch = sorted(kids.get(k, []), key=lambda c: (-(maxc.get(c, 0)), -(self.pinned.get(c, {}).get("power", 0))))
            free, rest = 0, []
            cap = max(topn, 40) if is_x(k) and k[-1] != "x" else topn   # an exchange's peers stay unfolded
            for c in ch:
                if c in self.pinned or is_x(c) or free < cap:
                    if c not in self.pinned and not is_x(c):
                        free += 1
                    visit(c, level + 1, k)
                else:
                    rest.append(c)
            if rest:
                ok = k + ("*",)
                self.nodes[ok] = {"level": level + 1, "parent": k, "children": [], "others": len(rest), "folded": None}
                self.nodes[k]["children"].append(ok)

                def mark(n):
                    self.disp[n] = ok
                    for c in kids.get(n, []):
                        mark(c)
                for r in rest:
                    mark(r)
            if self.folds.get(k):   # an upstream's neighbours that reach you another way, as one line
                fk = k + ("~",)
                self.nodes[fk] = {"level": level + 1, "parent": k, "children": [], "others": 0, "folded": self.folds[k]}
                self.nodes[k]["children"].append(fk)

        visit(root, 0, None)
        self._chain = chain
        # layout: one row per leaf, parents centred over their children
        self.rows = 0

        def place(k):
            n = self.nodes[k]
            if not n["children"]:
                n["row"] = self.rows
                self.rows += 1
                return
            for c in n["children"]:
                place(c)
            n["row"] = (self.nodes[n["children"][0]]["row"] + self.nodes[n["children"][-1]]["row"]) / 2
        place(root)

    def counts(self, paths):
        out = {}
        for p in paths:
            seen = set()
            for k in self._chain(p):
                d = self.disp.get(k)
                if d is None or d in seen:
                    continue
                seen.add(d)
                out[d] = out.get(d, 0) + 1
                if d[-1] == "*":
                    break
        return out

    def visible(self, paths):
        return sum(1 for p in paths if p and p[-1] == self.asn)


# ---------------------------------------------------------------------- drawing
def _bezier(x0, y0, x1, y1, n=28):
    mx = (x0 + x1) / 2
    pts = []
    for i in range(n + 1):
        t = i / n
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
        pts.append((a * x0 + b * mx + c * mx + d * x1, a * y0 + b * y0 + c * y1 + d * y1))
    return pts


def _polyline(dr, pts, fill, width, dash=None):
    """A line through `pts`; `dash`: on/off lengths, any even number of them ((6, 3, 1, 3) is dash-dot)."""
    if not dash:
        dr.line(pts, fill=fill, width=width, joint="curve")
        return
    seq, idx, pos, drawing = list(dash), 0, 0.0, True
    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
        seg = math.hypot(xb - xa, yb - ya)
        t = 0.0
        while t < seg:
            step = min(seq[idx] - pos, seg - t)
            if drawing:
                f0, f1 = t / seg, (t + step) / seg
                dr.line([(xa + (xb - xa) * f0, ya + (yb - ya) * f0), (xa + (xb - xa) * f1, ya + (yb - ya) * f1)],
                        fill=fill, width=width)
            t += step
            pos += step
            if pos >= seq[idx] - 1e-9:
                pos, idx, drawing = 0.0, (idx + 1) % len(seq), not drawing


def _blend(c, alpha):
    return tuple(int(v * alpha + 255 * (1 - alpha)) for v in c)


def render(tree, names, now_paths, base_paths, total, title, subtitle, W=1100):
    """PNG of the tree at one moment; colours compare `now_paths` with `base_paths`."""
    rowh, top = 22, 98
    H = top + max(tree.rows, 3) * rowh + 26
    S = 2   # draw at 2x and downsample for smooth lines
    img = Image.new("RGB", (W * S, H * S), C["bg"])
    dr = ImageDraw.Draw(img)
    counts, base = tree.counts(now_paths), tree.counts(base_paths)
    vis = tree.visible(now_paths)
    total = max(total, 1)
    pct = round(100 * vis / total)

    def T(xy, text, f, fill):
        dr.text((xy[0] * S, xy[1] * S), text, font=font(f[0] * S, f[1]), fill=fill)

    T((16, 10), title, (17, True), C["text"])
    T((16, 36), subtitle, (12, False), C["muted"])
    pcol = C["crit"] if pct < 50 else C["warn"] if pct < 80 else C["ok"]
    T((16, 52), f"{vis}/{total} RIS peers can see the prefix ({pct}%)", (13, True), pcol)
    # legend
    # (the same patterns as the branches: dropped red dashed and thick, not carrying you red dotted, reaches you
    # another way blue dotted, no longer peered grey dash-dot)
    lg = [("carrying", C["accent"], None, 3), ("lost >50%", C["warn"], None, 3), ("dropped", C["crit"], (6, 4), 4),
          ("new", C["ok"], None, 3), ("not carrying you", C["crit"], (1, 3), 2),
          ("reaches you another way", C["accent"], (1, 3), 2), ("no longer peered", C["muted"], (6, 3, 1, 3), 2)]
    x = W - 16
    for label, col, dash, lw in reversed(lg):
        w = dr.textlength(label, font=font(11 * S)) / S
        x -= w
        T((x, 14), label, (11, False), C["muted"])
        x -= 26
        _polyline(dr, [((x) * S, 21 * S), ((x + 20) * S, 21 * S)], col, lw * S,
                  tuple(v * S for v in dash) if dash else None)
        x -= 14

    colw = (W - 24 - 230) / max(tree.depth, 1)
    cols = (["You", "Upstreams · exchanges", "Their transits & peers · exchange", "Next hop out · your peers",
             "Further out", "Further out"] if any(is_x(k) for k in tree.nodes if len(k) > 1)
            else ["You", "Your upstreams", "Their transits & peers", "Next hop out", "Further out", "Further out"])
    for lvl in range(tree.depth + 1):
        T((24 + lvl * colw - (4 if lvl else 0), 74), cols[min(lvl, len(cols) - 1)].upper(), (11, True), C["muted"])
    dr.line([(16 * S, 90 * S), ((W - 16) * S, 90 * S)], fill=C["border"], width=S)
    pos = lambda k: (24 + tree.nodes[k]["level"] * colw, top + tree.nodes[k]["row"] * rowh + rowh / 2)

    def state(k):
        c, b = counts.get(k, 0), base.get(k, 0)
        if tree.nodes[k]["folded"]:
            return "fold", C["muted"]
        if not c and tree.pinned.get(k, {}).get("gone"):
            return "gone", C["muted"]
        if c and b:
            return ("degraded", C["warn"]) if c < b * 0.5 else ("live", C["accent"])
        if b and not c:
            return "lost", C["crit"]
        if c and not b:
            return "new", C["ok"]
        if k in tree.pinned:   # (faint blue when it reaches you another way, red when RIS sees it carrying you nowhere)
            kind = tree.pinned[k].get("kind")
            return (kind, C["accent"]) if kind in ("direct", "elsewhere") else ("never", C["crit"])
        return "idle", C["border"]
    SOFT = ("never", "gone", "direct", "elsewhere", "fold")

    wof = lambda c: max(1, int(round((1 + 8 * math.sqrt(c / total)) * S)))
    for k, n in tree.nodes.items():
        if not n["parent"]:
            continue
        (x0, y0), (x1, y1) = pos(n["parent"]), pos(k)
        st, col = state(k)
        c, b = counts.get(k, 0), base.get(k, 0)
        if st == "lost":
            width, dash = max(int(2.5 * S), wof(b) // 2), (6 * S, 4 * S)
        elif st == "gone":
            width, dash, col = int(1.4 * S), (6 * S, 3 * S, 1 * S, 3 * S), _blend(col, 0.6)
        elif st == "never":
            width, dash, col = int(1.4 * S), (1 * S, 3 * S), _blend(col, 0.8)
        elif st in SOFT:   # reaches you another way, or the folded line of those
            width, dash, col = int(1.4 * S), (1 * S, 3 * S), _blend(col, 0.55)
        elif st == "idle":
            width, dash = S, None
        else:
            width, dash = wof(c), None
        _polyline(dr, [(px * S, py * S) for px, py in _bezier(x0, y0, x1, y1)], col, width, dash)
    for k, n in tree.nodes.items():
        x, y = pos(k)
        st, col = state(k)
        c, b = counts.get(k, 0), base.get(k, 0)
        r = 7 if n["level"] == 0 else 3 + 4 * math.sqrt(max(c, b * 0.3 if st == "lost" else 0) / total)
        hollow = st == "idle" or st in SOFT
        box = [(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S]
        if len(k) > 1 and is_x(k):   # a grouping (exchanges), not a network
            dr.rectangle(box, fill=C["bg"] if hollow else col, outline=col, width=max(1, int(1.5 * S)))
        else:
            dr.ellipse(box, fill=C["bg"] if hollow else col, outline=col, width=max(1, int(1.5 * S)))
        if n["others"]:
            label = f"+{n['others']} more"
        elif n["folded"]:
            label = f"+{len(n['folded'])} reach you another way"
        elif len(k) > 1 and is_x(k):
            label = "Exchanges" if k[-1] == "x" else k[-1][2:]
        else:
            a = k[-1]
            nm = (names.get(a) or "").split(" ")[0].split(",")[0][:16]
            label = f"AS{a}" + (" (you)" if n["level"] == 0 else (f" {nm}" if nm else ""))
        lx = x + r + 5
        has_kids = bool(n["children"])
        T((lx, y - (13 if has_kids else 7)), label, (12, n["level"] <= 1 or (len(k) > 1 and is_x(k))),
          C["muted"] if st == "lost" or st == "idle" or st in SOFT else C["text"])
        cnt = ("" if n["level"] == 0 or n["folded"]
               else f"not carrying you via AS{tree.pinned[k]['of']}" if st == "never"
               else "reaches you directly" if st == "direct" else "reaches you another way" if st == "elsewhere"
               else "no longer peered" if st == "gone" else f"0 (was {b})" if st == "lost"
               else f"{c} of {b}" if st == "degraded" else f"{c} (new)" if st == "new" else f"{c}" if c else "")
        if cnt:
            ccol = C["crit"] if st in ("lost", "never") else C["warn"] if st == "degraded" else \
                C["ok"] if st == "new" else C["muted"]
            if has_kids:
                T((lx, y + 1), cnt, (11, False), ccol)
            else:
                lw = dr.textlength(label, font=font(12 * S, n["level"] <= 1)) / S
                T((lx + lw + 6, y - 6), cnt, (11, False), ccol)
    img = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------- per-event images
TREE_KINDS = ("outage", "upstream_missing", "unexpected_upstream", "origin_hijack", "more_specific", "upstream_shift",
              "upstream_down", "exchange_loss", "peer_leak")


def _monitored_parent(cfg, prefix):
    try:
        net = ipaddress.ip_network(prefix, strict=False)
    except (TypeError, ValueError):
        return None
    cands = [(ipaddress.ip_network(p).prefixlen, p) for p in cfg.prefixes
             if ipaddress.ip_network(p).version == net.version and net.subnet_of(ipaddress.ip_network(p))]
    return max(cands)[1] if cands else None


async def event_images(cfg, sourceapp, ev, phase, now=None, peering=None):
    """[(cid, png_bytes, caption)] for an alert email, or [] if the event has no tree to show.
    `peering`: peering.Peering, so peers go on the tree's "Exchanges" branch."""
    if ev.get("kind") not in TREE_KINDS:
        return []
    prefix = _monitored_parent(cfg, ev.get("prefix"))
    if not prefix:
        return []
    now = now or time.time()
    B = cfg.bucket_seconds
    conn = db.connect(cfg.db_path)
    try:
        start = ev["start_ts"]
        # "before": the healthiest moment in the 3 hours before the event
        r = conn.execute("SELECT ts FROM buckets WHERE prefix=? AND ts BETWEEN ? AND ? "
                         "ORDER BY visible DESC, ts DESC LIMIT 1", (prefix, start - 3 * 3600, start - B)).fetchone()
        t_before = (r["ts"] + B) if r else start - 300
        moments = [("before", t_before, "Before the event")]
        if phase == "close" and ev.get("end_ts"):
            w = conn.execute("SELECT ts FROM buckets WHERE prefix=? AND ts BETWEEN ? AND ? "
                             "ORDER BY COALESCE(min_visible, visible) ASC, ts LIMIT 1",
                             (prefix, start - B, ev["end_ts"])).fetchone()
            if w:
                moments.append(("worst", w["ts"] + B / 2, "Worst point"))
            moments.append(("after", min(now, ev["end_ts"] + 120), "Recovered"))
        else:
            moments.append(("now", now, "Now (when this alert was sent)"))
        states = {k: await asyncio.to_thread(state_paths, conn, prefix, ts) for k, ts, _ in moments}
        group = peering.group if peering else (lambda a: None)
        ups = set(cfg.expected_upstreams)
        for paths in states.values():
            ups.update(p[-2] for p in paths if len(p) >= 2 and p[-1] == cfg.asn and not group(p[-2]))
        states = {k: group_peers(v, cfg.asn, group) for k, v in states.items()}
        base = states["before"]
        nbrs = await upstream_neighbors(sourceapp, sorted(ups))
        gone = {u: gone_neighbors(conn, u) for u in ups}
        asns = {a for paths in states.values() for p in paths for a in p[-6:] if isinstance(a, int)} | ups | \
               {n for lst in nbrs.values() for n, _ in lst} | {n for lst in gone.values() for n, _ in lst}
        names = {}
        if asns:
            q = ",".join("?" * len(asns))
            names = {r["asn"]: r["name"] for r in conn.execute(f"SELECT asn, name FROM asn_names WHERE asn IN ({q})",
                                                                list(asns))}
        tree = Tree(cfg.asn, list(states.values()), sorted(ups), nbrs, gone=gone,
                    known=set(cfg.expected_upstreams) | set(cfg.peers))
        total = max(tree.visible(p) for p in states.values())
        out = []
        for key, ts, label in moments:
            stamp = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(ts))
            sub = (f"{prefix} · AS{cfg.asn} · colours compared with {time.strftime('%H:%M', time.localtime(t_before))} "
                   f"(before the event)" if key != "before" else f"{prefix} · AS{cfg.asn} · healthy reference")
            png = await asyncio.to_thread(render, tree, names, states[key], base, total, f"{label} · {stamp}", sub)
            out.append((f"tree-{key}", png, f"{label} ({stamp})"))
        return out
    finally:
        conn.close()
