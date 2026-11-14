"""Transit lost: a network behind one of your upstreams stops carrying you on from it, while the upstream itself stays
up and visibility holds. The RIS peers find another way in at once, so no outage, dip or upstream shift shows
it: this event does.

The scene (no network; a scratch engine on its own database, fed by hand): 40 RIS peers reach 192.0.2.0/24 (AS64500),
20 of them through AS64510 behind your upstream AS64496, 12 through AS64511 behind it too, and 8 through AS64512 behind
your other upstream AS64497. Half an hour of that, then AS64510 drops out: 15 of its peers come in through AS64511
instead, 5 through AS64513 behind AS64497."""
import dataclasses
import time

import pytest

from conftest import CONFIG, P1

T0 = 1_760_000_000 // 3600 * 3600        # on the hour: on a bucket's edge
U1, U2 = 64496, 64497                    # your upstreams (tests/conftest.py's config)
T1, T2, T3, T4 = 64510, 64511, 64512, 64513
PEERS = [f"203.0.113.{i}" for i in range(1, 41)]
LOST = PEERS[:20]                        # the ones that come in through AS64510


def route(peer):
    i = PEERS.index(peer)
    return [65001, T1, U1, 64500] if i < 20 else [65002, T2, U1, 64500] if i < 32 else [65003, T3, U2, 64500]


def elsewhere(peer):
    """Where a peer of AS64510's goes when it drops out: three in four to AS64511 behind the same upstream."""
    return [65001, T2, U1, 64500] if LOST.index(peer) < 15 else [65001, T4, U2, 64500]


@pytest.fixture
def scene(tmp_path):
    """(cfg, conn, engine, alerts heard): the 40 routes since T0, half an hour of buckets behind them."""
    from bgpcanary import config, db
    cfg = dataclasses.replace(config.load(CONFIG), prefixes=[P1], control_prefixes=[], db_path=str(tmp_path / "tl.db"))
    db.init(cfg.db_path)
    conn = db.connect(cfg.db_path)
    eng, heard = start(cfg, conn, {p: route(p) for p in PEERS})
    eng.tick(T0 + 1800)
    yield cfg, conn, eng, heard
    conn.close()


def start(cfg, conn, routes, at=T0):
    """An engine as the service starts one: the routes RIS holds, then live."""
    from bgpcanary.engine import Engine
    eng, heard = Engine(cfg, conn), []
    eng.listeners.append(lambda eid, phase: heard.append((eid, phase)))
    eng.quiet, eng.status["seeding"] = True, True   # (the routes it starts with aren't news: no "new chain")
    for p, path in routes.items():
        eng.apply(at, "rrc00", p, 65000, P1, "A" if path else "W", path, source="bgplay-init")
    eng.quiet, eng.status["seeding"] = False, False
    eng.last_bucket = at
    for m in eng.mons.values():
        m.bucket_min = eng.visible(m.prefix)
    return eng, heard


def move(eng, at, peers, where):
    """Each of `peers` changes its route at `at` (None: withdrawn), then the buckets close up to there."""
    eng.tick(at)
    for p in peers:
        path = where(p)
        eng.apply(at, "rrc00", p, 65000, P1, "A" if path else "W", path)


