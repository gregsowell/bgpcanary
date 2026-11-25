"""An event's evidence as a PDF, to send to a provider: "Download all evidence (PDF)" on the event page.

It holds what the event page shows, in about the page's order: the event and the independent checks of it, what
else went on around it, the path tree before the event, at its peak and where it stands now, the timeline, who
carried the prefix and the AS paths at those three moments, Route Views' output, RIPE Atlas's results and the
charts, with the raw BGP updates as an appendix. It's built from evidence.collect's answer, as the plain-text report
is (evidence.as_text), so the two say the same. All times are UTC.

Left out: the alerts sent and the neighbour panel ("what is it to you?"). They're about your own setup, not the
event, and an alert channel's error can name a webhook's address."""
import json
import re
import time

from . import __version__, evidence, pdf, routeviews

TL_SOURCE = {"event": "event", "related": "event", "atlas": "RIPE Atlas", "atlas-bad": "RIPE Atlas",
             "rv": "Route Views", "bgp": "BGP"}
# An event open for weeks has weeks of evidence: thousands of ping rounds and scheduled probes, which its page lists in
# full. The PDF keeps the first and the last (first, last) of each such list, its start and its end, and says how many
# it left out: an event of a few hours has fewer, and the PDF has them all.
CAP_TIMELINE, CAP_ROUNDS, CAP_PROBES, CAP_AROUND = (150, 100), (60, 40), (8, 4), (12, 6)
# Route Views' "show ip bgp" runs: the routine one before the event, the first during it and the latest (before,
# during, after, as with the tree)
CAP_RV_RUNS = (2, 1)
FROM_RE = re.compile(r"^\s+(\S+) from (\S+)")   # a path's next hop: "    192.0.2.9 from 192.0.2.9 (198.51.100.1)"


def _rv_paths(raw):
    """A collector's answer to "show ip bgp <prefix>", one line a path: [(AS path as it has it, prepends and all;
    the next hop it heard it from; whether it's the best)]. Its whole answer (seven lines or so a path: origin,
    communities, timers) is on the event page and in the text report."""
    out = []
    for line in (raw or "").splitlines():
        m = routeviews.PATH_RE.match(line)
        if m:
            out.append([m.group(1).strip(), "", False])
        elif out:
            f = FROM_RE.match(line)
            if f and not out[-1][1]:
                out[-1][1] = f.group(1)
            elif re.search(r",\s*best\b", line):
                out[-1][2] = True
    return out


TREE_NOTES = {
    "before": "The steady state: the healthiest moment in the three hours before the event (as the alert emails "
              "picture it), which the other pictures are coloured against.",
    "peak": "When the routes had changed most: the \"during\" moment of the table below.",
    "after": "Where the routes settled after the event ended.",
    "now": "Where the routes stand now: the event is still open."}


def tree_moments(conn, cfg, ev, d):
    """The moments the PDF pictures the tree at, [(key, ts, label)]: before the event (its healthiest moment in the
    three hours before, as the alert emails have it: the steady state, where the moment just before can already be
    sliding), at its peak and after it or now (those of the "who carried" table and the AS path lists). Each is a
    bucket's end: a bucket's mix is the one at its end. [] when no bucket covers the event."""
    if not d["mix"]:
        return []
    B, start, ms = cfg.bucket_seconds, ev["start_ts"], d["mix"]["moments"]
    r = conn.execute("SELECT ts FROM buckets WHERE prefix=? AND ts BETWEEN ? AND ? ORDER BY visible DESC, ts DESC "
                     "LIMIT 1", (d["prefix"], start - 3 * 3600, start - B)).fetchone()
    out = [("before", (r["ts"] if r else ms[0]["ts"]) + B, "Before the event")]
    for key, m, label in (("peak", ms[1], "At the peak"),
                          ("after", ms[2], "After the event") if ev["end_ts"] else ("now", ms[2], "Now")):
        if all(m["ts"] + B != t for _k, t, _l in out):   # (no route change at all: the peak is where it began)
            out.append((key, m["ts"] + B, label))
    return out


