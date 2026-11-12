"""The event page's "around this event": what the routes, the ping rounds and the other events did in the half hour
either side of an event, whether or not that raised an event of its own; the tree and the charts for an event that
names no prefix; and the events the strip under a tree shades.

The scene: a carrier's link address (203.0.113.9, in none of the monitored prefixes) stops answering RIPE Atlas
probes, which raises a reachability-loss event without a prefix. In the same seconds a transit behind one upstream
drops out: 20 of the 40 RIS peers move to another transit behind the same upstream and 6 to the other upstream.
That's too few between upstreams for an upstream-shift event, so nothing else about it was recorded."""
import asyncio
import json
import time

import pytest

from conftest import P1, P2

B = 30
T0 = int((time.time() - 10 * 86400) // B * B)   # ten days back: clear of the fixtures' own events
LINK, LOOPBACK = "203.0.113.9", "192.0.2.1"
WAS, NOW_SAME_UPSTREAM = "65010 64510 64496 64500", "65010 64512 64496 64500"   # two ways in behind AS64496
NOW_OTHER_UPSTREAM, UNTOUCHED = "65010 64511 64497 64500", "65011 64511 64497 64500"   # ...and behind AS64497
BEFORE = ({"65010 64510 64496": 30, "65011 64511 64497": 10}, {"64496": 30, "64497": 10})
AFTER = ({"65010 64510 64496": 4, "65010 64512 64496": 20, "65010 64511 64497": 6, "65011 64511 64497": 10},
         {"64496": 24, "64497": 16})
QUIET = T0 + 7200      # an event with nothing going on around it
ELSEWHERE = T0 + 6 * 3600   # ...and a stretch where the other prefix is the busy one


def bucket(prefix, ts, mix, updates=0, low=40):
    return (prefix, ts, 40, 40.0, low, json.dumps(mix[0]), json.dumps(mix[1]), updates, 0)


def ping_round(msm, ts, silent=(1,), rtt=40.0):
    """Ten probes' results of one round: the ones in `silent` got nothing back."""
    return [(msm, p, ts + p, "ping", 3, 0 if p in silent else 3, None if p in silent else rtt, 1 if p not in silent else 0)
            for p in range(1, 11)]


@pytest.fixture(scope="module")
def scene(web):
    conn = web.svc.aconn
    peers = [("rrc90", f"198.18.0.{i}", 65010 if i < 30 else 65011) for i in range(40)]
    ups = [(T0 - 3600, "bgplay-init", *p, P1, "A", WAS if i < 30 else UNTOUCHED) for i, p in enumerate(peers)]
    ups += [(T0 + 1 + i * 0.5, "ris-live", *peers[i], P1, "A", NOW_SAME_UPSTREAM) for i in range(1, 21)]
    ups += [(T0 + 12 + i - 21, "ris-live", *peers[i], P1, "A", NOW_OTHER_UPSTREAM) for i in range(21, 27)]
    ups += [(T0 + 2, "ris-live", *peers[0], P1, "A", NOW_SAME_UPSTREAM), (T0 + 20, "ris-live", *peers[0], P1, "A", WAS)]
    conn.executemany("INSERT INTO updates(ts, source, collector, peer, peer_asn, prefix, kind, path) VALUES(?,?,?,?,?,?,?,?)",
                     ups)
    conn.executemany(
        "INSERT INTO buckets(prefix, ts, visible, baseline, min_visible, chains, upstreams, updates, withdrawals) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [bucket(P1, T0 - 1830, BEFORE), bucket(P1, T0 - 30, BEFORE), bucket(P1, T0, AFTER, updates=28),
         bucket(P1, T0 + 1800, AFTER), bucket(P1, QUIET - 1800, AFTER), bucket(P1, QUIET + 1800, AFTER),
         bucket(P1, ELSEWHERE, AFTER, updates=2), bucket(P2, ELSEWHERE, AFTER, updates=7)])
    events = [   # id, kind, severity, prefix, start, end, title, historical
        (9001, "reachability_loss", "critical", None, T0, T0 + 15, f"{LINK} (link-a) unreachable from 9 of 10 RIPE Atlas probes", 0),
        (9002, "chain_change", "info", P1, T0 + 20, T0 + 20, f"New upstream chain 64512 64496 for {P1}", 1),
        (9003, "upstream_shift", "warning", P1, T0 + 300, T0 + 360, f"{P1}: 12 of 40 RIS peers moved between upstreams", 0),
        (9004, "transit_filter", "info", P1, T0 - 86400, None, f"AS64513 is not carrying {P1} from AS64496", 0),
        (9005, "upstream_shift", "warning", P1, QUIET, QUIET + 60, f"{P1}: a shift with nothing around it", 0),
        (9006, "upstream_down", "warning", P1, T0 - 30, T0 + 8 * 3600, f"{P1}: an upstream down for hours", 0),
        (9007, "path_change", "info", None, ELSEWHERE, ELSEWHERE, "The path back from AS64499 changed", 0)]
    conn.executemany("INSERT INTO events(id, kind, severity, prefix, start_ts, end_ts, title, detail, historical) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", [(*e[:7], "{}", e[7]) for e in events])
    conn.executemany("INSERT INTO atlas_msm(id, kind, purpose, target, created, probes, interval) VALUES(?,?,?,?,?,?,?)",
                     [(990000001, "ping", "baseline", LINK, T0 - 5 * 86400, 10, 1800),
                      (990000002, "ping", "baseline", LOOPBACK, T0 - 5 * 86400, 10, 1800)])
    rounds = []
    for t in range(T0 - 86400, QUIET + 1801, 1800):   # half-hourly; probe 1 never answers either address
        rounds += ping_round(990000001, t, silent=range(1, 10) if t == T0 else (1,))
        rounds += ping_round(990000002, t - 60, rtt=200.0 if t == T0 + 1800 else 40.0)
    conn.executemany("INSERT INTO atlas_result(msm_id, prb_id, ts, kind, sent, rcvd, rtt_avg, reached) VALUES(?,?,?,?,?,?,?,?)",
                     rounds)
    conn.executemany("INSERT OR REPLACE INTO asn_names(asn, name) VALUES(?,?)", [(64511, "TRANSIT-B"), (64512, "TRANSIT-C")])
    conn.commit()
    yield conn
    conn.execute("DELETE FROM events WHERE id BETWEEN 9001 AND 9099")
    conn.execute("DELETE FROM updates WHERE collector='rrc90'")
    conn.execute("DELETE FROM buckets WHERE ts BETWEEN ? AND ?", (T0 - 90000, T0 + 90000))
    conn.execute("DELETE FROM atlas_result WHERE msm_id >= 990000000")
    conn.execute("DELETE FROM atlas_msm WHERE id >= 990000000")
    conn.execute("DELETE FROM asn_names WHERE asn IN (64511, 64512)")
    conn.commit()


