"""Everything known about an event in one place: BGP (upstream mix + raw AS paths), Route Views output and
RIPE Atlas results, and what else went on around it whether or not that raised an event of its own, for the event
page and a plain-text evidence report to send to a provider."""
import ipaddress
import json
import statistics
import time

from . import changes, db, paths

PAD = 1800   # look this far before the start and after the end
LONG = 6 * 3600   # a window longer than this: its route changes are spelled out around the start and the end only
BURSTS = 4        # ...and the biggest few bursts of each stretch, not every one


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def _rounds(rows, gap=120):
    """Group (msm, ts)-ordered rows into rounds: one measurement's results with no gap longer than `gap`."""
    out, cur = [], None
    for r in rows:
        if not cur or cur[-1]["msm_id"] != r["msm_id"] or r["ts"] - cur[-1]["ts"] > gap:
            cur = []
            out.append(cur)
        cur.append(r)
    return out


def _upstream_mix(conn, prefix, ev, s, e):
    """Peers per upstream before the event, at its most-changed bucket, and after it ended; and who moved between
    the first two (paths.movers over the buckets' chains)."""
    rows = db.fill_mix(conn, conn.execute("SELECT ts, visible, upstreams, chains FROM buckets WHERE prefix=? AND ts>=? "
                                          "AND ts<=? ORDER BY ts", (prefix, s, e)), prefix)
    if not rows:
        return None
    start, end = ev["start_ts"], ev["end_ts"]
    mix = lambda r: {int(k): v for k, v in json.loads(r["upstreams"] or "{}").items() if k != "None"}
    before = next((r for r in reversed(rows) if r["ts"] < start), rows[0])
    b = mix(before)
    during_rows = [r for r in rows if start <= r["ts"] <= (end or e)] or [before]
    dist = lambda r: sum(abs(mix(r).get(u, 0) - b.get(u, 0)) for u in set(mix(r)) | set(b)) + abs(r["visible"] - before["visible"])
    worst = max(during_rows, key=dist)
    after = next((r for r in rows if end and r["ts"] >= end + 60), None) or rows[-1]
    moments = [("before", before), ("during (peak change)", worst), ("after" if end else "now", after)]
    ups = sorted(set().union(*(mix(r) for _, r in moments)), key=lambda u: -max(mix(r).get(u, 0) for _, r in moments))
    floor = max(3, int((before["visible"] or 0) * 0.02))
    movers = paths.movers(json.loads(before["chains"] or "{}"), json.loads(worst["chains"] or "{}"), floor)[:4]
    return {"moments": [{"label": l, "ts": r["ts"], "visible": r["visible"], "mix": mix(r)} for l, r in moments],
            "upstreams": ups, "movers": movers}


def _states(conn, prefix, moments):
    """The RIS peers' stored routes toward `prefix` at each of `moments`, as db.state_at gives them for one:
    {moment: rows}. state_at goes through the prefix's whole history, so it's asked once for the first of a run of
    moments and the state is carried forward through the updates to the others; a moment more than LONG after the
    one before it starts a new run (weeks of updates cost more to carry it through than to ask again)."""
    out, want, i = {}, sorted(set(moments)), 0
    while i < len(want):
        j = i
        while j + 1 < len(want) and want[j + 1] - want[j] <= LONG:
            j += 1
        cur = {(r["collector"], r["peer"]): dict(r) for r in db.state_at(conn, prefix, want[i])}
        out[want[i]] = list(cur.values())
        if j > i:
            k = i + 1
            for r in conn.execute("SELECT collector, peer, peer_asn, kind, path, ts FROM updates WHERE prefix=? AND ts>? "
                                  "AND ts<=? ORDER BY ts, id", (prefix, want[i], want[j])):
                while r["ts"] > want[k]:
                    out[want[k]] = list(cur.values())
                    k += 1
                cur[(r["collector"], r["peer"])] = dict(r)
            for t in want[k:j + 1]:
                out[t] = list(cur.values())
        i = j + 1
    return out


def _paths_of(rows, top=4):
    """Distinct AS paths in a state (db.state_at's rows), grouped by upstream (bgpcanary stores RIS paths with
    prepends collapsed; Route Views' raw output keeps them)."""
    by = {}
    for r in rows:
        if r["kind"] != "A" or not r["path"]:
            continue
        up = paths.upstream(paths.parse_str(r["path"]))
        g = by.setdefault(up, {})
        g[r["path"]] = g.get(r["path"], 0) + 1
    return [{"upstream": u, "peers": sum(g.values()),
             "paths": sorted(g.items(), key=lambda x: -x[1])[:top], "more": max(0, len(g) - top)}
            for u, g in sorted(by.items(), key=lambda x: -sum(x[1].values()))]


