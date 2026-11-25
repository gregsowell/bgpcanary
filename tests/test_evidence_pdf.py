"""The event page's "Download all evidence (PDF)": everything the page shows, in one PDF to send to a provider
(bgpcanary/evidence_pdf.py), read back with tests/pdfcheck.py. The "Download evidence GIF" next to it is built in the
browser (static/replay.js evidenceGif): only its button is checked here.

The scene, 20 days back: 40 RIS peers reach 192.0.2.0/24 (AS64500), 30 through AS64496 and 10 through AS64497. For
half an hour 36 of them lose it (an outage, checked against Route Views and RIPE Atlas, which lost it too), then it
comes back. Two hours later the 30 behind AS64496 lose it again, and that event is still open. In the outage's first
minutes a carrier's link address stops answering RIPE Atlas: an event that names no prefix."""
import asyncio
import json
import re
import time

import pytest

import pdfcheck
from conftest import P1

B = 30
T0 = int((time.time() - 20 * 86400) // B * B)
OUT, OPEN, LINK = 9101, 9102, 9103
PEERS = [("rrc91", f"198.18.1.{i}", 65020 if i < 30 else 65021) for i in range(40)]
VIA = ["65020 64510 64496 64500"] * 30 + ["65021 64511 64497 64500"] * 10
RV_RAW = """BGP routing table entry for 192.0.2.0/24, version 7
Paths: (2 available, best #1, table default)
  Not advertised to any peer
  3356 64496 64500 64500
    198.51.100.9 from 198.51.100.9 (198.51.100.9)
      Origin IGP, metric 0, valid, external, best
      Community: 3356:100
  1299 64497 64500
    203.0.113.7 from 203.0.113.7 (203.0.113.7)
      Origin IGP, valid, external"""
CONFIRM = {"verdict": "confirmed", "why": "Route Views has lost it too", "at": T0 + 90,
           "route_views": {"servers": [{"server": "route-views.example", "paths": 0, "usual": 2}]},
           "atlas": {"replied": 1, "probes": 10, "target": "192.0.2.1", "ts": T0 + 100}}


def bucket(ts, visible, a, b, updates=0):
    return (P1, ts, visible, 40.0, visible, json.dumps({"65020 64510 64496": a, "65021 64511 64497": b}),
            json.dumps({"64496": a, "64497": b}), updates, 0)


@pytest.fixture(scope="module")
def scene(web):
    conn = web.svc.aconn
    ups = [(T0 - 3600, "bgplay-init", *p, P1, "A", VIA[i]) for i, p in enumerate(PEERS)]
    ups += [(T0 + 5 + i * 0.5, "ris-live", *PEERS[i], P1, "W", None) for i in range(36)]
    ups += [(T0 + 1700 + i * 0.5, "ris-live", *PEERS[i], P1, "A", VIA[i]) for i in range(36)]
    ups += [(T0 + 7205 + i * 0.2, "ris-live", *PEERS[i], P1, "W", None) for i in range(30)]
    conn.executemany("INSERT INTO updates(ts, source, collector, peer, peer_asn, prefix, kind, path) "
                     "VALUES(?,?,?,?,?,?,?,?)", ups)
    conn.executemany("INSERT INTO buckets(prefix, ts, visible, baseline, min_visible, chains, upstreams, updates, "
                     "withdrawals) VALUES(?,?,?,?,?,?,?,?,?)",
                     [bucket(T0 - 1830, 40, 30, 10), bucket(T0 - 30, 40, 30, 10), bucket(T0 + 300, 4, 0, 4, 36),
                      bucket(T0 + 1890, 40, 30, 10, 36), bucket(T0 + 7170, 40, 30, 10), bucket(T0 + 7230, 10, 0, 10, 30),
                      bucket(T0 + 7800, 12, 0, 12)])
    events = [(OUT, "outage", "critical", P1, T0, T0 + 1800, "192.0.2.0/24 outage: visibility fell to 4/40 RIS peers",
               {"confirm": CONFIRM}, 4, 40.0),
              (OPEN, "upstream_down", "warning", P1, T0 + 7200, None, "192.0.2.0/24: your upstream AS64496 has gone",
               {}, None, None),
              (LINK, "reachability_loss", "critical", None, T0 + 600, T0 + 615,
               "203.0.113.9 (link-a) unreachable from 9 of 10 RIPE Atlas probes", {}, None, None)]
    conn.executemany("INSERT INTO events(id, kind, severity, prefix, start_ts, end_ts, title, detail, min_visible, "
                     "baseline, historical) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                     [(*e[:7], json.dumps(e[7]), *e[8:]) for e in events])
    conn.execute("INSERT INTO rv_snapshots(ts, server, prefix, n_paths, paths, raw, event_id) VALUES(?,?,?,?,?,?,?)",
                 (T0 + 120, "route-views.example", P1, 2, json.dumps(["3356 64496 64500", "1299 64497 64500"]), RV_RAW, OUT))
    conn.execute("INSERT INTO probes(ts, server, kind, target, trigger, event_id, success_pct, reached, output) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (T0 + 150, "route-views.example", "ping", "192.0.2.1", "event", OUT, 0.0, 0,
                                               "Sending 5, 100-byte ICMP Echos to 192.0.2.1\n.....\nSuccess rate is 0 percent (0/5)"))
    conn.execute("INSERT INTO atlas_msm(id, kind, purpose, target, created, probes, interval) VALUES(?,?,?,?,?,?,?)",
                 (990000101, "ping", "baseline", "192.0.2.1", T0 - 5 * 86400, 10, 1800))
    conn.executemany("INSERT INTO atlas_result(msm_id, prb_id, ts, kind, sent, rcvd, rtt_avg, reached) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     [(990000101, p, t + p, "ping", 3, 0 if t == T0 + 100 and p > 1 else 3,
                       None if t == T0 + 100 and p > 1 else 40.0, 0 if t == T0 + 100 and p > 1 else 1)
                      for t in (T0 - 1700, T0 + 100) for p in range(1, 11)])
    conn.commit()
    yield conn
    conn.execute("DELETE FROM events WHERE id BETWEEN 9101 AND 9199")
    conn.execute("DELETE FROM updates WHERE collector='rrc91'")
    conn.execute("DELETE FROM buckets WHERE prefix=? AND ts BETWEEN ? AND ?", (P1, T0 - 3600, T0 + 9000))
    conn.execute("DELETE FROM rv_snapshots WHERE server='route-views.example'")
    conn.execute("DELETE FROM probes WHERE server='route-views.example'")
    conn.execute("DELETE FROM atlas_result WHERE msm_id=990000101")
    conn.execute("DELETE FROM atlas_msm WHERE id=990000101")
    conn.commit()