@pytest.fixture(autouse=True)
def offline(web, monkeypatch):
    async def no_lookups(sourceapp, asns):   # the pages ask RIPEstat about the upstreams' neighbours: not here
        return {}

    async def no_names(asns):
        return {}
    monkeypatch.setattr(web.tree, "upstream_neighbors", no_lookups)
    monkeypatch.setattr(web.svc, "as_names", no_names)


def collect(web, eid):
    ev = web.svc.aconn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    return asyncio.run(web._evidence(web.svc.aconn, ev))


# ------------------------------------------------------------------------------------------------ the page
def test_an_event_without_a_prefix_shows_what_the_routes_did(scene, admin_client):
    r = admin_client.get("/events/9001")
    assert r.status_code == 200
    t = r.text
    assert 'id="around"' in t and "Around this event" in t
    # it names none of the prefixes: the routes are those of the monitored prefix that changed
    assert "This event names none of your prefixes" in t and "whose routes changed most here" in t
    assert "27 of 40 RIS peers changed path" in t and "(1 ended where they began)" in t
    assert "<li>20 moved from AS64510 FAR-AWAY → AS64496 UPSTREAM-ONE to AS64512 TRANSIT-C → AS64496 UPSTREAM-ONE</li>" in t
    assert "<li>6 moved from AS64510 FAR-AWAY → AS64496 UPSTREAM-ONE to AS64511 TRANSIT-B → AS64497 UPSTREAM-TWO</li>" in t
    assert "30 → 24 peers" in t and "10 → 16 peers" in t
    # ...and the tree, the charts and the raw updates are that prefix's
    assert 'id="replay"' in t and "initReplay(9001)" in t and f'routes to <span class="mono">{P1}</span>' in t
    assert 'id="vis"' in t and f"prefix: {json.dumps(P1)}" in t