def _utc(ts, fmt="%Y-%m-%d %H:%M:%S"):
    return time.strftime(fmt, time.gmtime(ts)) + " UTC"


def _cap(s):
    return s[:1].upper() + s[1:]


def build(cfg, ev, d, pictures=(), charts=(), raw=(), link="", kind_name="", now=None):
    """The PDF, as bytes. `ev`: the event's row; `d`: evidence.collect's answer; `pictures`: tree.pictures' answer
    for the moments the tree is shown at; `charts`: [(title, png)] (charts.all_charts); `raw`: evidence.raw_updates'
    answer; `link`: the event page's address; `kind_name`: the event type's name."""
    ev = dict(ev)
    now = now or time.time()
    det = json.loads(ev.get("detail") or "{}")
    names = d["names"]

    def nm(a):
        return (f"AS{a}" + (f" ({names[a].split(' ')[0]})" if names.get(a) else "")) if a else "unknown"

    doc = pdf.Doc(title=f"Event {ev['id']}: {ev['title']}", author=f"AS{cfg.asn}",
                  subject="BGP / reachability evidence report", creator=f"bgpcanary {__version__}",
                  footer=f"AS{cfg.asn} · event {ev['id']} · evidence report generated {_utc(now, '%Y-%m-%d %H:%M')}")
    _summary(doc, cfg, ev, det, d, now, link, kind_name)
    _around(doc, d)
    _trees(doc, pictures)
    _timeline(doc, d)
    _bgp(doc, d, nm)
    _route_views(doc, d)
    _atlas(doc, d, nm)
    _charts(doc, charts)
    doc.space(10)
    doc.para("All times UTC. RIS: the RIPE NCC's Routing Information Service, whose route collectors peer with "
             "networks around the world. RIPE Atlas results are public: each measurement can be checked at the address "
             "given with it.", size=8, color="muted")
    _raw(doc, raw, ev["prefix"] or d["prefix"])
    return doc.render()


