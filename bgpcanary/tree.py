"""Server-side connectivity-tree images (PNG) for alert emails.

Same model as the dashboard tree (static/replay.js): your AS at the root, your upstreams, each upstream's
own transits/peers (always shown, so "not carrying you" is visible), then the next hop out. Each image
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
_NBR = {}   # upstream -> (ts, [(asn, power)])
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
async def upstream_neighbors(sourceapp, upstreams):
    """{upstream: [(asn, power)]} - each upstream's own transits/peers (left neighbours), cached 6 h."""
    out = {}
    for u in upstreams:
        hit = _NBR.get(u)
        if not hit or time.time() - hit[0] > 6 * 3600:
            try:
                d = await ripe.stat("asn-neighbours", sourceapp, resource=f"AS{u}")
                lst = [(n["asn"], n.get("power", 0)) for n in d.get("neighbours", [])
                       if n.get("type") in ("left", "uncertain")]
                hit = (time.time(), top_neighbors(lst, key=lambda x: x[0]))
                _NBR[u] = hit
            except Exception as ex:
                log.warning("asn-neighbours AS%s: %s", u, ex)
                hit = hit or (0, [])
        out[u] = hit[1]
    return out


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
    def __init__(self, asn, states, upstreams, neighbors, depth=3, topn=6):
        self.asn, self.depth = asn, depth
        root = (asn,)
        kids, maxc = {}, {}
        self.pinned = {}

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
                    add(uk, nk)
                    self.pinned[nk] = {"of": u, "power": power}
        self.maxc = maxc
        # collapse: pinned children always shown, otherwise top-N by peak count, the rest folded
        self.nodes, self.disp = {}, {}

        def visit(k, level, parent):
            self.nodes[k] = {"level": level, "parent": parent, "children": [], "others": 0}
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
                self.nodes[ok] = {"level": level + 1, "parent": k, "children": [], "others": len(rest)}
                self.nodes[k]["children"].append(ok)

                def mark(n):
                    self.disp[n] = ok
                    for c in kids.get(n, []):
                        mark(c)
                for r in rest:
                    mark(r)

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
    if not dash:
        dr.line(pts, fill=fill, width=width, joint="curve")
        return
    on, off = dash
    pos, drawing = 0.0, True
    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
        seg = math.hypot(xb - xa, yb - ya)
        t = 0.0
        while t < seg:
            left = (on if drawing else off) - pos
            step = min(left, seg - t)
            if drawing:
                f0, f1 = t / seg, (t + step) / seg
                dr.line([(xa + (xb - xa) * f0, ya + (yb - ya) * f0), (xa + (xb - xa) * f1, ya + (yb - ya) * f1)],
                        fill=fill, width=width)
            t += step
            pos += step
            if pos >= (on if drawing else off) - 1e-9:
                pos, drawing = 0.0, not drawing


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
    lg = [("carrying", C["accent"], None), ("lost >50%", C["warn"], None), ("dropped", C["crit"], (5, 4)),
          ("new", C["ok"], None), ("not carrying you", C["crit"], (2, 3))]
    x = W - 16
    for label, col, dash in reversed(lg):
        w = dr.textlength(label, font=font(11 * S)) / S
        x -= w
        T((x, 14), label, (11, False), C["muted"])
        x -= 26
        _polyline(dr, [((x) * S, 21 * S), ((x + 20) * S, 21 * S)], col, 3 * S, tuple(v * S for v in dash) if dash else None)
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
        if c and b:
            return ("degraded", C["warn"]) if c < b * 0.5 else ("live", C["accent"])
        if b and not c:
            return "lost", C["crit"]
        if c and not b:
            return "new", C["ok"]
        if k in tree.pinned:
            return "never", C["crit"]
        return "idle", C["border"]

    wof = lambda c: max(1, int(round((1 + 8 * math.sqrt(c / total)) * S)))
    for k, n in tree.nodes.items():
        if not n["parent"]:
            continue
        (x0, y0), (x1, y1) = pos(n["parent"]), pos(k)
        st, col = state(k)
        c, b = counts.get(k, 0), base.get(k, 0)
        if st == "lost":
            width, dash = max(S, wof(b) // 2), (5 * S, 4 * S)
        elif st == "never":
            width, dash, col = int(1.4 * S), (2 * S, 3 * S), _blend(col, 0.6)
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
        hollow = st in ("idle", "never")
        box = [(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S]
        if len(k) > 1 and is_x(k):   # a grouping (exchanges), not a network
            dr.rectangle(box, fill=C["bg"] if hollow else col, outline=col, width=max(1, int(1.5 * S)))
        else:
            dr.ellipse(box, fill=C["bg"] if hollow else col, outline=col, width=max(1, int(1.5 * S)))
        if n["others"]:
            label = f"+{n['others']} more"
        elif len(k) > 1 and is_x(k):
            label = "Exchanges" if k[-1] == "x" else k[-1][2:]
        else:
            a = k[-1]
            nm = (names.get(a) or "").split(" ")[0].split(",")[0][:16]
            label = f"AS{a}" + (" (you)" if n["level"] == 0 else (f" {nm}" if nm else ""))
        lx = x + r + 5
        has_kids = bool(n["children"])
        T((lx, y - (13 if has_kids else 7)), label, (12, n["level"] <= 1 or (len(k) > 1 and is_x(k))),
          C["muted"] if st in ("idle", "lost", "never") else C["text"])
        cnt = ("" if n["level"] == 0 else "not carrying you" if st == "never" else f"0 (was {b})" if st == "lost"
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
              "exchange_loss", "peer_leak")


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
        asns = {a for paths in states.values() for p in paths for a in p[-6:] if isinstance(a, int)} | ups | \
               {n for lst in nbrs.values() for n, _ in lst}
        names = {}
        if asns:
            q = ",".join("?" * len(asns))
            names = {r["asn"]: r["name"] for r in conn.execute(f"SELECT asn, name FROM asn_names WHERE asn IN ({q})",
                                                                list(asns))}
        tree = Tree(cfg.asn, list(states.values()), sorted(ups), nbrs)
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