def test_the_ping_rounds_that_stood_out(scene, admin_client):
    t = admin_client.get("/events/9001").text
    assert "1 of 10 probes got a reply" in t and "(usually 9)" in t      # one probe never answers: 9 is what's usual
    assert "median round trip 200 ms" in t and "(usually 40)" in t
    assert "The other 3 ping rounds were as usual." in t


def test_every_other_event_of_the_window_is_listed(scene, admin_client):
    t = admin_client.get("/events/9001").text
    assert f"New upstream chain 64512 64496 for {P1}" in t and "found during backfill" in t   # an info one, backfilled
    assert "12 of 40 RIS peers moved between upstreams" in t and "began 5m 00s after" in t
    assert "began at the same time" in t   # (the chain change, 20 s in) and "30s before" for the upstream that was down
    assert "Already open when this one began" in t and f"AS64513 is not carrying {P1} from AS64496" in t
    assert "Related events:" not in t   # (the evidence panel's own list moved into this one)


def test_a_quiet_event_says_so(scene, admin_client):
    t = admin_client.get("/events/9005").text
    assert "Around this event" in t and f'No RIS peer changed its path to <span class="mono">{P1}</span>.' in t
    assert "None began in this window." in t and "names none of your prefixes" not in t
    assert "No RIPE Atlas ping rounds in this window." not in t and "ping rounds to your addresses were as usual" in t


def test_a_long_event_is_looked_at_around_its_start_and_its_end(scene, web, admin_client):
    d = collect(web, 9006)
    assert [sp["label"] for sp in d["around"]["routes"]] == ["around the start"]   # (nothing is stored at its end)
    assert d["around"]["routes"][0]["bursts"][0]["peers"] == 27
    assert "around the start" in admin_client.get("/events/9006").text


def test_the_fixtures_own_event_pages_still_render(scene, admin_client):
    # (not event 1: its seeded "confirm" detail is a word, where the service stores the check's whole result)
    for eid in (2, 3, 4, 5):
        r = admin_client.get(f"/events/{eid}")
        assert r.status_code == 200 and "Around this event" in r.text, eid
    assert "Nothing is stored for" in admin_client.get("/events/2").text   # (the fixtures seed no buckets)


# ------------------------------------------------------------------------------------------------ the evidence
def test_what_the_collection_holds(scene, web):
    d = collect(web, 9001)
    assert d["prefix"] == P1 and d["prefix_how"] == "busiest"
    (sp,) = d["around"]["routes"]
    assert sp["peers"] == 40 and not sp["settled"] and sp["changed"] == [64496, 64497]
    assert (sp["before"], sp["after"]) == ({64496: 30, 64497: 10}, {64496: 24, 64497: 16})
    (b,) = sp["bursts"]
    assert b["peers"] == 27 and b["back"] == 1 and T0 < b["start"] < b["end"] < T0 + B
    assert b["moves"] == [(((64510, 64496), (64512, 64496)), 20), (((64510, 64496), (64511, 64497)), 6)]
    assert d["around"]["rounds"] == 5 and d["around"]["targets"] == 2
    assert [(r["target"], r["odd"]) for r in d["around"]["odd"]] == [(LINK, "loss"), (LOOPBACK, "slow")]
    assert [r["id"] for r in d["related"]] == [9006, 9002, 9003] and [r["id"] for r in d["already_open"]] == [9004]
    assert any(k == "bgp" and "27 of 40 RIS peers changed their path" in text for _ts, k, text in d["timeline"])
    # the paths listed for the moment the mix changed most are those of that bucket's end, like its numbers
    during = d["paths"][1]
    assert during["label"].startswith("during") and {g["upstream"]: g["peers"] for g in during["groups"]} == {64496: 24, 64497: 16}