def _summary(doc, cfg, ev, det, d, now, link, kind_name):
    doc.para(f"BGP / reachability evidence report · AS{cfg.asn}", size=9, font=pdf.BOLD, color="muted", after=2)
    doc.heading(ev["title"], 0)
    sev, end = ev["severity"], ev["end_ts"]
    rows = [("Event", f"{kind_name or ev['kind']} ({ev['kind']}), event {ev['id']}"),
            ("Severity", sev.upper(), {"critical": "crit", "warning": "warn"}.get(sev, "text")),
            ("Prefix", ev["prefix"] or (f"none (the routes shown are those of {d['prefix']})" if d["prefix"] else "none"))]
    rows += [("Started", _utc(ev["start_ts"])), ("Ended", _utc(end) if end else f"still ongoing at {_utc(now)}"),
             ("Duration", evidence._span_of(max(end or now, ev["start_ts"]) - ev["start_ts"]) + ("" if end else " so far"))]
    if ev["min_visible"] is not None:
        rows.append(("Lowest visibility", f"{ev['min_visible']}" + (
            f" of {ev['baseline']:.0f} RIS peers ({100 * ev['min_visible'] / ev['baseline']:.0f}%)" if ev["baseline"]
            else " RIS peers")))
    if ev["historical"]:
        rows.append(("Found", "afterwards, in RIPE's archive (bgpcanary wasn't hearing RIS live then)"))
    if det.get("example_path"):
        rows.append(("Example path", " ".join(map(str, det["example_path"]))))
    if det.get("path"):
        rows.append(("Path", " ".join(map(str, det["path"]))))
    if det.get("peers"):
        rows.append(("Peers", det["peers"]))
    if det.get("pathfinder"):
        rows += [(f"Path {det.get('direction') or ''}".strip(), " -> ".join(f"AS{a}" for a in det.get("now") or [])),
                 ("Before", " -> ".join(f"AS{a}" for a in det.get("was") or [])), ("Path finder target", det["pathfinder"])]
    if det.get("reference_prefixes"):
        rows.append(("Compared with", ", ".join(det["reference_prefixes"]) + f" (AS{det.get('upstream')}'s own prefixes)"))
    rows += [("Report", f"generated {_utc(now)} by bgpcanary {__version__}" + (f" · {link}" if link else "")),
             ("Sources", "RIPE RIS (BGP as its route collectors' peers see it), Route Views collectors, RIPE Atlas "
                         "probes (ping and traceroute)")]
    doc.kv(rows)
    if det.get("note"):
        doc.para(det["note"])
    for key, label in (("object", "Registry object (now)" if det.get("before") else "Registry object"),
                       ("before", "Registry object (before)")):
        if det.get(key):
            doc.para(label, font=pdf.BOLD, size=8.5, after=1)
            doc.pre(det[key])
    cf = det.get("confirm")   # (Service._confirm_outage's verdict and what it rests on)
    if isinstance(cf, dict) and cf:
        doc.heading("Checked against independent sources", 1)
        verdict = {"contradicted": "not confirmed: probably RIPE's data", "confirmed": "confirmed"}.get(
            cf.get("verdict"), "couldn't tell")
        doc.para(f"Verdict: {verdict}. " + (_cap(cf["why"]) + ". " if cf.get("why") else "")
                 + (f"Checked {_utc(cf['at'])}." if cf.get("at") else ""))
        rv = (cf.get("route_views") or {}).get("servers")
        if rv:
            doc.table([("Route Views collector", None, "l"), (f"Paths from AS{cfg.asn} then", 120, "r"), ("Usually", 70, "r")],
                      [[s["server"], f"no answer: {s['error']}" if s.get("error") else s.get("paths"),
                        "" if s.get("error") else ("-" if s.get("usual") is None else s["usual"])] for s in rv])
        if cf.get("atlas"):
            a = cf["atlas"]
            doc.para(f"RIPE Atlas: {a['replied']} of {a['probes']} probes got a reply from {a['target']}"
                     + (f" ({_utc(a['ts'])})." if a.get("ts") else "."))
        if cf.get("controls"):
            doc.para("Control prefixes in RIS then: " + " · ".join(
                f"{c['prefix']} {c['visible']}" + (f" of ~{c['baseline']:.0f}" if c.get("baseline") else "") + " peers"
                + (" (lost)" if c.get("down") else "") for c in cf["controls"]), color="muted")


def _around(doc, d):
    doc.heading("Around this event", 1)
    s, e = d["window"]
    doc.para(f"What else bgpcanary saw from {_utc(s, '%Y-%m-%d %H:%M')} to {_utc(e, '%Y-%m-%d %H:%M')}, 30 minutes "
             "either side of the event, whether or not it raised an event of its own.", size=8.5, color="muted")
    for ln in evidence.around_text(d, CAP_AROUND)[3:]:   # (after the text report's own heading)
        if not ln.strip():
            continue
        if ln.startswith("  - "):
            doc.para(ln[4:], indent=8, bullet=True, after=1.5)
        elif ln.startswith("  "):
            doc.para(ln.strip(), indent=12, after=1.5)
        else:
            doc.para(ln, font=pdf.BOLD if ln.endswith(":") else pdf.REGULAR, after=2)


def _trees(doc, pictures):
    if not pictures:
        return
    doc.heading("Path tree", 1)
    doc.para("The tree starts at your AS and follows each RIS peer's AS path outward: your upstreams, the networks "
             "behind them, then the next hop out. A line's thickness is the number of RIS peers using that branch. "
             "Colours compare each moment with the first: orange lost more than half its peers, red dashed dropped out "
             "completely, green is new, and a red dotted line is a neighbour of an upstream that RIS sees carrying your "
             "routes nowhere. The event page replays every change in between.", size=8.5, color="muted")
    for cid, png, caption in pictures:
        note = TREE_NOTES.get(cid.removeprefix("tree-"))
        doc.image(png, caption=f"{caption}. {note}" if note else caption)