def events(conn, kind="transit_lost"):
    return [dict(r) for r in conn.execute("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,))]


def test_a_transit_dropping_out_behind_an_upstream_is_reported(scene):
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1805, LOST, elsewhere)
    eng.tick(T0 + 1835)
    assert events(conn) == []   # one bucket isn't enough: a resync or an odd snapshot could do that
    eng.tick(T0 + 1865)
    (ev,) = events(conn)
    assert (ev["severity"], ev["prefix"], ev["start_ts"], ev["end_ts"]) == ("info", P1, T0 + 1800, None)
    assert ev["title"] == f"{P1}: AS{T1}, behind your upstream AS{U1}, stopped carrying you (20 → 0 RIS peers)"
    import json
    d = json.loads(ev["detail"])
    assert (d["key"], d["upstream"], d["transit"], d["baseline"], d["carrying"]) == (f"tl|{P1}|{U1}|{T1}", U1, T1, 20, 0)
    assert d["moved_to"] == [[U1, T2, 15], [U2, T4, 5]]
    assert f"They reach you through AS{T2} behind AS{U1} (+15), AS{T4} behind your upstream AS{U2} (+5) instead." in d["note"]
    assert "Visibility held: 40 of 40 RIS peers still see the prefix." in d["note"]
    assert f"Usually AS{U1} lost its session with AS{T1}, or AS{T1} stopped passing your prefix on." in d["note"]
    assert d["still_via"] == []
    assert heard[-1] == (ev["id"], "open")
    # nothing else saw it: visibility held, and only 5 of 40 peers changed upstream (AS64513 behind AS64497 is new)
    others = [(r["kind"], r["title"]) for r in conn.execute("SELECT kind, title FROM events WHERE kind!='transit_lost'")]
    assert others == [("chain_change", f"New upstream chain {T4} {U2} for {P1}")]


def test_one_that_still_carries_you_another_way_is_told_as_such(scene):
    """AS64510's peers now come in through AS64510 → AS64513 → AS64497: it still carries you, behind your other
    upstream. It's reported (the pair AS64510 → AS64496 is gone), but not as if it had stopped passing your prefix on.
    AS64496 is left with 12 of its 32 paths, so it's given five minutes to turn into an upstream-down event first; it
    doesn't, and by then the lost transit has lasted long enough to be a warning."""
    import json
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1805, LOST, lambda p: [65001, T1, T4, U2, 64500])
    eng.tick(T0 + 1800 + 5 * 60 + 30)
    assert events(conn) == []
    eng.tick(T0 + 1800 + 6 * 60)
    (ev,) = events(conn)
    assert (ev["severity"], ev["start_ts"], ev["end_ts"]) == ("warning", T0 + 1800, None)
    assert [ph for i, ph in heard if i == ev["id"]] == ["open"]
    d = json.loads(ev["detail"])
    assert d["still_via"] == [[T4, U2, 20]]
    assert (f"AS{T1} still carries you, now mostly through AS{T4} behind your upstream AS{U2} (20 RIS peers): "
            f"either AS{U1} lost its session with AS{T1}, or AS{T1} now prefers that way in.") in d["note"]
    assert "stopped passing your prefix on" not in d["note"]


def test_it_ends_once_the_transit_is_back_and_stays_informational(scene):
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1805, LOST, elsewhere)
    move(eng, T0 + 1900, LOST, route)             # back after a minute and a half
    eng.tick(T0 + 1900 + 500)
    assert events(conn)[0]["end_ts"] is None      # it has to stay back for 10 minutes: one that flaps is one event
    eng.tick(T0 + 1900 + 700)
    (ev,) = events(conn)
    assert (ev["severity"], ev["start_ts"], ev["end_ts"]) == ("info", T0 + 1800, T0 + 1920)
    assert ev["title"] == (f"{P1}: AS{T1}, behind your upstream AS{U1}, stopped carrying you for 2 min "
                           f"(20 → 0 RIS peers) and is back (20)")
    assert [ph for i, ph in heard if i == ev["id"]] == ["open", "close"]
    # AS64511 carried 27 for a while and went back to its 12: not a loss
    assert len(events(conn)) == 1


def test_one_that_lasts_turns_into_a_warning(scene):
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1805, LOST, elsewhere)
    eng.tick(T0 + 1800 + 4 * 60)
    assert events(conn)[0]["severity"] == "info"
    eng.tick(T0 + 1800 + 5 * 60 + 30)
    (ev,) = events(conn)
    assert ev["severity"] == "warning" and ev["end_ts"] is None
    assert [ph for i, ph in heard if i == ev["id"]] == ["open", "escalate"]
    cfg.transit_lost_warn_minutes = 0   # 0: a warning at once
    move(eng, T0 + 3000, LOST, route)
    eng.tick(T0 + 3700)
    move(eng, T0 + 3705, LOST, elsewhere)
    eng.tick(T0 + 3770)
    assert [e["severity"] for e in events(conn)] == ["warning", "warning"]