def test_the_prefix_an_event_is_drawn_from(scene, web):
    from bgpcanary import evidence
    conn, cfg = web.svc.aconn, web.cfg
    pick = lambda prefix, at=T0: evidence.focus_prefix(conn, cfg, {"prefix": prefix}, at - 1800, at + 1800)   # noqa: E731
    assert pick(P1) == (P1, "own") and pick(P2) == (P2, "own")
    assert pick("192.0.2.128/25") == (P1, "overlap")          # a more-specific of a monitored prefix
    assert pick("203.0.113.0/24") == (None, None)             # someone else's
    assert pick(None) == (P1, "busiest")                      # none named: the one whose routes changed
    assert pick(None, ELSEWHERE) == (P2, "busiest")
    assert pick(None, T0 - 40000) == (P1, "busiest")          # nothing changed anywhere: the first
    assert collect(web, 9007)["prefix"] == P2                 # (a Path finder watch's event names none either)


def test_a_run_of_changes_counts_each_peer_once():
    from bgpcanary import evidence
    a, b, c = [65010, 64510, 64496, 64500], [65010, 64512, 64496, 64500], [65010, 64511, 64497, 64500]
    ch = lambda peer, old, new: {"collector": "rrc90", "peer": peer, "old": old, "new": new}   # noqa: E731
    moves, back = evidence._net_moves([ch("p1", a, b), ch("p2", a, b), ch("p1", b, c), ch("p3", a, b), ch("p3", b, a),
                                       ch("p4", a, None), ch("p5", None, a)])
    assert back == 1   # p3 went and came back
    assert dict(moves) == {((64510, 64496), (64511, 64497)): 1, ((64510, 64496), (64512, 64496)): 1,
                           ((64510, 64496), None): 1, (None, (64510, 64496)): 1}


def test_rounds_are_judged_against_the_best_one_when_there_is_no_day_before(web):
    from bgpcanary import evidence
    rounds = [{"msm": 990000009, "probes": 10, "no_reply": n, "median_rtt": rtt}
              for n, rtt in ((1, 40.0), (1, 41.0), (4, 40.0), (1, 95.0), (6, None))]
    evidence._judge_rounds(web.svc.aconn, rounds, T0)
    assert [r["odd"] for r in rounds] == [None, None, "loss", "slow", "loss"] and rounds[0]["usual_no_reply"] == 1


def test_an_event_stored_twice_is_listed_once(scene, web):
    from bgpcanary import evidence
    conn = scene
    twin = ("upstream_shift", "warning", P1, QUIET + 600, QUIET + 660, f"{P1}: recorded again by a backfill", "{}", 1)
    conn.executemany("INSERT INTO events(id, kind, severity, prefix, start_ts, end_ts, title, detail, historical) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", [(9010, *twin), (9011, *twin)])
    conn.commit()
    try:
        ev = dict(conn.execute("SELECT * FROM events WHERE id=9005").fetchone())
        rows = evidence._other_events(conn, ev, QUIET - 1800, QUIET + 1860, time.time())
        assert [(r["id"], r["copies"], r["after"]) for r in rows if r["id"] >= 9010] == [(9010, 2, 600)]
        mine = evidence._other_events(conn, dict(conn.execute("SELECT * FROM events WHERE id=9011").fetchone()),
                                      QUIET - 1800, QUIET + 1860, time.time())
        assert 9010 not in [r["id"] for r in mine]   # ...and never as an event "around" its own copy
    finally:
        conn.execute("DELETE FROM events WHERE id IN (9010, 9011)")
        conn.commit()


def test_what_was_not_heard_live(scene, web):
    from bgpcanary import db, evidence
    conn = scene
    kept = db.kv_get(conn, "gaps", None)
    db.kv_set(conn, "gaps", [[QUIET - 5000, QUIET - 1700], [QUIET + 90000, QUIET + 90500]])
    conn.execute("INSERT INTO updates(ts, source, collector, peer, peer_asn, prefix, kind, path) VALUES(?,?,?,?,?,?,?,?)",
                 (QUIET + 5, "bgplay", "rrc90", "198.18.0.1", 65010, P1, "A", WAS))
    conn.commit()
    try:
        u = evidence._unheard(conn, P1, [("", QUIET - 1800, QUIET + 1860)])
        assert u["open"] == [(QUIET - 1800, QUIET - 1700)]   # clipped to the window; the later gap isn't in it
        assert u["filled"] == {"start": QUIET + 5, "end": QUIET + 5, "n": 1}
        assert evidence._unheard(conn, P1, [("", T0 - 1800, T0 + 1800)]) == {"open": [], "filled": None}
        both = evidence._unheard(conn, P1, [("around the start", T0 - 1800, T0 + 1800), ("around the end", QUIET - 1800, QUIET + 1860)])
        assert both == u   # (a long event: its two stretches together)
    finally:
        conn.execute("DELETE FROM updates WHERE source='bgplay' AND collector='rrc90'")
        if kept is None:
            conn.execute("DELETE FROM kv WHERE k='gaps'")
        else:
            db.kv_set(conn, "gaps", kept)
        conn.commit()


