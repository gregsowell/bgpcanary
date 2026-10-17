"""Everything known about an event in one place: BGP (upstream mix + raw AS paths), Route Views output and
RIPE Atlas results, for the event page and a plain-text evidence report to send to a provider."""
import ipaddress
import json
import statistics
import time

from . import db, paths

PAD = 1800   # look this far before the start and after the end


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
    """Peers per upstream before the event, at its most-changed bucket, and after it ended."""
    rows = db.fill_mix(conn, conn.execute("SELECT ts, visible, upstreams FROM buckets WHERE prefix=? AND ts>=? AND ts<=? "
                                          "ORDER BY ts", (prefix, s, e)), prefix)
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
    return {"moments": [{"label": l, "ts": r["ts"], "visible": r["visible"], "mix": mix(r)} for l, r in moments],
            "upstreams": ups}


def _paths_at(conn, prefix, ts, top=4):
    """Distinct AS paths seen by RIS peers at `ts`, grouped by upstream (bgpcanary stores RIS paths with
    prepends collapsed; Route Views' raw output keeps them)."""
    by = {}
    for r in db.state_at(conn, prefix, ts):
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


def collect(conn, cfg, ev, atlas_payload, nbrs, label_of, name_of):
    """Evidence for one event. `atlas_payload(conn, target, s, e, msm, nbrs=)` builds traceroute views;
    `label_of(target)` gives a baseline target's label; `name_of(asns)` returns {asn: name}."""
    ev = dict(ev)
    start = ev["start_ts"]
    end = ev["end_ts"]
    now = time.time()
    s, e = start - PAD, min((end or now) + PAD, now)
    prefix = ev["prefix"] if ev["prefix"] in cfg.prefixes else None
    if not prefix and ev["prefix"]:   # a more-specific / covering event: use the monitored prefix it overlaps
        try:
            net = ipaddress.ip_network(ev["prefix"], strict=False)
            prefix = next((p for p in cfg.prefixes if ipaddress.ip_network(p).overlaps(net)), None)
        except ValueError:
            pass
    out = {"window": (s, e), "prefix": prefix, "related": [], "mix": None, "paths": [], "rv_runs": [],
           "rv_probes": [], "atlas_rounds": [], "atlas_traces": [], "timeline": [], "names": {}}
    asns = set()

    out["related"] = [dict(r) for r in conn.execute(
        "SELECT id, kind, severity, title, start_ts, end_ts FROM events WHERE id!=? AND historical=0 AND "
        "start_ts<=? AND COALESCE(end_ts, ?)>=? AND kind NOT IN ('chain_change') ORDER BY start_ts",
        (ev["id"], e, now, s))]

    if prefix:
        out["mix"] = _upstream_mix(conn, prefix, ev, s, e)
        if out["mix"]:
            asns.update(out["mix"]["upstreams"])
            for m in out["mix"]["moments"]:
                ps = _paths_at(conn, prefix, m["ts"])
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
            tl.append((r["start_ts"], "related", f"[{r['severity']}] {r['title']} (event {r['id']})"))
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
    if out["mix"]:
        for m in out["mix"]["moments"][1:2]:
            tl.append((m["ts"], "bgp", "BGP: biggest change in upstream mix: " + ", ".join(
                f"AS{u} {out['mix']['moments'][0]['mix'].get(u, 0)}→{m['mix'].get(u, 0)}" for u in out["mix"]["upstreams"])))
    out["timeline"] = sorted(tl, key=lambda x: x[0])
    out["names"] = name_of([a for a in asns if a])
    return out


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

    if d.get("already_open"):
        w("")
        w("Already open when this started:")
        for r in d["already_open"]:
            w(f"  - [{r['severity']}] {r['title']} (since {_when(r['start_ts'])})")
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
