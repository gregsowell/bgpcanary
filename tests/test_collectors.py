"""Route Views collectors that stop working for everyone, replaced by the one that sees your prefixes best
(bgpcanary/collectors.py).

The scene (no network): a scratch service for 192.0.2.0/24 (AS64500) that asks three Route Views collectors, polled
every 12 hours for three weeks. Their tries are rows in rv_snapshots, written by hand. Route Views' telnet
(routeviews.show_bgp_many, routeviews.ping) and its archive (collectors.get) are answered by fakes, and anything that
would reach a real collector fails the test. The collector names are made up, under Route Views' domain, which is how
the archive is found (collectors.archive_dir); the first is Route Views' Cisco collector, whose archive is kept apart."""
import asyncio
import calendar
import dataclasses
import json
import time
from urllib.parse import unquote

import pytest

from conftest import CONFIG, P1

NOW = 1_795_000_000.0                  # 2026-11-18 11:06 UTC
H, DAY = 3600, 86400
MAIN = "route-views.routeviews.org"    # (first: it also runs ping and traceroute)
B, C = "rv-b.routeviews.org", "rv-c.routeviews.org"
D, E, F, G = "rv-d.routeviews.org", "rv-e.routeviews.org", "rv-f.routeviews.org", "rv-g.routeviews.org"
POLLS = [NOW - 12 * H * k for k in range(42, 0, -1)]   # three weeks of 12-hourly snapshots, oldest first


class Archive:
    """Route Views' archive, answering collectors.get by hand. `newest`: each collector's newest file (None: none this
    month or last); one not in it is unknown to the archive. `asked`: every URL asked for."""

    def __init__(self):
        self.newest, self.asked, self.broken = {}, [], False

    @staticmethod
    def _dir(host):
        from bgpcanary import collectors
        return collectors.archive_dir(host)

    async def get(self, url):
        from bgpcanary import collectors
        self.asked.append(url)
        await asyncio.sleep(0)
        if self.broken:
            raise OSError("archive unreachable")
        dirs = {self._dir(h): t for h, t in self.newest.items()}
        if url == collectors.ARCHIVE + "/":
            return 200, "".join(f'<li><A HREF="/{d}/bgpdata"> MRT format</A>' for d in dirs if d != "oix-route-views")
        rest = url[len(collectors.ARCHIVE) + 1:].strip("/").split("/")
        d = rest[0]
        if d not in dirs:
            return 404, "Not Found"
        month = rest[1] if d == "oix-route-views" and len(rest) > 1 else rest[2] if len(rest) > 2 else None
        if month is None:
            return 200, "<a href='2026.10/'>2026.10/</a>"
        t = dirs[d]
        if t is None or month != time.strftime("%Y.%m", time.gmtime(t)):
            return 404, "Not Found"
        files = [t - 900 * k for k in range(4)]   # its last hour
        names = [time.strftime("oix-full-snapshot-%Y-%m-%d-%H%M.bz2" if d == "oix-route-views" else
                               "updates.%Y%m%d.%H%M.bz2", time.gmtime(x)) for x in files]
        return 200, "".join(f'<a href="{n}">{n}</a>' for n in names)


class Telnet:
    """Route Views' collectors over telnet. `paths`: how many paths to 192.0.2.0/24 each one has (an exception: what it
    raises; one not in it never answers). `pings`: the ones that run ping. `asked`: (host, what) for every session."""

    def __init__(self):
        self.paths, self.pings, self.asked = {}, set(), []

    async def show_bgp_many(self, host, prefixes):
        self.asked.append((host, "show"))
        await asyncio.sleep(0)
        n = self.paths.get(host, TimeoutError())
        if isinstance(n, Exception):
            raise n
        plist = [f"{65000 + i} 64496 64500" for i in range(n)]
        return {p: ("\n".join(f"  {x}" for x in plist), plist) for p in prefixes}

    async def ping(self, host, target):
        self.asked.append((host, "ping"))
        await asyncio.sleep(0)
        if host in self.pings:
            return "Success rate is 100 percent (5/5), round-trip min/avg/max = 20/21/23 ms", 100.0, 21.0
        return "% Unknown command", None, None


@pytest.fixture
def archive(monkeypatch):
    from bgpcanary import collectors
    a = Archive()
    a.newest = {MAIN: NOW - 2 * H, B: NOW - 900, C: NOW - 12 * DAY, D: NOW - 900, E: NOW - 900, F: NOW - 900,
                G: NOW - 900}
    monkeypatch.setattr(collectors, "get", a.get)
    return a