def test_lengths_of_time_in_words():
    from bgpcanary import evidence
    assert [evidence._span_of(s) for s in (45, -45, 300, 5399, 7500, 3 * 86400 + 4 * 3600)] == \
        ["45 s", "45 s", "5 min", "90 min", "2 h 05 min", "3 d 4 h"]


# ------------------------------------------------------------------------------------------------ the report
def test_the_text_report_tells_it_too(scene, admin_client):
    r = admin_client.get("/events/9001/report.txt")
    assert r.status_code == 200
    t = r.text
    assert "AROUND THIS EVENT" in t and f"Routes to {P1}, as the RIS peers saw them (this event names none of your prefixes):" in t
    assert "27 of 40 RIS peers changed path: 20 moved from AS64510 FAR-AWAY → AS64496 UPSTREAM-ONE to AS64512 TRANSIT-C" in t
    assert "(1 of them ended where they began)" in t and "AS64496 30 → 24 peers, AS64497 10 → 16 peers." in t
    assert f"{LINK}: 1 of 10 probes got a reply (usually 9)" in t and "median round trip 200 ms (usually 40)" in t
    assert "The other 3 were as usual." in t
    assert "5 min after this one began: [warning]" in t and "found during backfill" in t
    assert "Already open when this one began:" in t
    quiet = admin_client.get("/events/9005/report.txt").text
    assert "No RIS peer changed its path." in quiet and "all as usual." in quiet


# ------------------------------------------------------------------------------------------------ the raw list
def raw_part(page):
    """The event page's raw list, as HTML."""
    return page.split('id="raw"', 1)[1].split("</details>", 1)[0]


def test_the_raw_list_covers_the_half_hour_either_side(scene, admin_client):
    # the event lasted 15 s: the updates 16, 17 and 20 s after its start used to be left out, the list stopping at its end
    t = admin_client.get("/events/9001").text
    raw = raw_part(t)
    assert f'28 updates for <span class="mono">{P1}</span> in the half hour either side of the event' in t
    assert raw.count("<tr>") == 1 + 28 and "raw-book" not in raw   # (a header row, then the updates)
    assert "Around the start" not in raw and "The list stops" not in raw and "bookkeeping" not in raw
    assert f'href="/data?tab=changes&prefix={P1}&start={T0 - 1800}&end={T0 + 15 + 1800}"' in raw   # (the same stretch)


def test_bookkeeping_rows_wait_behind_a_tick_box(scene, admin_client):
    conn = scene
    conn.executemany("INSERT INTO updates(ts, source, collector, peer, peer_asn, prefix, kind, path) VALUES(?,?,?,?,?,?,?,?)",
                     [(T0 + 300, "resync", "rrc90", "198.18.0.5", 65010, P1, "A", NOW_SAME_UPSTREAM),
                      (T0 + 400, "stale", "rrc90", "198.18.0.6", 65010, P1, "W", None)])
    conn.commit()
    try:
        t = admin_client.get("/events/9001").text
        raw = raw_part(t)
        assert "28 updates for" in t   # (they aren't counted as updates)
        assert "show bookkeeping rows too (2: looking-glass corrections" in raw and "classList.toggle('raw-all'" in raw
        assert raw.count('<tr class="raw-book">') == 2 and raw.count("<tr>") == 1 + 28
        ev = conn.execute("SELECT * FROM events WHERE id=9001").fetchone()
        from bgpcanary import evidence
        (st,) = evidence.raw_updates(conn, P1, ev, cap=3)   # bookkeeping rows get a third of the share: 1 here
        assert (st["bookkeeping"], st["book_shown"], st["shown"]) == (2, 1, 3)
    finally:
        conn.execute("DELETE FROM updates WHERE collector='rrc90' AND source IN ('resync', 'stale')")
        conn.commit()