def _timeline(doc, d):
    doc.heading("Timeline (UTC)", 1)
    if not d["timeline"]:
        doc.para("Nothing recorded in this window.", color="muted")
        return
    def row(x):
        ts, k, text = x
        cells = [time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)), TL_SOURCE.get(k, k), text]
        bg = "crit_bg" if k == "atlas-bad" else "info_bg" if k == "event" else None
        return {"cells": cells, "bg": bg} if bg else cells
    head, tail, n = evidence.cut(d["timeline"], CAP_TIMELINE)
    doc.table([("Time", 86, "l"), ("Source", 58, "l"), ("What happened", None, "l")],
              [row(x) for x in head] + _left_out(n, head, tail, 3, 2) + [row(x) for x in tail])


def _gap(n, head, tail, ts, what="more"):
    """What stands in for the `n` items a list left out between `head` and `tail` (evidence.cut); `ts` gives an
    item's time."""
    where = (f"between {_utc(ts(head[-1]))} and {_utc(ts(tail[0]))}" if head and tail
             else f"after {_utc(ts(head[-1]))}" if head else f"before {_utc(ts(tail[0]))}" if tail else "")
    return f"... {n} {what} {where}: the event page lists them all"


def _left_out(n, head, tail, ncols, at, ts=lambda x: x[0]):
    """The table row standing in for the `n` rows left out between `head` and `tail`, its text in column `at`."""
    if not n:
        return []
    cells = [""] * ncols
    cells[at] = {"text": _gap(n, head, tail, ts), "color": "muted"}
    return [{"cells": cells, "bg": "band"}]


def _bgp(doc, d, nm):
    mix = d["mix"]
    if not mix:
        return
    doc.heading(f"BGP: who carried {d['prefix']}", 1)
    doc.para("RIS peers reaching the prefix through each upstream: just before the event, at the moment the mix changed "
             "most, and after it (or now, while it's still open).", size=8.5, color="muted")
    ms = mix["moments"]
    rows = []
    for u in mix["upstreams"]:
        b = ms[0]["mix"].get(u, 0)
        cells = [nm(u)]
        for i, m in enumerate(ms):
            v = m["mix"].get(u, 0)
            col = "crit" if i and b and v < b * 0.5 else "ok" if i and v > b * 1.5 + 5 else "text"
            cells.append({"text": str(v), "color": col, "font": pdf.REGULAR if col == "text" else pdf.BOLD})
        rows.append(cells)
    rows.append([{"text": "Total RIS peers seeing the prefix", "color": "muted"}]
                + [{"text": str(m["visible"]), "color": "muted"} for m in ms])
    doc.table([("Upstream", None, "l")] + [(f"{_cap(m['label'])}\n{_utc(m['ts'], '%m-%d %H:%M:%S')}", 96, "r")
                                           for m in ms], rows, size=8.5)
    if mix.get("who"):
        doc.para("Behind it, between the first two moments (the networks behind your upstreams whose way to you "
                 f"changed): {mix['who']}.")
    for snap in d["paths"]:
        doc.heading(f"AS paths {snap['label']} · {_utc(snap['ts'])}", 2)
        doc.para("As the RIS peers saw them, prepends collapsed (the Route Views output further down has exact paths).",
                 size=8, color="muted", after=2)
        if not snap["groups"]:
            doc.para("No paths recorded at this moment.", color="muted")
        for g in snap["groups"]:
            doc.para(f"via {nm(g['upstream'])} · {g['peers']} peers", font=pdf.BOLD, size=8.5, after=1)
            doc.pre("\n".join(f"{n:4d} ×  {p}" for p, n in g["paths"])
                    + (f"\n        (+{g['more']} more distinct paths)" if g["more"] else ""))