@pytest.fixture
def telnet(monkeypatch):
    from bgpcanary import routeviews

    async def real_collector(self):
        raise AssertionError(f"a test tried to reach {self.host}")
    t = Telnet()
    t.paths = {D: 24, E: 10, F: ConnectionRefusedError("refused"), G: 3}
    monkeypatch.setattr(routeviews.Session, "__aenter__", real_collector)
    monkeypatch.setattr(routeviews, "show_bgp_many", t.show_bgp_many)
    monkeypatch.setattr(routeviews, "ping", t.ping)
    return t


@pytest.fixture
def svc(tmp_path, monkeypatch, archive, telnet):
    """A scratch Service for 192.0.2.0/24 asking MAIN, B and C. Building one sets two module globals the seeded app's
    service owns when the whole suite runs, so they're put back afterwards (as in test_gap_list)."""
    from bgpcanary import config, routeviews, tree
    from bgpcanary.service import Service
    monkeypatch.setattr(routeviews, "on_usage", routeviews.on_usage)
    monkeypatch.setattr(tree, "HISTORY", tree.HISTORY)
    s = Service(dataclasses.replace(config.load(CONFIG), prefixes=[P1], control_prefixes=[], rv_configured=[MAIN, B, C],
                                    rv_swaps={}, db_path=str(tmp_path / "rv.db")))
    yield s
    s.conn.close()


def tried(svc, server, times, error=None, n=12):
    """`server` was asked at each of `times`: it answered with `n` paths, or failed with `error`."""
    plist = [f"{65000 + i} 64496 64500" for i in range(n)]
    svc.aconn.executemany("INSERT INTO rv_snapshots(ts,server,prefix,n_paths,paths,raw,event_id,error) "
                          "VALUES(?,?,?,?,?,?,NULL,?)",
                          [(t, server, P1, 0 if error else n, "[]" if error else json.dumps(plist),
                            "" if error else "x", error) for t in times])
    svc.aconn.commit()


def history(svc, dead=C, since=NOW - 18 * DAY, error="TimeoutError"):
    """Three weeks of polls: every collector answered each time, except `dead`, which has failed since `since`."""
    for s in svc.cfg.rv_configured:
        tried(svc, s, [t for t in POLLS if s != dead or t < since])
        if s == dead:
            tried(svc, s, [t for t in POLLS if t >= since], error=error)


def poll(svc, archive, at, dead=C):
    """A round of polls at `at`: every collector asked now answers but `dead`, and the archive has the others' data
    up to then."""
    for s in svc.cfg.rv_servers:
        tried(svc, s, [at], error="TimeoutError" if s == dead else None)
        if s != dead:
            archive.newest[s] = at


def check(svc, at=NOW):
    asyncio.run(svc.collectors.check(at))


def events(svc):
    return [dict(r) for r in svc.aconn.execute("SELECT * FROM events WHERE kind='rv_collector' ORDER BY id")]


# ------------------------------------------------------------------------------------------- replaced
def test_a_collector_gone_for_everyone_is_replaced_by_the_one_that_sees_you_best(svc, archive, telnet):
    from bgpcanary import collectors, db
    from bgpcanary.collectors import Replacer
    history(svc)
    check(svc)
    assert svc.cfg.rv_servers == [MAIN, B, D] and svc.cfg.rv_configured == [MAIN, B, C]
    assert sorted(h for h, _ in telnet.asked) == [D, E, F, G]     # every other collector the archive lists, once
    (ev,) = events(svc)
    assert (ev["severity"], ev["prefix"], ev["start_ts"], ev["end_ts"]) == ("info", None, NOW, NOW)
    assert ev["title"] == f"Route Views collector {C} stopped working; {D} replaces it"
    d = json.loads(ev["detail"])
    assert (d["collector"], d["replacement"], d["paths"], d["tries"]) == (C, D, 24, 36)
    assert d["asked"] == [[D, 24], [E, 10], [G, 3], [F, None]]
    assert f"{C} hasn't answered this server since {collectors.when(NOW - 18 * DAY - 12 * H)} (36 tries over 18 days)" \
        in d["note"]
    assert f"nothing from it since {collectors.when(NOW - 12 * DAY)}, so it's down for everyone" in d["note"]
    assert f"Of the 4 other Route Views collectors asked, 3 answered, and {D} sees the most paths to your prefixes: " \
           f"24 (then {E} 10, {G} 3)" in d["note"]
    # its answer is its first snapshot, filed with the event
    (snap,) = svc.aconn.execute("SELECT * FROM rv_snapshots WHERE server=?", (D,)).fetchall()
    assert (snap["ts"], snap["event_id"], snap["n_paths"], snap["error"]) == (NOW, ev["id"], 24, None)
    # kept on its own, and back after a restart
    assert db.kv_get(svc.aconn, collectors.KV)["swaps"][C]["by"] == D
    svc.cfg.rv_swaps = {}
    Replacer(svc)
    assert svc.cfg.rv_servers == [MAIN, B, D]
    # nothing more to do: it isn't judged again
    telnet.asked.clear()
    check(svc, NOW + 6 * H)
    assert len(events(svc)) == 1 and telnet.asked == []