def _rv_runs(conn, ev, s, e):
    """Route Views snapshot runs (all collectors queried within 2 minutes): the last routine one before the
    event, and every run from the start to the end of the window (event-triggered or not)."""
    rows = conn.execute("SELECT * FROM rv_snapshots WHERE (ts>=? AND ts<=?) OR event_id=? ORDER BY ts",
                        (ev["start_ts"] - 86400, e, ev["id"])).fetchall()
    runs, cur = [], None
    for r in rows:
        if not cur or r["ts"] - cur[-1]["ts"] > 120:
            cur = []
            runs.append(cur)
        cur.append(r)
    before = [run for run in runs if run[-1]["ts"] < ev["start_ts"] and not any(x["event_id"] for x in run)][-1:]
    during = [run for run in runs if run[0]["ts"] >= ev["start_ts"] - 120 or any(x["event_id"] == ev["id"] for x in run)]
    out = []
    for kind, lst in (("last routine run before the event", before), ("during / after", during)):
        for run in lst[:6]:
            out.append({"label": "event-triggered" if any(x["event_id"] == ev["id"] for x in run) else kind,
                        "ts": run[0]["ts"], "snaps": run})
    return out


def window(ev, now=None):
    """The stretch an event's evidence covers: PAD before its start to PAD after its end (or now)."""
    now = now or time.time()
    return ev["start_ts"] - PAD, min((ev["end_ts"] or now) + PAD, now)


def focus_prefix(conn, cfg, ev, s, e):
    """The monitored prefix an event's picture is drawn from, and how it was picked: (prefix, "own") when the
    event's prefix is monitored; (the monitored prefix it overlaps, "overlap") for a more-specific or covering one;
    and for an event that names no prefix (a reachability loss on a carrier's link address, a Path finder watch)
    the monitored prefix whose routes changed most between `s` and `e`, the first one when none did, as
    (prefix, "busiest"). (None, None) when there's none to show."""
    if ev["prefix"] in cfg.prefixes:
        return ev["prefix"], "own"
    if ev["prefix"]:
        try:
            net = ipaddress.ip_network(ev["prefix"], strict=False)
            p = next((p for p in cfg.prefixes if ipaddress.ip_network(p).overlaps(net)), None)
        except ValueError:
            p = None
        return p, "overlap" if p else None
    if not cfg.prefixes:
        return None, None
    q = ",".join("?" * len(cfg.prefixes))
    r = conn.execute(f"SELECT prefix, SUM(updates) n FROM buckets WHERE prefix IN ({q}) AND ts>=? AND ts<=? "
                     "GROUP BY prefix ORDER BY n DESC, prefix LIMIT 1", (*cfg.prefixes, s, e)).fetchone()
    return (r["prefix"] if r and r["n"] else cfg.prefixes[0]), "busiest"


def _spans(ev, s, e, now):
    """The stretches of the window whose route changes are spelled out, as (label, start, end): all of it, or for a
    long one the half hours around the event's start and around its end (the last half hour while it's open)."""
    if e - s <= LONG:
        return [("", s, e)]
    start, end = ev["start_ts"], ev["end_ts"]
    return [("around the start", start - PAD, start + PAD),
            ("around the end", end - PAD, min(end + PAD, now)) if end else ("the last half hour", now - PAD, now)]


def _net_moves(chs):
    """Where a run of path changes (changes.path_changes rows, oldest first) left each RIS peer, against where it
    was before it: ([((old way in, new way in), peers)] biggest first, how many ended where they began). A peer
    that changed several times counts once."""
    first, last = {}, {}
    for c in chs:
        k = (c["collector"], c["peer"])
        first.setdefault(k, c["old"])
        last[k] = c["new"]
    moves, back = {}, 0
    for k, old in first.items():
        if old == last[k]:
            back += 1
            continue
        key = (changes.chain_key(old) if old else None, changes.chain_key(last[k]) if last[k] else None)
        moves[key] = moves.get(key, 0) + 1
    return sorted(moves.items(), key=lambda x: -x[1]), back