def test_a_flap_within_one_bucket_is_nothing(scene):
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1825, LOST, elsewhere)   # gone at the end of one bucket...
    move(eng, T0 + 1845, LOST, route)       # ...back in the next
    eng.tick(T0 + 1950)
    assert events(conn) == []


def test_it_can_be_switched_off_and_tuned(scene):
    cfg, conn, eng, heard = scene
    cfg.transit_lost_min_pct = 60   # AS64510 carries half of the peers: too little to be watched now
    move(eng, T0 + 1805, LOST, elsewhere)
    eng.tick(T0 + 1900)
    assert events(conn) == []
    cfg.transit_lost_min_pct = 10
    move(eng, T0 + 2400, LOST, route)
    eng.tick(T0 + 4200)
    cfg.transit_lost_check = False
    move(eng, T0 + 4205, LOST, elsewhere)
    eng.tick(T0 + 4300)
    assert events(conn) == []
    cfg.transit_lost_check = True   # switched on while it's out, then off again while one is open
    move(eng, T0 + 4400, LOST, route)
    eng.tick(T0 + 6200)
    move(eng, T0 + 6205, LOST, elsewhere)
    eng.tick(T0 + 6300)
    assert events(conn)[0]["end_ts"] is None
    cfg.transit_lost_check = False
    eng.tick(T0 + 6330)
    assert events(conn)[0]["end_ts"] == T0 + 6330   # what was open ends when it's switched off


def test_its_usual_reach_is_not_its_peak(scene):
    """AS64511 carries 27 while AS64510 is out, then its usual 12 again: a fall to under half of its recent peak, but
    not of what it usually carries."""
    cfg, conn, eng, heard = scene
    cfg.transit_lost_pct = 50
    move(eng, T0 + 1805, LOST, elsewhere)
    move(eng, T0 + 2400, LOST, route)
    eng.tick(T0 + 3300)
    assert [(e["title"].split(",")[0]) for e in events(conn)] == [f"{P1}: AS{T1}"]


def test_an_upstream_that_loses_everything_is_an_upstream_down_not_a_lost_transit(scene):
    cfg, conn, eng, heard = scene
    gone = PEERS[:32]   # everyone through AS64496 comes in through AS64497 instead
    move(eng, T0 + 1805, gone, lambda p: [65001, T4, U2, 64500])
    eng.tick(T0 + 1900)
    assert events(conn) == []
    assert [e["kind"] for e in events(conn, "upstream_down")] == ["upstream_down"]


def test_an_upstream_losing_its_paths_a_few_at_a_time_is_one_upstream_down(scene):
    """Paths go a few at a time: AS64496 first loses the 20 that came through AS64510, a minute later the rest. Its
    first drop could pass for a lost transit, but it's given time to become what it is: an upstream-down event, which
    names both networks that stopped carrying you on from it."""
    import json
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1805, LOST, lambda p: [65001, T4, U2, 64500])
    move(eng, T0 + 1865, PEERS[20:32], lambda p: [65002, T4, U2, 64500])
    eng.tick(T0 + 1800 + 15 * 60)
    assert events(conn) == []
    (down,) = events(conn, "upstream_down")
    assert down["end_ts"] is None and json.loads(down["detail"])["dropped"] == [T1, T2]


def test_an_upstreams_only_transit_dropping_out_with_its_paths_is_the_upstreams_event(tmp_path):
    """All of AS64496's paths come through AS64510; then AS64510 drops out, and AS64496 is left with 6 of its 32 paths
    (through AS64511). It lost what it had: too little left for an upstream-down event, but the upstream shift tells
    it, and a lost transit would say the same thing again."""
    from bgpcanary import config, db
    cfg = dataclasses.replace(config.load(CONFIG), prefixes=[P1], control_prefixes=[], db_path=str(tmp_path / "tl.db"))
    db.init(cfg.db_path)
    conn = db.connect(cfg.db_path)
    only = lambda p: [65001, T1, U1, 64500] if PEERS.index(p) < 32 else route(p)
    eng, heard = start(cfg, conn, {p: only(p) for p in PEERS})
    move(eng, T0 + 1805, PEERS[:32],
         lambda p: [65002, T2, U1, 64500] if PEERS.index(p) < 6 else [65001, T4, U2, 64500])
    eng.tick(T0 + 1800 + 20 * 60)
    assert events(conn) == [] and events(conn, "upstream_down") == []
    assert [e["kind"] for e in events(conn, "upstream_shift")] == ["upstream_shift"]
    conn.close()