def test_it_waits_until_every_try_has_failed_for_the_days_set(svc, archive, telnet):
    history(svc, since=NOW - 2.5 * DAY)               # five failed tries over two and a half days: not yet
    check(svc)
    assert events(svc) == [] and archive.asked == [] and telnet.asked == []
    svc.cfg.rv_replace_days = 2
    check(svc)
    assert svc.cfg.rv_servers == [MAIN, B, D]


def test_a_try_the_rate_limiter_turned_away_is_not_a_failure(svc, archive, telnet):
    history(svc, error=f"{C}: rate-limited by Route Views (daily telnet limit for this IP)")
    check(svc)
    assert events(svc) == [] and telnet.asked == []   # its rate limiter answered: it's there


def test_an_answer_with_no_paths_is_an_answer(svc, archive, telnet):
    history(svc)
    tried(svc, C, [NOW - 2.5 * DAY + H], n=0)         # (a collector that has lost your routes still answers)
    check(svc)
    assert events(svc) == [] and telnet.asked == []   # failing since then: two days only


def test_one_route_views_still_hears_from_is_reported_not_replaced(svc, archive, telnet):
    archive.newest[C] = NOW - 1800                    # the archive got data from it half an hour ago
    history(svc)
    check(svc)
    assert svc.cfg.rv_servers == [MAIN, B, C] and telnet.asked == []
    (ev,) = events(svc)
    assert (ev["severity"], ev["end_ts"]) == ("warning", None)
    assert ev["title"] == f"Route Views collector {C} hasn't answered this server for 18 days, but it's working"
    note = json.loads(ev["detail"])["note"]
    assert "Route Views' archive has data from it from 31 minutes ago" in note and f"telnet {C}" in note   # (files
    # are named to the minute)
    check(svc, NOW + 6 * H)                           # told once
    archive.asked.clear()
    check(svc, NOW + 12 * H)
    assert len(events(svc)) == 1 and archive.asked == []   # (and the archive is looked at again once a day)
    telnet.paths[C] = 9                               # it answers again: the warning closes
    asyncio.run(svc.snapshot_server(C, [P1]))
    (ev,) = events(svc)
    assert ev["end_ts"] is not None and svc.collectors.told() == []


def test_if_it_stops_for_everyone_later_it_is_replaced_then(svc, archive, telnet):
    archive.newest[C] = NOW - 1800
    history(svc)
    check(svc)
    poll(svc, archive, NOW + DAY)                     # a day later the archive still has nothing newer from it
    check(svc, NOW + DAY + H)
    warning, notice = events(svc)
    assert warning["end_ts"] == NOW + DAY + H and notice["title"].endswith(f"{D} replaces it")
    assert svc.cfg.rv_servers == [MAIN, B, D] and svc.collectors.told() == []


def test_a_name_the_archive_does_not_know_is_reported_not_replaced(svc, archive, telnet):
    odd = "route-server.example.net"
    svc.cfg.rv_servers = [MAIN, B, odd]
    history(svc, dead=odd)
    check(svc)
    (ev,) = events(svc)
    assert ev["severity"] == "warning" and "isn't one of the collectors Route Views' archive lists" in \
        json.loads(ev["detail"])["note"]
    assert svc.cfg.rv_servers == [MAIN, B, odd] and telnet.asked == []


def test_every_collector_failing_at_once_is_this_servers_trouble(svc, archive, telnet):
    for s in (MAIN, B, C):                            # (the last answers four days ago)
        tried(svc, s, [t for t in POLLS if t <= NOW - 4 * DAY])
        tried(svc, s, [t for t in POLLS if t > NOW - 4 * DAY], error="TimeoutError")
    check(svc)
    (ev,) = events(svc)
    assert ev["title"] == "None of the 3 Route Views collectors has answered this server for 4 days"
    assert ev["severity"] == "warning" and ev["end_ts"] is None
    assert archive.asked == [] and telnet.asked == [] and svc.cfg.rv_servers == [MAIN, B, C]
    check(svc, NOW + 6 * H)
    assert len(events(svc)) == 1
    telnet.paths[B] = 12                              # one answers again: it was this side
    asyncio.run(svc.snapshot_server(B, [P1]))
    assert events(svc)[0]["end_ts"] is not None