def _routes(conn, prefix, label, s, e, B, state):
    """What the RIS peers' routes to `prefix` did between `s` and `e`, whether or not it raised an event: the bursts
    of path changes that moved a fair share of the peers (the biggest BURSTS, oldest first, each with where it left
    them), how many smaller ones there were, the peers per upstream at both ends ("changed": the upstreams whose
    count differs) and who moved between the two (paths.movers over the buckets' chains). `B`: the bucket length; a
    bucket holds the mix as it stood at its end. `state`: the peers' routes at `s` (db.state_at's rows). None when
    nothing is stored for that stretch."""
    rows = db.fill_mix(conn, conn.execute("SELECT ts, visible, min_visible, upstreams, chains FROM buckets WHERE prefix=? "
                                          "AND ts>=? AND ts<=? ORDER BY ts", (prefix, s, e)), prefix)
    if not rows:
        return None
    first, last = rows[0], rows[-1]
    floor = max(3, int((first["visible"] or 0) * 0.02))
    chs = changes.path_changes(conn, prefix, s, e, state=state)
    big, small = [], []
    for b in reversed(changes.bursts_of(chs)):   # (bursts_of lists the newest first)
        moves, back = _net_moves(b["changes"])
        (big if b["peers"] >= floor else small).append(
            {"start": b["start"], "end": b["end"], "peers": b["peers"], "moves": moves, "back": back})
    shown = sorted(sorted(big, key=lambda b: -b["peers"])[:BURSTS], key=lambda b: b["start"])
    mix = lambda r: {int(k): v for k, v in json.loads(r["upstreams"] or "{}").items() if k != "None"}   # noqa: E731
    before, after = mix(first), mix(last)
    movers = paths.movers(json.loads(first["chains"] or "{}"), json.loads(last["chains"] or "{}"), floor)[:4]
    low = min(rows, key=lambda r: r["min_visible"] if r["min_visible"] is not None else r["visible"])
    lowest = low["min_visible"] if low["min_visible"] is not None else low["visible"]
    ups = sorted(set(before) | set(after), key=lambda u: -max(before.get(u, 0), after.get(u, 0)))
    changed = [u for u in ups if abs(before.get(u, 0) - after.get(u, 0)) >= floor]
    return {"label": label, "start": first["ts"] + B, "end": min(last["ts"] + B, max(e, last["ts"])),
            "peers": first["visible"], "bursts": shown, "more_bursts": len(big) - len(shown), "small": len(small),
            "before": before, "after": after, "upstreams": ups, "changed": changed, "movers": movers,
            "settled": not movers and not changed,
            "lowest": lowest if (first["visible"] or 0) - lowest >= floor else None, "lowest_ts": low["ts"]}


def _judge_rounds(conn, rounds, s):
    """Mark, in place, the ping rounds that stand out: "usual_no_reply" (how many probes of that measurement
    usually get nothing back: the median over the day before `s`, as some probes never answer; the best round here
    when there's no day before) and "odd": 'loss' when clearly more than that got no reply, 'slow' when the
    median round trip at least doubled, else None."""
    msms = sorted({r["msm"] for r in rounds})
    usual = {}
    if msms:
        q = ",".join("?" * len(msms))
        by = {}
        # (+msm_id: found by time, a day's rows, not by measurement, whose rows go back as far as the install does)
        for rs in _rounds(conn.execute(
                f"SELECT msm_id, prb_id, ts, sent, rcvd, rtt_avg FROM atlas_result WHERE kind='ping' AND +msm_id IN ({q}) "
                "AND ts>=? AND ts<? ORDER BY msm_id, ts", (*msms, s - 86400, s)).fetchall()):
            if len(rs) < 5:   # a straggler's result, not a round
                continue
            rtts = [r["rtt_avg"] for r in rs if r["rtt_avg"] is not None]
            by.setdefault(rs[0]["msm_id"], []).append((sum(1 for r in rs if r["sent"] and not r["rcvd"]),
                                                       statistics.median(rtts) if rtts else None))
        for m, v in by.items():
            rt = [x for _, x in v if x is not None]
            usual[m] = (statistics.median([f for f, _ in v]), statistics.median(rt) if rt else None)
    by_msm = {}
    for r in rounds:
        by_msm.setdefault(r["msm"], []).append(r)
    for m, mine in by_msm.items():
        fails, rtt = usual.get(m) or (min(x["no_reply"] for x in mine), None)
        if rtt is None:
            rtt = min((x["median_rtt"] for x in mine if x["median_rtt"]), default=None)
        for r in mine:
            r["usual_no_reply"] = min(int(fails + 0.5), r["probes"])
            r["usual_rtt"] = rtt
            r["odd"] = ("loss" if r["no_reply"] - fails >= max(3, 0.1 * r["probes"]) or r["no_reply"] * 2 >= r["probes"]
                        else "slow" if r["median_rtt"] and rtt and r["median_rtt"] >= max(2 * rtt, rtt + 50) else None)