def test_the_raw_list_says_when_it_stops_short(scene, web, admin_client, monkeypatch):
    from bgpcanary import evidence
    ev = scene.execute("SELECT * FROM events WHERE id=9001").fetchone()
    (st,) = evidence.raw_updates(scene, P1, ev, cap=10)
    assert (st["updates"], st["shown"], len(st["rows"])) == (28, 10, 10)
    assert (st["bookkeeping"], st["book_shown"]) == (0, 0)
    assert [r["ts"] for r in st["rows"]] == sorted(r["ts"] for r in st["rows"]) and st["rows"][0]["ts"] == T0 + 1.5
    real = evidence.raw_updates
    monkeypatch.setattr(web.evidence, "raw_updates", lambda conn, prefix, ev: real(conn, prefix, ev, cap=10))
    raw = raw_part(admin_client.get("/events/9001").text)
    assert "The list stops at the first 10 of the 28 updates in this stretch." in raw
    assert f'href="/data?tab=raw&prefix={P1}&start={T0 - 1800}&end={T0 + 15 + 1800}"' in raw and raw.count("<tr>") == 1 + 10


def test_a_long_events_raw_list_has_its_start_and_its_end(scene, admin_client):
    from bgpcanary import evidence
    ev = scene.execute("SELECT * FROM events WHERE id=9006").fetchone()
    first, last = evidence.raw_updates(scene, P1, ev)
    assert (first["label"], first["updates"], first["end"]) == ("around the start", 28, ev["start_ts"] + 1800)
    assert (last["label"], last["updates"], last["start"]) == ("around the end", 0, ev["end_ts"] - 1800)
    raw = raw_part(admin_client.get("/events/9006").text)
    assert "Around the start" in raw and "Around the end" in raw and "No updates are stored for this stretch." in raw
    assert "in the half hours around the start and the end" in admin_client.get("/events/9006").text


def test_an_end_stored_before_the_start_counts_as_the_start(scene, admin_client):
    from bgpcanary import evidence
    conn = scene
    conn.execute("INSERT INTO events(id, kind, severity, prefix, start_ts, end_ts, title, detail, historical) "
                 "VALUES(9012, 'outage', 'critical', ?, ?, ?, 'An outage closed before it began', '{}', 0)", (P1, T0 - 10, T0 - 1840))
    conn.commit()
    try:
        ev = conn.execute("SELECT * FROM events WHERE id=9012").fetchone()
        assert evidence.window(ev) == (T0 - 10 - 1800, T0 - 10 + 1800)   # (it was cut off 30 s before the start)
        (st,) = evidence.raw_updates(conn, P1, ev)
        assert st["updates"] == 28
        assert "28 updates for" in admin_client.get("/events/9012").text
    finally:
        conn.execute("DELETE FROM events WHERE id=9012")
        conn.commit()


# ------------------------------------------------------------------------------------------------ the tree
def test_the_tree_replays_the_prefix_the_page_is_about(scene, admin_client):
    r = admin_client.get("/api/events/9001/replay")
    assert r.status_code == 200
    d = r.json()
    assert d["prefix"] == P1 and d["event"]["id"] == 9001 and d["event"]["prefix"] is None
    assert len(d["peers"]) == 40 and len(d["updates"]) == 28
    ids = [e["id"] for e in d["events"]]
    assert ids[0] == 9001 and 9003 in ids and 9006 in ids   # the strip shades the event and the trouble around it
    assert 9002 not in ids and 9004 not in ids              # ...not the info ones
    assert admin_client.get("/api/events/9007/replay").json()["prefix"] == P2
    assert admin_client.get("/api/events/999999/replay").status_code == 404