def test_an_upstream_that_comes_back_without_one_of_its_transits(scene):
    """The upstream loses everything for 40 minutes (an upstream-down event), then comes back through AS64511 only:
    AS64510 stays gone. Nothing changes in the paths when the upstream-down event ends, and by then the drop is older
    than the half hour the detector looks back: it was held, not forgotten."""
    cfg, conn, eng, heard = scene
    eng.tick(T0 + 3 * 3600)   # (three hours of normal behind it, so what's usual is clear)
    move(eng, T0 + 10805, PEERS[:32], lambda p: [65001, T4, U2, 64500])
    move(eng, T0 + 13205, PEERS[:32], lambda p: [65001, T2, U1, 64500] if p in LOST else route(p))
    eng.tick(T0 + 13800)
    assert events(conn) == [] and events(conn, "upstream_down")[0]["end_ts"] is None
    eng.tick(T0 + 14000)
    (down,) = events(conn, "upstream_down")
    (ev,) = events(conn)
    assert down["end_ts"] is not None and ev["start_ts"] == T0 + 10800   # dated from when it really dropped
    assert ev["title"].endswith("stopped carrying you (20 → 0 RIS peers)")


def test_an_outage_is_reported_as_one(scene):
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1805, PEERS, lambda p: None)
    eng.tick(T0 + 1960)
    assert events(conn) == [] and [e["kind"] for e in events(conn, "outage")] == ["outage"]


def test_one_open_across_a_restart_still_closes(scene):
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1805, LOST, elsewhere)
    eng.tick(T0 + 1900)
    (ev,) = events(conn)
    # a restart: a new engine, the routes as RIS holds them now
    now = {p: (elsewhere(p) if p in LOST else route(p)) for p in PEERS}
    eng2, heard2 = start(cfg, conn, now, at=T0 + 1950)
    assert eng2.anomalies[f"tl|{P1}|{U1}|{T1}"]["start"] == T0 + 1800
    move(eng2, T0 + 2000, LOST, route)
    eng2.tick(T0 + 2700)
    (ev,) = events(conn)
    assert ev["end_ts"] == T0 + 2010 and "stopped carrying you for 4 min" in ev["title"]


def test_one_that_flaps_is_one_event_and_a_new_drop_is_dated_from_itself(scene):
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1805, LOST, elsewhere)
    move(eng, T0 + 1900, LOST, route)
    move(eng, T0 + 2080, LOST, elsewhere)    # gone again three minutes after it came back: the same event
    move(eng, T0 + 2200, LOST, route)
    eng.tick(T0 + 2900)
    (ev,) = events(conn)
    assert (ev["start_ts"], ev["end_ts"]) == (T0 + 1800, T0 + 2220)
    move(eng, T0 + 3605, LOST, elsewhere)    # long after it was over: a new one, from when it dropped this time
    eng.tick(T0 + 3700)
    assert [e["start_ts"] for e in events(conn)] == [T0 + 1800, T0 + 3600]


def test_one_gone_for_a_day_is_taken_as_the_new_normal(scene):
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1805, LOST, elsewhere)
    eng.tick(T0 + 1800 + 86400 - 60)
    assert events(conn)[0]["end_ts"] is None
    eng.tick(T0 + 1800 + 86400 + 60)
    (ev,) = events(conn)
    assert ev["end_ts"] is not None and ev["severity"] == "warning"
    assert ev["title"].endswith("(20 → 0 RIS peers) and hasn't come back in a day: taken as the new normal")
    eng.tick(T0 + 1800 + 2 * 86400)
    assert len(events(conn)) == 1   # (and nothing is reported about it again)