def _other_events(conn, ev, s, e, now):
    """Every other event that was open at some point of the window, oldest first, whatever its kind and whether it
    was seen live or found during a backfill; "after": how long after this event's start it began (negative:
    before). One stored more than once (releases before 1.7.3 could record a backfilled event again) is listed
    once, with "copies"."""
    own = (ev["kind"], ev["prefix"], ev["start_ts"], ev["title"])
    seen, out = {}, []
    for r in conn.execute("SELECT id, kind, severity, title, prefix, start_ts, end_ts, historical FROM events WHERE id!=? "
                          "AND start_ts<=? AND COALESCE(end_ts, ?)>=? ORDER BY start_ts, id", (ev["id"], e, now, s)):
        k = (r["kind"], r["prefix"], r["start_ts"], r["title"])
        if k == own:
            continue
        if k in seen:
            seen[k]["copies"] += 1
            continue
        seen[k] = dict(r, copies=1, after=r["start_ts"] - ev["start_ts"])
        out.append(seen[k])
    return out


def _unheard(conn, prefix, spans):
    """What bgpcanary didn't hear live in the stretches looked at (`spans`, as _spans gives them). "open": the parts
    RIS Live missed that are still to be filled from RIPE's archive (the service's list of gaps, and the stretch
    in the making while RIS Live is down), clipped to them. "filled": how many of their updates for `prefix` came
    from the archive afterwards, and the first and last of them; None when all were heard live."""
    gaps = db.kv_get(conn, "gaps", []) or []
    since = db.kv_get(conn, "ris_unheard_since")   # (service.UNHEARD_KV)
    open_, filled = set(), None
    for _span, s, e in spans:
        open_.update((max(a, s), min(b, e)) for a, b in gaps if a <= e and b >= s)
        if since and since <= e:
            open_.add((max(since, s), e))
        f = conn.execute("SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM updates WHERE prefix=? AND ts>=? AND ts<=? "
                         "AND source='bgplay'", (prefix, s, e)).fetchone() if prefix else None
        if f and f["n"]:
            filled = {"start": min(f["a"], filled["start"]) if filled else f["a"],
                      "end": max(f["b"], filled["end"]) if filled else f["b"], "n": f["n"] + (filled["n"] if filled else 0)}
    return {"open": sorted(open_), "filled": filled}