def test_the_timeline_gets_a_mark_for_each_thing_the_panel_lists(scene, admin_client):
    d = admin_client.get("/api/events/9001/replay").json()
    assert (d["start"], d["end"]) == (T0 - 1800, T0 + 15 + 1800)   # the same half hour either side as the panel
    marks = d["marks"]
    assert [m["t"] for m in marks] == sorted(m["t"] for m in marks)
    by = {}
    for m in marks:
        by.setdefault(m["kind"], []).append(m)
    (burst,) = by["routes"]   # the burst of path changes, as a stretch
    assert T0 < burst["t"] < burst["end"] < T0 + B and burst["level"] == "info"
    assert burst["text"].startswith(f"27 of 40 RIS peers changed their path to {P1}: 20 moved from AS64510 FAR-AWAY")
    loss, slow = by["ping"]   # the two rounds that stood out, as moments
    assert (loss["level"], slow["level"]) == ("critical", "warning") and loss["end"] is None
    assert loss["text"] == f"{LINK}: 1 of 10 probes got a reply (usually 9)" and T0 <= loss["t"] <= T0 + 10
    assert slow["text"] == f"{LOOPBACK}: median round trip 200 ms (usually 40)" and slow["t"] > T0 + 1700
    # every other event that began in the window, the info one too; not the one already open before it
    assert [(m["id"], m["level"]) for m in by["event"]] == [(9006, "warning"), (9002, "info"), (9003, "warning")]
    assert "unheard" not in by and all(m.get("id") != 9004 for m in marks)
    # an event with nothing around it has no marks, and the page explains the pins
    assert admin_client.get("/api/events/9005/replay").json()["marks"] == []
    page = admin_client.get("/events/9001").text
    assert 'data-rp="note"' in page and "The pins on the strip are what" in page and "with a pin on its timeline" in page


def test_marks_of_a_long_event_and_of_a_stretch_not_heard(scene, web):
    from bgpcanary import db, evidence
    conn = scene
    ev = conn.execute("SELECT * FROM events WHERE id=9006").fetchone()
    marks = evidence.marks(conn, web.cfg, ev, lambda target: "link-a" if target == LINK else "", lambda asns: {})
    rest = [m for m in marks if m["kind"] != "event"]
    assert [m["kind"] for m in rest] == ["ping", "routes", "ping"]   # the lost round, the burst, the slow round
    assert rest[0]["text"] == f"{LINK} (link-a): 1 of 10 probes got a reply (usually 9)"   # (with the target's label)
    assert {m["id"] for m in marks if m["kind"] == "event"} == {9001, 9002, 9003, 9005, 9007}
    # a reachability-loss event is a ping round itself: that round is the event on the timeline, not a pin beside it
    conn.execute("UPDATE events SET detail=? WHERE id=9001", (json.dumps({"target": LINK, "msm": 990000001}),))
    conn.commit()
    try:
        loss_event = conn.execute("SELECT * FROM events WHERE id=9001").fetchone()
        pings = [m for m in evidence.marks(conn, web.cfg, loss_event, lambda t: "", lambda a: {}) if m["kind"] == "ping"]
        assert [m["level"] for m in pings] == ["warning"]   # (the slow round to the other address stays)
    finally:
        conn.execute("UPDATE events SET detail='{}' WHERE id=9001")
        conn.commit()
    # the state at the window's start, when the caller read it for another prefix, isn't taken for this one's
    assert evidence.marks(conn, web.cfg, ev, lambda t: "", lambda a: {}, state=(P2, [])) == \
        evidence.marks(conn, web.cfg, ev, lambda t: "", lambda a: {})
    kept = db.kv_get(conn, "gaps", None)
    db.kv_set(conn, "gaps", [[QUIET - 100, QUIET + 200]])
    conn.commit()
    try:
        quiet = conn.execute("SELECT * FROM events WHERE id=9005").fetchone()
        (gap,) = evidence.marks(conn, web.cfg, quiet, lambda t: "", lambda a: {})
        assert (gap["kind"], gap["t"], gap["end"]) == ("unheard", QUIET - 100, QUIET + 200) and "RIS Live" in gap["text"]
    finally:
        if kept is None:
            conn.execute("DELETE FROM kv WHERE k='gaps'")
        else:
            db.kv_set(conn, "gaps", kept)
        conn.commit()


def test_the_strip_under_a_tree_shades_every_warning_and_critical_event(scene, admin_client):
    r = admin_client.get("/api/replay", params={"prefix": P1, "start": T0 - 3600, "end": T0 + 3600})
    assert r.status_code == 200
    ids = [e["id"] for e in r.json()["events"]]
    assert ids == [9006, 9001, 9003]   # oldest first: one of this prefix, one that names none, one more of this prefix
    other = admin_client.get("/api/replay", params={"prefix": P2, "start": T0 - 3600, "end": T0 + 3600}).json()
    assert [e["id"] for e in other["events"]] == [9001]   # (an event without a prefix belongs on every prefix's strip)