def _probe_result(p):
    if p["error"]:
        return f"error: {p['error']}"
    if p["kind"] == "ping":
        return f"{p['success_pct'] or 0:.0f}% replies" + (f", {p['rtt_avg']:.0f} ms" if p["rtt_avg"] else "")
    if p["reached"]:
        return "reached"
    if p.get("ping_ok"):
        return f"target answers ping; path hidden after {p['last_hop'] or '?'}"
    return f"stopped at {p['last_hop'] or '?'}"


def _route_views(doc, d):
    doc.heading("Route Views", 1)
    if not d["rv_runs"]:
        doc.para("No Route Views snapshots in this window.", color="muted")
    else:
        doc.para("Each collector's answer to \"show ip bgp\" for the prefix, one line a path: the AS path as Route Views "
                 "has it (prepends kept) and, where the answer gives them, the next hop it came from and the one it chose "
                 "as best. The event page and its text report have the whole answers, with origin, communities and "
                 "timers.", size=8.5, color="muted")
    head, tail, n = evidence.cut(d["rv_runs"], CAP_RV_RUNS)
    for run in head + ([None] if n else []) + tail:
        if run is None:
            doc.para(_gap(n, head, tail, lambda r: r["ts"], "more runs") + ".", color="muted")
            continue
        doc.heading(f"show ip bgp · {run['label']} · {_utc(run['ts'])}", 2)
        for x in run["snaps"]:
            doc.para(f"{x['server']} ({x['prefix']}): " + (f"error: {x['error']}" if x["error"] else f"{x['n_paths'] or 0} paths"),
                     font=pdf.BOLD, size=8.5, after=1, color="crit" if x["error"] else "text")
            ps = _rv_paths(x["raw"])
            if ps:   # (older answers were stored as their AS paths alone: no next hop, no best)
                w, hw = max(len(p) for p, _h, _b in ps), max(len(h or "-") for _p, h, _b in ps)
                hops = any(h for _p, h, _b in ps)
                doc.pre("\n".join(f"{p:{w}}" + (f"  from {h or '-':{hw}}" if hops else "") + ("  best" if b else "")
                                  for p, h, b in ps))
            elif (x["raw"] or "").strip():   # (no path in it: "% Network not in table" and the like, as it came)
                doc.pre("\n".join(ln.strip() for ln in x["raw"].strip().splitlines()[:20]))
    if d["rv_probes"]:
        doc.heading("Ping and traceroute from Route Views", 2)
        head, tail, n = evidence.cut(d["rv_probes"], CAP_PROBES)
        for p in head + ([None] if n else []) + tail:
            if p is None:
                doc.para(_gap(n, head, tail, lambda r: r["ts"]) + ".", color="muted")
                continue
            doc.para(f"{_utc(p['ts'])} · {p['kind']} {p['target']} from {p['server']}: {_probe_result(p)} ({p['trigger']})",
                     font=pdf.BOLD, size=8.5, after=1)
            if (p["output"] or "").strip():
                doc.pre(p["output"].strip())