def collect(conn, cfg, ev, atlas_payload, nbrs, label_of, name_of):
    """Evidence for one event. `atlas_payload(conn, target, s, e, msm, nbrs=)` builds traceroute views;
    `label_of(target)` gives a baseline target's label; `name_of(asns)` returns {asn: name}."""
    ev = dict(ev)
    start = ev["start_ts"]
    end = ev["end_ts"]
    now = time.time()
    s, e = window(ev, now)
    # (an event that names none of your prefixes still gets the routes of one: where its cause often shows)
    prefix, how = focus_prefix(conn, cfg, ev, s, e)
    out = {"window": (s, e), "prefix": prefix, "prefix_how": how, "related": [], "mix": None, "paths": [], "rv_runs": [],
           "rv_probes": [], "atlas_rounds": [], "atlas_traces": [], "timeline": [], "names": {}}
    asns = set()

    out["related"] = _other_events(conn, ev, s, e, now)
    spans = _spans(ev, s, e, now)
    around = out["around"] = {"routes": [], "unheard": _unheard(conn, prefix, spans)}
    if prefix:
        B = cfg.bucket_seconds
        out["mix"] = _upstream_mix(conn, prefix, ev, s, e)
        # the peers' routes at every moment looked at below, from one read of the stored history
        at = [m["ts"] + B for m in out["mix"]["moments"]] if out["mix"] else []   # (a bucket's mix is the one at its end)
        states = _states(conn, prefix, at + [s0 for _span, s0, _e0 in spans])
        for span, s0, e0 in spans:
            sp = _routes(conn, prefix, span, s0, e0, B, states[s0])
            if sp:
                around["routes"].append(sp)
                asns.update(sp["upstreams"])
                asns.update(a for m in sp["movers"] for a in (m["asn"], m["from"], m["to"]) if a)
                asns.update(a for bu in sp["bursts"] for way, _n in bu["moves"] for c in way if c for a in c if a)
        if out["mix"]:
            asns.update(out["mix"]["upstreams"])
            asns.update(a for m in out["mix"]["movers"] for a in (m["asn"], m["from"], m["to"]) if a)
            for m in out["mix"]["moments"]:
                ps = _paths_of(states[m["ts"] + B])
                out["paths"].append({"label": m["label"], "ts": m["ts"], "groups": ps})
                for g in ps:
                    asns.add(g["upstream"])
                    for p, _n in g["paths"]:
                        asns.update(int(x) for x in p.split() if x.isdigit())

    out["rv_runs"] = _rv_runs(conn, ev, s, e)
    out["rv_probes"] = db.annotate_probes(conn, conn.execute(
        "SELECT * FROM probes WHERE event_id=? OR (ts>=? AND ts<=?) ORDER BY ts", (ev["id"], s, e)).fetchall())

    # RIPE Atlas: every ping round to your own targets in the window, and every traceroute run
    mine = {r["target"] for r in conn.execute("SELECT DISTINCT target FROM atlas_msm WHERE purpose IN ('baseline','check')")}
    prow = conn.execute("SELECT r.msm_id, r.prb_id, r.ts, r.sent, r.rcvd, r.rtt_avg, m.target, m.purpose FROM atlas_result r "
                        "JOIN atlas_msm m ON m.id=r.msm_id WHERE r.kind='ping' AND r.ts>=? AND r.ts<=? "
                        "ORDER BY r.msm_id, r.ts", (s, e)).fetchall()
    for rs in _rounds([r for r in prow if r["target"] in mine]):
        sent = sum(r["sent"] or 0 for r in rs)
        rtts = [r["rtt_avg"] for r in rs if r["rtt_avg"] is not None]
        failed = [r["prb_id"] for r in rs if r["sent"] and not r["rcvd"]]
        out["atlas_rounds"].append({
            "ts": rs[0]["ts"], "target": rs[0]["target"], "label": label_of(rs[0]["target"]), "purpose": rs[0]["purpose"],
            "msm": rs[0]["msm_id"], "probes": len(rs), "no_reply": len(failed), "failed_probes": failed,
            "loss": 100 * (1 - sum(r["rcvd"] or 0 for r in rs) / sent) if sent else None,
            "median_rtt": statistics.median(rtts) if rtts else None})
    _judge_rounds(conn, out["atlas_rounds"], s)
    around.update(rounds=len(out["atlas_rounds"]), targets=len({r["target"] for r in out["atlas_rounds"]}),
                  odd=[r for r in out["atlas_rounds"] if r["odd"]])
    tmsms = conn.execute("SELECT DISTINCT m.id, m.target, m.purpose, m.created FROM atlas_msm m JOIN atlas_result r "
                         "ON r.msm_id=m.id WHERE m.kind='traceroute' AND (m.event_id=? OR (r.ts>=? AND r.ts<=?)) "
                         "ORDER BY m.created", (ev["id"], s, e)).fetchall()
    for m in tmsms[:6]:
        d = atlas_payload(conn, m["target"], s, e, m["id"], nbrs=nbrs)
        trs = d["traces"]
        if not trs:
            continue
        groups = {}
        for t in trs:
            k = (t["carrier"], t["up"])
            g = groups.setdefault(k, [0, 0])
            g[0] += 1
            g[1] += t["reached"] or 0
        stops = {}
        for t in trs:
            if not t["reached"]:
                stops[t["last_asn"]] = stops.get(t["last_asn"], 0) + 1
        failed = [t for t in trs if not t["reached"]][:5]
        asns.update(a for k in groups for a in k if a)
        asns.update(a for a in stops if a)
        for t in failed:
            asns.update(h["asn"] for h in t["hops"] if h.get("asn"))
        out["atlas_traces"].append({
            "msm": m["id"], "target": m["target"], "label": label_of(m["target"]), "purpose": m["purpose"],
            "ts": min(t["ts"] for t in trs), "total": len(trs), "reached": sum(1 for t in trs if t["reached"]),
            "groups": sorted(groups.items(), key=lambda x: -x[1][0]), "stops": sorted(stops.items(), key=lambda x: -x[1]),
            "failed": [{"prb": t["prb"], "probe": d["probes"].get(t["prb"]) or d["probes"].get(str(t["prb"])) or {},
                        "hops": t["hops"]} for t in failed]})

    # one chronological list of what happened
    tl = [(start, "event", f"{ev['severity'].upper()}: {ev['title']}")]
    if end:
        tl.append((end, "event", "event ended"))
    for r in out["related"]:
        if r["start_ts"] >= s:
            tl.append((r["start_ts"], "related", f"[{r['severity']}] {r['title']} (event {r['id']}"
                       + (", found during backfill)" if r["historical"] else ")")))
    out["already_open"] = [r for r in out["related"] if r["start_ts"] < s]
    out["related"] = [r for r in out["related"] if r["start_ts"] >= s]
    for r in out["atlas_rounds"]:
        bad = r["no_reply"] * 2 >= r["probes"]
        tl.append((r["ts"], "atlas-bad" if bad else "atlas",
                   f"RIPE Atlas ping {r['target']}{' (' + r['label'] + ')' if r['label'] else ''}: "
                   f"{r['probes'] - r['no_reply']}/{r['probes']} probes got replies"
                   + (f", median {r['median_rtt']:.0f} ms" if r["median_rtt"] else "")))
    for t in out["atlas_traces"]:
        tl.append((t["ts"], "atlas", f"RIPE Atlas traceroute ({t['purpose']}) to {t['target']}: {t['reached']}/{t['total']} reached"))
    for run in out["rv_runs"]:
        tl.append((run["ts"], "rv", f"Route Views snapshot ({run['label']}): " + ", ".join(
            f"{x['server'].replace('.routeviews.org', '')} {('error' if x['error'] else x['n_paths'])}" for x in run["snaps"])))
    for p in out["rv_probes"]:
        res = ("error" if p["error"] else f"{p['success_pct'] or 0:.0f}% replies" if p["kind"] == "ping"
               else "reached" if p["reached"]
               else f"target answers ping; path hidden after {p['last_hop'] or '?'}" if p.get("ping_ok")
               else f"stopped at {p['last_hop'] or '?'}")
        tl.append((p["ts"], "rv", f"Route Views {p['kind']} {p['target']} from {p['server']}: {res}"))
    out["names"] = name_of([a for a in asns if a])
    nm = lambda a: f"AS{a}" + (f" ({out['names'][a].split(' ')[0]})" if out["names"].get(a) else "")   # noqa: E731
    for sp in around["routes"]:
        sp["who"] = "; ".join(paths.mover_text(m, nm) for m in sp["movers"][:3])
        for b in sp["bursts"]:
            b["text"] = changes.burst_text(b, out["names"])
            b["parts"] = b["text"].split("; ") if b["text"] else []
            tl.append((b["start"], "bgp", f"BGP: {b['peers']} of {sp['peers']} RIS peers changed their path to {prefix}"
                       + (f": {b['text']}" if b["text"] else "")))
    if out["mix"]:
        who = "; ".join(paths.mover_text(m, nm) for m in out["mix"]["movers"][:3])
        out["mix"]["who"] = who
        for m in out["mix"]["moments"][1:2]:
            tl.append((m["ts"], "bgp", "BGP: biggest change in upstream mix: " + ", ".join(
                f"AS{u} {out['mix']['moments'][0]['mix'].get(u, 0)}→{m['mix'].get(u, 0)}" for u in out["mix"]["upstreams"])
                + (f"; behind it: {who}" if who else "")))
    out["timeline"] = sorted(tl, key=lambda x: x[0])
    return out