def test_a_burst_of_lost_transits_during_a_gap_fill_is_recorded_once(scene):
    """A gap filler knows what the live engine has open: it records no second event for it."""
    from bgpcanary.engine import for_gap
    cfg, conn, eng, heard = scene
    move(eng, T0 + 1805, LOST, elsewhere)
    eng.tick(T0 + 1900)
    f = for_gap(cfg, conn, eng.peering, eng, T0 + 1900)
    assert f.anomalies[f"tl|{P1}|{U1}|{T1}"]["id"] is None
    f.quiet = True
    for p in PEERS:
        f.apply(T0 + 1900, "rrc00", p, 65000, P1, "A", elsewhere(p) if p in LOST else route(p), source="bgplay-init")
    f.quiet = False
    f.tick(T0 + 2200)
    assert len(events(conn)) == 1


# ------------------------------------------------------------------------------------------------ the pages
def test_the_settings_show_it_on_by_default_and_save_it(web, admin_client):
    from bgpcanary import config, registry, transit
    cfg = web.cfg
    assert (cfg.transit_lost_check, cfg.transit_lost_min_pct, cfg.transit_lost_pct, cfg.transit_lost_warn_minutes) == \
        (True, 10, 25, 5)
    assert config.alert_mode(cfg, "transit_lost") in ("warning", "on")   # (the brief ones don't alert by default)
    page = admin_client.get("/settings/alerts").text
    assert "Transit lost behind an upstream" in page and 'name="t_transit_lost_check" value="1" checked' in page
    assert 'name="t_transit_lost_pct" value="25' in page and "Warning and up" in page
    assert "Transit lost detection" in admin_client.get("/settings/advanced").text

    def form(on):
        f = {"mode_" + t["kind"]: config.alert_mode(cfg, t["kind"]) for t in config.ALERT_TYPES}
        for t in config.ALERT_TYPES:
            for attr in t["tuning"]:
                v = getattr(cfg, attr)
                if isinstance(v, bool):
                    if v:
                        f["t_" + attr] = "1"
                else:
                    f["t_" + attr] = "\n".join(v) if isinstance(v, list) else str(v)
        f.pop("t_transit_lost_check", None)
        if on:
            f["t_transit_lost_check"] = "1"
        for name, mod in (("transit", transit), ("registry", registry)):   # (left as they are)
            if mod.enabled(cfg, web.svc.aconn):
                f["check_" + name] = "1"
        return f
    try:
        r = admin_client.post("/settings/alerts/types", data=form(False), follow_redirects=False)
        assert r.status_code == 303 and cfg.transit_lost_check is False
        r = admin_client.post("/settings/alerts/types", data={**form(True), "t_transit_lost_warn_minutes": "15"},
                              follow_redirects=False)
        assert r.status_code == 303 and cfg.transit_lost_check is True and cfg.transit_lost_warn_minutes == 15
    finally:
        cfg.transit_lost_check, cfg.transit_lost_warn_minutes = True, 5
        web.svc.aconn.execute("DELETE FROM kv WHERE k IN (?, ?)", (config.TUNING_KV, config.ALERT_KV))
        web.svc.aconn.commit()
        cfg.alert_kinds = {}


def test_a_brief_one_shows_on_the_dashboard_and_the_trees_strip(web, admin_client):
    conn = web.svc.aconn
    now = time.time()
    cur = conn.execute("INSERT INTO events(kind, severity, prefix, start_ts, end_ts, title, detail, historical) "
                       "VALUES('transit_lost', 'info', ?, ?, ?, ?, '{}', 0)",
                       (P1, now - 600, now - 480, f"{P1}: AS64510, behind your upstream AS64496, stopped carrying you"))
    conn.commit()
    eid = cur.lastrowid
    try:
        assert "stopped carrying you" in admin_client.get("/").text   # Recent events lists it although it's info
        r = admin_client.get("/api/replay", params={"prefix": P1, "start": now - 3600, "end": now})
        assert eid in [e["id"] for e in r.json()["events"]]          # ...and the strip under the tree marks it
        assert "stopped carrying you" not in admin_client.get("/events").text   # (info: on the Events page on request)
        assert "stopped carrying you" in admin_client.get("/events?info=1").text
    finally:
        conn.execute("DELETE FROM events WHERE id=?", (eid,))
        conn.commit()