def test_nothing_is_judged_while_the_others_do_not_answer_either(svc, archive, telnet):
    history(svc)
    tried(svc, B, [t for t in POLLS if t >= NOW - DAY], error="TimeoutError")
    svc.aconn.execute("DELETE FROM rv_snapshots WHERE server=? AND ts>=? AND error IS NULL", (B, NOW - DAY))
    svc.aconn.execute("DELETE FROM rv_snapshots WHERE server=? AND ts>=? AND error IS NULL", (MAIN, NOW - DAY))
    svc.aconn.commit()
    check(svc)
    assert events(svc) == [] and archive.asked == []


def test_nothing_is_judged_while_the_archive_is_behind(svc, archive, telnet):
    archive.newest[MAIN] = NOW - 10 * H               # nothing recent from the first one, which answers here
    history(svc)
    check(svc)
    assert events(svc) == [] and telnet.asked == []
    archive.newest[MAIN] = NOW - 2 * H
    archive.broken = True                             # nor while it can't be read
    check(svc)
    assert events(svc) == [] and telnet.asked == []


def test_none_seeing_enough_of_your_paths_drops_it(svc, archive, telnet):
    telnet.paths = {D: 4, E: 0, G: 3}
    history(svc)
    check(svc)
    assert svc.cfg.rv_servers == [MAIN, B]
    (ev,) = events(svc)
    assert ev["title"] == (f"Route Views collector {C} stopped working; dropped, as no other collector sees enough of "
                           f"your paths")
    assert f"none sees at least 5 paths to your prefixes (the most: {D}, 4)" in json.loads(ev["detail"])["note"]


def test_no_collector_answering_the_search_changes_nothing(svc, archive, telnet):
    telnet.paths = {}
    history(svc)
    check(svc)
    assert events(svc) == [] and svc.cfg.rv_servers == [MAIN, B, C] and len(telnet.asked) == 4
    check(svc, NOW + 6 * H)                           # (tried again a day later, not at every check)
    assert len(telnet.asked) == 4


def test_the_first_ones_replacement_must_run_ping(svc, archive, telnet):
    svc.cfg.probe_targets = ["192.0.2.1"]
    archive.newest[MAIN] = NOW - 12 * DAY
    history(svc, dead=MAIN)
    telnet.pings = {E}
    check(svc)
    assert svc.cfg.rv_servers == [E, B, C]            # the best one that pings, in its place
    assert (D, "ping") in telnet.asked and json.loads(events(svc)[0]["detail"])["pings"] is True


def test_without_one_that_pings_the_best_one_takes_the_first_place_anyway(svc, archive, telnet):
    svc.cfg.probe_targets = ["192.0.2.1"]
    archive.newest[MAIN] = NOW - 12 * DAY
    history(svc, dead=MAIN)
    check(svc)
    assert svc.cfg.rv_servers == [D, B, C]
    assert "It doesn't answer ping" in json.loads(events(svc)[0]["detail"])["note"]


# ------------------------------------------------------------------------------------------- your list stays in charge
def test_undo_puts_it_back_until_it_has_answered_again(svc, archive, telnet):
    history(svc)
    check(svc)
    assert svc.collectors.undo(C)["by"] == D and svc.cfg.rv_servers == [MAIN, B, C]
    tried(svc, C, [NOW + 12 * H], error="TimeoutError")
    check(svc, NOW + DAY)
    assert len(events(svc)) == 1 and svc.cfg.rv_servers == [MAIN, B, C]   # not replaced again
    assert svc.collectors.undo(C) is None


def test_a_list_without_the_dead_one_drops_its_swap(svc, archive, telnet):
    history(svc)
    check(svc)
    svc.cfg.rv_servers = [MAIN, B, E]                 # (Settings saved, or the config file edited, and restarted)
    check(svc, NOW + 6 * H)
    assert svc.cfg.rv_swaps == {} and svc.cfg.rv_servers == [MAIN, B, E]