def _span_of(sec):
    """A length of time in words: '45 s', '12 min', '2 h 05 min', '3 d 4 h'."""
    sec = int(abs(sec))
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{round(sec / 60)} min"
    if sec < 2 * 86400:
        return f"{sec // 3600} h {sec % 3600 // 60:02d} min"
    return f"{sec // 86400} d {sec % 86400 // 3600} h"


def around_text(d):
    """The report's "around this event" part (collect()'s "around", "related" and "already_open"), as lines: what
    the routes did, the ping rounds that stood out, the other events, and what wasn't heard live."""
    ar = d["around"]
    hm = lambda ts: time.strftime("%H:%M:%S", time.gmtime(ts))   # noqa: E731
    L = ["", "AROUND THIS EVENT (the 30 minutes before and after it, UTC)", "-" * 72]
    w = L.append
    if d["prefix"]:
        w(f"Routes to {d['prefix']}, as the RIS peers saw them"
          + (" (this event names none of your prefixes)" if d["prefix_how"] == "busiest" else "") + ":")
        for sp in ar["routes"]:
            if sp["label"]:
                w(f"  {sp['label'].capitalize()} ({time.strftime('%Y-%m-%d', time.gmtime(sp['start']))}):")
            for b in sp["bursts"]:
                w(f"  {hm(b['start'])}" + (f"-{hm(b['end'])}" if b["end"] - b["start"] >= 1 else "")
                  + f"  {b['peers']} of {sp['peers']} RIS peers changed path" + (f": {b['text']}" if b["text"] else "")
                  + (f" ({b['back']} of them ended where they began)" if b["back"] else ""))
            plural = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"   # noqa: E731
            extra = [f"{plural(sp['more_bursts'], 'more burst')} of that size"] if sp["more_bursts"] else []
            if sp["small"]:
                extra.append(f"{plural(sp['small'], 'smaller change')}, a peer or two at a time")
            if not sp["bursts"] and not sp["small"]:
                w("  No RIS peer changed its path.")
                continue
            if extra:
                w("  " + ("Also " if sp["bursts"] else "") + "; ".join(extra) + ".")
            who = f" {sp['who'][:1].upper()}{sp['who'][1:]}." if sp["who"] else ""
            if sp["settled"]:
                w(f"  At {hm(sp['end'])} the upstreams and the networks behind them carried you as at {hm(sp['start'])}.")
            elif sp["changed"]:
                w(f"  At {hm(sp['end'])}, compared with {hm(sp['start'])}: "
                  + ", ".join(f"AS{u} {sp['before'].get(u, 0)} → {sp['after'].get(u, 0)} peers" for u in sp["changed"])
                  + "." + ("" if sp["bursts"] else who))
            else:
                w(f"  At {hm(sp['end'])} each upstream had as many peers as at {hm(sp['start'])}, but not by the same ways in."
                  + who)
            if sp["lowest"] is not None:
                w(f"  Fewest peers with a route: {sp['lowest']} of {sp['peers']}, at {hm(sp['lowest_ts'])}.")
        if not ar["routes"]:
            w("  Nothing is stored for this stretch.")
    if ar["rounds"]:
        w(f"RIPE Atlas ping rounds: {ar['rounds']} to {ar['targets']} of your addresses"
          + (":" if ar["odd"] else ", all as usual."))
        for r in ar["odd"]:
            tgt = r["target"] + (f" ({r['label']})" if r["label"] else "")
            w(f"  {hm(r['ts'])}  {tgt}: "
              + (f"{r['probes'] - r['no_reply']} of {r['probes']} probes got a reply (usually {r['probes'] - r['usual_no_reply']})"
                 if r["odd"] == "loss" else f"median round trip {r['median_rtt']:.0f} ms (usually {r['usual_rtt']:.0f})"))
        if ar["odd"] and ar["rounds"] > len(ar["odd"]):
            w(f"  The other {ar['rounds'] - len(ar['odd'])} were as usual.")
    else:
        w("RIPE Atlas ping rounds: none in this window.")
    w("Other events:" if d["related"] else "Other events: none began in this window.")
    for r in d["related"]:
        when = ("at the same time" if abs(r["after"]) < 60
                else f"{_span_of(r['after'])} {'after' if r['after'] > 0 else 'before'} this one began")
        w(f"  - {when}: [{r['severity']}] {r['title']} (event {r['id']}"
          + (", found during backfill" if r["historical"] else "") + ")")
    if d["already_open"]:
        w("Already open when this one began:")
        for r in d["already_open"]:
            w(f"  - [{r['severity']}] {r['title']} (since {_when(r['start_ts'])})")
    for a, b in ar["unheard"]["open"]:
        w(f"Not heard: RIS Live wasn't being received from {hm(a)} to {hm(b)}, and RIPE's archive hasn't filled that "
          "stretch in yet: routes may have changed then without showing here.")
    f = ar["unheard"]["filled"]
    if f:
        w(f"Filled in afterwards: {f['n']} of the route updates here ({hm(f['start'])} to {hm(f['end'])}) come from RIPE's "
          "archive, not the live feed: bgpcanary wasn't hearing RIS Live then, or wasn't running yet.")
    return L