@pytest.fixture(autouse=True)
def offline(web, monkeypatch):
    async def no_lookups(sourceapp, asns):   # the tree asks RIPEstat about the upstreams' neighbours: not here
        return {}
    monkeypatch.setattr(web.tree, "upstream_neighbors", no_lookups)


def get_pdf(c, eid):
    r = c.get(f"/events/{eid}/evidence.pdf")
    assert r.status_code == 200, r.text[:300]
    assert r.headers["content-type"] == "application/pdf"
    return r


def images(data):
    return sum(b"/Subtype /Image" in b for b in pdfcheck.objects(data).values())


def test_the_pdf_holds_what_the_page_shows(scene, admin_client):
    r = get_pdf(admin_client, OUT)
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(T0))
    assert r.headers["content-disposition"] == f'attachment; filename="bgpcanary-event-{OUT}-{stamp}.pdf"'
    t = pdfcheck.text(r.content)
    for want in ["192.0.2.0/24 outage: visibility fell to 4/40 RIS peers", "CRITICAL", "4 of 40 RIS peers (10%)",
                 "Verdict: confirmed. Route Views has lost it too.", "route-views.example",
                 # what went on around it, the tree at three moments, the timeline
                 "Around this event", "36 of 40 RIS peers changed path", "Other events:",
                 "203.0.113.9 (link-a) unreachable from 9 of 10 RIPE Atlas probes",
                 "Before the event (", "At the peak (", "After the event (", "Timeline (UTC)",
                 # who carried it, and the AS paths before, at the peak and after
                 "BGP: who carried 192.0.2.0/24", "AS64496", "AS paths before ·", "AS paths during (peak change) ·",
                 "AS paths after ·", "65020 64510 64496 64500",
                 # Route Views: one line a path, the best marked; its probe
                 "route-views.example (192.0.2.0/24): 2 paths", "3356 64496 64500 64500  from 198.51.100.9  best",
                 "Success rate is 0 percent (0/5)",
                 # RIPE Atlas, the charts, the raw updates, all in UTC
                 "Ping rounds to your addresses", "1 / 10", "Visibility around the event",
                 "Appendix: raw BGP updates for 192.0.2.0/24", "All times UTC."]:
        assert want in t, want
    assert re.search(r"^1299 64497 64500 +from 203\.0\.113\.7$", t, re.M)   # (not the best one)
    assert "3356:100" not in t   # (a path's communities and timers are on the page and in the text report)
    assert [title for title, _p, _k in pdfcheck.outline(r.content)] == [
        "192.0.2.0/24 outage: visibility fell to 4/40 RIS peers", "Checked against independent sources",
        "Around this event", "Path tree", "Timeline (UTC)", "BGP: who carried 192.0.2.0/24", "Route Views", "RIPE Atlas",
        "Visibility around the event", "Appendix: raw BGP updates for 192.0.2.0/24"]
    assert images(r.content) == 6   # the tree three times, the three charts
    # the appendix: every update of the stretch, one line each
    assert re.search(r"\d\d:\d\d:\d\d  rrc91   198\.18\.1\.0 +AS65020 +W  ris-live", t)