def _atlas(doc, d, nm):
    doc.heading("RIPE Atlas", 1)
    if d["atlas_rounds"]:
        doc.heading("Ping rounds to your addresses", 2)
        def row(r):
            cells = [time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"])),
                     r["target"] + (f" ({r['label']})" if r["label"] else ""), f"{r['probes'] - r['no_reply']} / {r['probes']}",
                     f"{r['loss']:.0f}%" if r["loss"] is not None else "-", f"{r['median_rtt']:.0f} ms" if r["median_rtt"] else "-",
                     f"{r['msm']} ({r['purpose']})"]
            return {"cells": cells, "bg": "crit_bg"} if r["no_reply"] * 2 >= r["probes"] else cells
        head, tail, n = evidence.cut(d["atlas_rounds"], CAP_ROUNDS)
        doc.table([("Time (UTC)", 86, "l"), ("Target", None, "l"), ("Probes replying", 64, "r"), ("Packet loss", 50, "r"),
                   ("Median RTT", 50, "r"), ("Measurement", 108, "l")],
                  [row(r) for r in head] + _left_out(n, head, tail, 6, 1, lambda r: r["ts"]) + [row(r) for r in tail])
        doc.para("A measurement's results: https://atlas.ripe.net/measurements/<number>/", size=8, color="muted")
    for t in d["atlas_traces"]:
        url = f"https://atlas.ripe.net/measurements/{t['msm']}/"
        doc.heading(f"Traceroute ({t['purpose']}) to {t['target']}" + (f" ({t['label']})" if t["label"] else "")
                    + f" · {_utc(t['ts'])}", 2)
        doc.para(f"{t['reached']} of {t['total']} probes reached the target. Measurement {t['msm']}: {url}", link=url,
                 color="crit" if t["reached"] < t["total"] * 0.9 else "text")
        doc.table([("Carrier -> upstream", None, "l"), ("Reached", 80, "r")],
                  [[f"{nm(k[0])} -> {nm(k[1])}", {"text": f"{v[1]} / {v[0]}", "color": "crit" if v[1] < v[0] else "text"}]
                   for k, v in t["groups"]])
        if t["stops"]:
            doc.table([("Failed traceroutes stopped in", None, "l"), ("Probes", 80, "r")], [[nm(a), n] for a, n in t["stops"]])
        for f in t["failed"]:
            p = f["probe"]
            doc.para(f"Probe {f['prb']} ({p.get('cc') or '?'}, {nm(p.get('asn'))}), hop by hop:", font=pdf.BOLD, size=8.5, after=1)
            doc.pre("\n".join(f"{h['h']:>3}  {h['ip'] or '*':40s} {('AS' + str(h['asn'])) if h.get('asn') else '':12s} "
                              + (f"{h['rtt']:.1f} ms" if h.get("rtt") is not None else "") for h in f["hops"]))
    if not d["atlas_rounds"] and not d["atlas_traces"]:
        doc.para("No RIPE Atlas results in this window.", color="muted")


def _charts(doc, charts):
    if not charts:
        return
    doc.heading("Visibility around the event", 1)
    doc.para("Shaded: the events in this stretch (red critical, orange warning, blue a brief dip).", size=8.5, color="muted")
    for _title, png in charts:
        doc.image(png)


def _raw(doc, raw, prefix):
    if not raw or not any(st["updates"] for st in raw):
        return
    doc.new_page()
    doc.heading(f"Appendix: raw BGP updates for {prefix}", 1)
    doc.para(f"Every BGP update RIS reported for {prefix} in the stretches this report covers, oldest first: A an "
             "announcement, W a withdrawal. Bookkeeping rows (looking-glass corrections, seed rows after a restart, "
             "stale withdrawals) are left out.", size=8.5, color="muted")
    for st in raw:
        label = _cap(st["label"]) + ": " if st["label"] else ""
        doc.heading(f"{label}{_utc(st['start'], '%Y-%m-%d %H:%M:%S')} to {_utc(st['end'], '%H:%M:%S')} · "
                    f"{st['updates']} update{'' if st['updates'] == 1 else 's'}", 2)
        rows = [r for r in st["rows"] if not r["book"]]
        if st["updates"] > st["shown"]:
            doc.para(f"The list stops at the first {st['shown']} of the {st['updates']} updates in this stretch.",
                     size=8, color="warn")
        if not rows:
            doc.para("No updates are stored for this stretch.", color="muted")
            continue
        # one line an update, as a log: about 80 to a page (a table took half again as many pages)
        doc.pre("\n".join([f"{'Time':8}  {'Coll.':6}  {'Peer':25} {'Peer AS':11} {'':1}  {'Source':10} AS path"] + [
            f"{time.strftime('%H:%M:%S', time.gmtime(r['ts']))}  {r['collector'][:6]:6}  {r['peer']:25} "
            f"{'AS' + str(r['peer_asn']) if r['peer_asn'] else '':11} {r['kind']:1}  {r['source'][:10]:10} {r['path'] or ''}"
            for r in rows]), size=6.6)