def as_text(cfg, ev, d, link=""):
    """Plain-text evidence report (to attach to a ticket with a provider)."""
    nm = lambda a: f"AS{a}" + (f" ({d['names'].get(a, '').split(' ')[0]})" if d["names"].get(a) else "") if a else "unknown"
    L = []
    w = L.append
    w(f"BGP / reachability evidence report - AS{cfg.asn}")
    w("=" * 72)
    w(f"Event:     {ev['title']}")
    w(f"Type:      {ev['kind']} ({ev['severity']})")
    w(f"Prefix:    {ev['prefix'] or '-'}")
    w(f"Started:   {_when(ev['start_ts'])}")
    w(f"Ended:     {_when(ev['end_ts']) if ev['end_ts'] else 'ongoing at time of report'}")
    if ev["end_ts"]:
        w(f"Duration:  {int(ev['end_ts'] - ev['start_ts'])} s")
    w(f"Report:    generated {_when(time.time())} by bgpcanary" + (f" - {link}" if link else ""))
    w("Sources:   RIPE RIS (BGP from ~330 peers), Route Views collectors, RIPE Atlas probes (ping/traceroute)")
    det = json.loads(ev["detail"] or "{}")
    if det.get("note"):
        w("")
        w(det["note"])

    L.extend(around_text(d))
    w("")
    w("TIMELINE (UTC)")
    w("-" * 72)
    for ts, _k, text in d["timeline"]:
        w(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts))}  {text}")

    if d["mix"]:
        w("")
        w("BGP: RIS PEERS PER UPSTREAM")
        w("-" * 72)
        ms = d["mix"]["moments"]
        w(f"{'Upstream':34s}" + "".join(f"{m['label'][:22]:>24s}" for m in ms))
        w(f"{'':34s}" + "".join(f"{time.strftime('%H:%M:%S UTC', time.gmtime(m['ts'])):>24s}" for m in ms))
        for u in d["mix"]["upstreams"]:
            w(f"{nm(u):34s}" + "".join(f"{m['mix'].get(u, 0):>24d}" for m in ms))
        w(f"{'Total RIS peers seeing the prefix':34s}" + "".join(f"{m['visible']:>24d}" for m in ms))
        if d["mix"].get("who"):
            w("")
            w(f"Behind it (between the first two moments): {d['mix']['who']}.")
        for snap in d["paths"]:
            w("")
            w(f"AS paths seen by RIS peers {snap['label']} ({_when(snap['ts'])}), prepends collapsed "
              "(see the Route Views output below for exact paths):")
            for g in snap["groups"]:
                w(f"  via {nm(g['upstream'])}: {g['peers']} peers")
                for p, n in g["paths"]:
                    w(f"    {n:4d} x  {p}")
                if g["more"]:
                    w(f"           (+{g['more']} more distinct paths)")

    if d["rv_runs"]:
        w("")
        w("ROUTE VIEWS: show ip bgp (raw output)")
        w("-" * 72)
        for run in d["rv_runs"]:
            w(f"[{_when(run['ts'])}] {run['label']}")
            for x in run["snaps"]:
                w(f"  {x['server']} ({x['prefix']}): " + (f"ERROR {x['error']}" if x["error"] else f"{x['n_paths']} paths"))
                for line in (x["raw"] or "").strip().splitlines():
                    w(f"    {line.strip()}")
    if d["rv_probes"]:
        w("")
        w("ROUTE VIEWS: ping / traceroute")
        w("-" * 72)
        for p in d["rv_probes"]:
            w(f"[{_when(p['ts'])}] {p['kind']} {p['target']} from {p['server']} ({p['trigger']})" +
              (f" - ERROR {p['error']}" if p["error"] else ""))
            for line in (p["output"] or "").strip().splitlines():
                w(f"    {line.rstrip()}")

    if d["atlas_rounds"] or d["atlas_traces"]:
        w("")
        w("RIPE ATLAS")
        w("-" * 72)
        for r in d["atlas_rounds"]:
            w(f"[{_when(r['ts'])}] ping {r['target']}{' (' + r['label'] + ')' if r['label'] else ''}, measurement "
              f"{r['msm']}: {r['probes'] - r['no_reply']}/{r['probes']} probes got replies"
              + (f", packet loss {r['loss']:.0f}%" if r["loss"] is not None else "")
              + (f", median RTT {r['median_rtt']:.1f} ms" if r["median_rtt"] else ""))
        for t in d["atlas_traces"]:
            w("")
            w(f"[{_when(t['ts'])}] traceroute to {t['target']} ({t['purpose']}), measurement {t['msm']} "
              f"(https://atlas.ripe.net/measurements/{t['msm']}/): {t['reached']}/{t['total']} probes reached the target")
            for (car, up), (n, ok) in t["groups"]:
                w(f"    via {nm(car)} -> {nm(up)}: {ok}/{n} reached")
            if t["stops"]:
                w("  Failed traceroutes stopped in: " + ", ".join(f"{nm(a)} ({n})" for a, n in t["stops"]))
            for f in t["failed"]:
                p = f["probe"]
                w(f"  Probe {f['prb']} ({p.get('cc') or '?'}, {nm(p.get('asn'))}), hop by hop:")
                for h in f["hops"]:
                    w(f"    {h['h']:>3}  {h['ip'] or '*':40s} {nm(h['asn']) if h.get('asn') else '':28s} "
                      f"{('%.1f ms' % h['rtt']) if h.get('rtt') is not None else ''}")
    w("")
    w("All times UTC. RIS = RIPE NCC Routing Information Service; RIPE Atlas results are public and can be "
      "verified at the measurement links above.")
    return "\n".join(L) + "\n"