def test_an_open_event_pictures_the_tree_now(scene, admin_client):
    t = pdfcheck.text(get_pdf(admin_client, OPEN).content)
    assert "still ongoing at" in t and "so far" in t
    assert "Before the event (" in t and "At the peak (" in t and "Now (" in t and "After the event (" not in t
    assert "Where the routes stand now: the event is still open." in t


def test_an_event_without_a_prefix_shows_the_routes_of_one(scene, admin_client):
    r = get_pdf(admin_client, LINK)
    t = pdfcheck.text(r.content)
    assert "none (the routes shown are those of 192.0.2.0/24)" in t
    assert "BGP: who carried 192.0.2.0/24" in t and images(r.content) >= 4


def test_who_may_download_it(scene, viewer_client, client, admin_client):
    get_pdf(viewer_client, OUT)
    r = client.get(f"/events/{OUT}/evidence.pdf", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]
    assert admin_client.get("/events/987654/evidence.pdf").status_code == 404


def test_your_alert_channels_stay_out(admin_client):
    """Event 1 of the fixtures: an alert went to a webhook, which failed. That's yours, not the provider's business."""
    t = pdfcheck.text(get_pdf(admin_client, 1).content)
    assert "Outage: 192.0.2.0/24 seen by 4 of 320" in t
    assert "webhook" not in t and "HTTP 500" not in t and "Alerts sent" not in t


def test_a_long_list_keeps_its_start_and_end(scene, admin_client, monkeypatch):
    """An event open for weeks has thousands of timeline rows and ping rounds: the PDF keeps the first and the last."""
    from bgpcanary import evidence, evidence_pdf
    monkeypatch.setattr(evidence_pdf, "CAP_TIMELINE", (2, 1))
    monkeypatch.setattr(evidence_pdf, "CAP_ROUNDS", (1, 0))
    t = " ".join(pdfcheck.text(get_pdf(admin_client, OUT).content).split())   # (the notes wrap in their cells)
    assert re.search(r"\.\.\. \d+ more between 20\d\d-\S+ \S+ UTC and 20\d\d-\S+ \S+ UTC: the event page lists them all", t)
    assert re.search(r"\.\.\. 1 more after 20\d\d-\S+ \S+ UTC: the event page lists them all", t)   # (the rounds: none kept after)
    assert t.count("the event page lists them all") == 2
    assert evidence.cut(list(range(10)), (3, 2)) == ([0, 1, 2], [8, 9], 5)
    assert evidence.cut(list(range(5)), (3, 2)) == ([0, 1, 2, 3, 4], [], 0) == evidence.cut(list(range(5)), None)


def test_the_page_has_both_buttons(scene, admin_client):
    t = admin_client.get(f"/events/{OUT}").text
    assert f'href="/events/{OUT}/evidence.pdf" data-dl="pdf"' in t and "Download all evidence (PDF)" in t
    assert 'data-dl="gif"' in t and "Download evidence GIF" in t


def test_route_views_answers_one_line_a_path():
    from bgpcanary import evidence_pdf
    assert evidence_pdf._rv_paths(RV_RAW) == [["3356 64496 64500 64500", "198.51.100.9", True],
                                              ["1299 64497 64500", "203.0.113.7", False]]
    # (answers stored before bgpcanary kept the whole of them: the AS paths alone)
    assert evidence_pdf._rv_paths("  3356 64496 64500\n  1299 64497 64500") == [["3356 64496 64500", "", False],
                                                                                ["1299 64497 64500", "", False]]


def test_the_charts_draw_what_the_page_gets(scene, web, admin_client):
    from bgpcanary import charts
    d = admin_client.get("/api/series", params={"prefix": P1, "start": T0 - 3600, "end": T0 + 9000}).json()
    assert d["ts"][0] == T0 - 1830 and None in d["visible"]   # (a gap in the buckets: a break in the line)
    assert d["threshold_pct"] and set(d["chains"]) >= {"65020 64510 64496", "other"}
    pngs = charts.all_charts(d)
    assert [title for title, _png in pngs] == ["Visibility around the event", "Upstream chains", "Update churn"]
    assert all(png.startswith(b"\x89PNG\r\n\x1a\n") for _t, png in pngs)
    assert charts.all_charts({**d, "ts": []}) == []


def test_the_tree_pictures_say_utc(scene, web):
    from bgpcanary import tree
    out = asyncio.run(tree.pictures(web.cfg, None, P1, [("before", T0 - 1800, "Before"), ("peak", T0 + 330, "Peak")],
                                    utc=True))
    assert [cid for cid, _png, _cap in out] == ["tree-before", "tree-peak"]
    assert out[1][2] == "Peak (" + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(T0 + 330)) + ")"