def test_a_replacement_that_stops_working_too_is_replaced_in_turn(svc, archive, telnet):
    history(svc)
    check(svc)
    later = NOW + 5 * DAY
    tried(svc, D, [NOW + 12 * H * k for k in range(1, 11)], error="TimeoutError")
    for s in (MAIN, B):
        tried(svc, s, [NOW + 12 * H * k for k in range(1, 11)])
    archive.newest.update({D: NOW, MAIN: later - H, B: later - H})
    check(svc, later)
    assert svc.cfg.rv_servers == [MAIN, B, E] and set(svc.cfg.rv_swaps) == {C, D}
    check(svc, later + 6 * H)                         # (the first swap isn't forgotten: it leads to the second)
    assert svc.cfg.rv_servers == [MAIN, B, E]


def test_switched_off_it_replaces_nothing_and_closes_its_warnings(svc, archive, telnet):
    archive.newest[C] = NOW - 1800
    history(svc)
    check(svc)
    assert events(svc)[0]["end_ts"] is None
    svc.cfg.rv_replace_dead = False
    archive.newest[C] = NOW - 12 * DAY
    check(svc, NOW + 2 * DAY)
    assert events(svc)[0]["end_ts"] == NOW + 2 * DAY and svc.cfg.rv_servers == [MAIN, B, C] and telnet.asked == []


def test_the_archive_is_read_as_route_views_lays_it_out():
    from bgpcanary import collectors
    assert collectors.archive_dir("route-views.routeviews.org") == "oix-route-views"
    assert collectors.archive_dir("Route-Views.SG.routeviews.org.") == "route-views.sg"
    assert collectors.archive_dir("route-views2.oregon-ix.net") == "route-views2"
    assert collectors.archive_dir("route-server.example.net") is None
    assert collectors._months(calendar.timegm((2027, 1, 3, 0, 0, 0))) == ["2027.01", "2026.12"]
    m = [collectors._file_ts(x) for x in collectors.FILE_RE.finditer(
        'href="updates.20261107.0045.bz2" href="oix-full-snapshot-2026-11-24-1800.bz2"')]
    assert m == [calendar.timegm((2026, 11, 7, 0, 45, 0)), calendar.timegm((2026, 11, 24, 18, 0, 0))]


# ------------------------------------------------------------------------------------------- the pages
def test_settings_show_the_swap_with_an_undo_and_the_pages_count_only_the_collectors_asked(web, admin_client):
    from bgpcanary import config, web as app
    cfg, svc = web.cfg, web.svc
    conn = svc.aconn
    now = time.time()
    rows = [(now - 60, MAIN, 12, None), (now - 60, D, 24, None), (now - 30 * DAY, C, 21, None)]
    ids = [conn.execute("INSERT INTO rv_snapshots(ts,server,prefix,n_paths,paths,raw,event_id,error) "
                        "VALUES(?,?,?,?,?,'x',NULL,?)",
                        (t, s, P1, n, json.dumps([f"{65000 + i} 64496 64500" for i in range(n)]), e)).lastrowid
           for t, s, n, e in rows]
    conn.commit()
    svc.collectors.st["swaps"][C] = {"by": D, "at": now - 3600, "event": None, "paths": 24}
    cfg.rv_servers = [MAIN, C]
    svc.collectors._apply()
    svc.health._cache = (0, None)
    try:
        assert cfg.rv_servers == [MAIN, D]
        page = admin_client.get("/settings/advanced").text
        assert f"{MAIN}\n{C}</textarea>" in page                       # the list as configured...
        assert "Replaced automatically" in page and f"<strong>{D}</strong>" in page and 'form="undo-rv-1"' in page
        assert "Replace a Route Views collector that stops working for everyone" in page
        assert "Route Views collector stopped working" in admin_client.get("/settings/alerts").text
        assert f"replaced automatically: {C} → {D}" in admin_client.get("/").text   # (the data-sources panel)
        assert set(app.rv_latest(conn, P1)[0]) == {MAIN, D}             # the old one's last view no longer counts
        r = admin_client.post("/settings/advanced/undo-replacement", data={"host": C}, follow_redirects=False)
        assert r.status_code == 303 and "is asked again instead of" in unquote(r.headers["location"])
        assert cfg.rv_servers == [MAIN, C] and C in svc.collectors.st["kept"]
        r = admin_client.post("/settings/advanced/undo-replacement", data={"host": C}, follow_redirects=False)
        assert "err=1" in r.headers["location"]
    finally:
        cfg.rv_servers = []
        svc.collectors.st = {"swaps": {}, "kept": {}, "told": {}}
        svc.collectors._save()
        conn.execute(f"DELETE FROM rv_snapshots WHERE id IN ({','.join('?' * len(ids))})", ids)
        conn.execute("DELETE FROM kv WHERE k=?", (config.TUNING_KV,))
        conn.commit()
        svc.health._cache = (0, None)
