"""An answer from RIPE's archive that isn't all of it isn't taken for all of it.

The service asks RIPEstat's BGPlay about each prefix by itself (Service._bgplay_merged: the monitored prefixes, at a
start the control prefixes too) and makes one answer of what comes back. A request can fail, three times over, while
the next prefix's goes through. What came back then used to be passed on as the whole answer, and whoever read it saw
the prefix that was missing as one no RIS peer has a route to:
- the daily check against RIS's snapshot withdrew every route of it from the live engine: an outage that wasn't;
- a gap fill replayed the gap with no routes for it: zeroes on its chart and a "(backfill)" outage that never was;
- a restart recorded every route in its stored history as withdrawn, right after its last update;
- the one-time store of the first routes (older databases) left it out, for good.
And a replay carried on past a week it got no answer for, on the routes as they were before it (or on none at all).

The rule these tests hold: a replay needs every prefix's answer, so one that's missing makes it no answer and the
replay stops there, for whoever started it to come back for the rest (the list of gaps). A snapshot of the routes is
used prefix by prefix: the ones that answered are seeded and checked, the others are left exactly as they were.

The services here are scratch ones on their own databases (not the seeded app's), with BGPlay answered by hand:
no network."""
import asyncio
import dataclasses
import time

import pytest

from conftest import CONFIG, P1, P2
from test_gap_fill import PEERS, ROUTES, STRAY, T0, events, incident, iso, kinds
from test_gap_list import EVERYTHING, GAP, WEEK, gaps, one_pass, outages, stored_twice
from test_gap_list import no_waiting   # noqa: F401  (a fixture: the service's pauses pass at once)

CONTROL = "203.0.113.0/24"             # another network's prefix, watched as a check on RIS itself (a control prefix)
ELSEWHERE = {p: [64510, 64505] for p in PEERS}   # ...and the routes RIS holds for it: AS64505's, not through you
HOUR, DAY = 3600, 86400
SIDE = [64511, 64497, 64500]           # a path through one of the expected upstreams


def answer(prefix, start, end, changes=(), routes=ROUTES):
    """RIPEstat's BGPlay answer for `prefix` over [start, end): its routes at the start, then the changes inside."""
    src = lambda p: f"00-{p}"
    return {"query_starttime": iso(start), "query_endtime": iso(end),
            "sources": [{"id": src(p), "as_number": 64510} for p in PEERS + [STRAY]],
            "initial_state": [{"source_id": src(p), "target_prefix": prefix, "path": path}
                              for p, path in routes.items()],
            "events": [{"type": "A" if path else "W", "timestamp": iso(t),
                        "attrs": {"source_id": src(p), "target_prefix": prefix, **({"path": path} if path else {})}}
                       for t, p, path in changes if start <= t < end]}


class Archive:
    """RIPEstat's BGPlay, answering ripe.bgplay by hand: one prefix per request, as the service asks. `changes`: what
    happened, per prefix (as incident() lists it). `routes`: what RIS holds for a prefix when that isn't the 20 usual
    routes. `upto`: how far its data reaches; a request past that is answered as RIPE does, with the times moved back
    to it. `asked`: every request, as (prefix, start, end). `broken`: says for which requests no answer comes."""

    def __init__(self):
        self.changes, self.routes, self.upto, self.asked, self.broken = {}, {}, float("inf"), [], None

    async def bgplay(self, prefix, start, end, sourceapp):
        self.asked.append((prefix, start, end))
        await asyncio.sleep(0)             # the service waits for the answer here
        if self.broken and self.broken(prefix, start, end):
            raise OSError("timed out")
        return answer(prefix, min(start, self.upto), min(end, self.upto), self.changes.get(prefix, ()),
                      self.routes.get(prefix, ROUTES))

    def replays(self, prefix=P1):
        """The stretches of `prefix`'s history that were asked for, each once however often the service tried (not
        the one-minute looks at how things stand)."""
        return list(dict.fromkeys((s, e) for p, s, e in self.asked if p == prefix and round(e - s, 3) != 60))


@pytest.fixture
def archive(monkeypatch):
    from bgpcanary import ripe
    a = Archive()
    monkeypatch.setattr(ripe, "bgplay", a.bgplay)
    return a


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Makes scratch Services: scratch(name, **settings) is one on the database of that name, monitoring both
    prefixes unless `prefixes` says otherwise, with no control prefixes unless `control_prefixes` names some. Asking
    for a name again is the service starting again on the same database. Building a Service sets two module globals
    (routeviews.on_usage and tree.HISTORY: the seeded app's service owns them when the whole suite runs), so they're
    put back afterwards."""
    from bgpcanary import config, routeviews, tree
    from bgpcanary.service import Service
    monkeypatch.setattr(routeviews, "on_usage", routeviews.on_usage)
    monkeypatch.setattr(tree, "HISTORY", tree.HISTORY)
    made = []

    def make(name="one", **settings):
        cfg = dataclasses.replace(config.load(CONFIG), **{"prefixes": [P1, P2], "control_prefixes": [],
                                                           "db_path": str(tmp_path / f"{name}.db"), **settings})
        made.append(Service(cfg))
        return made[-1]

    yield make
    for svc in made:
        svc.conn.close()


@pytest.fixture
def one_day(monkeypatch):
    """The service's pauses pass at once, and it stops when it begins to wait for a second day: one round of what it
    does daily. (Not together with no_waiting: asked for after this one, it passes a day's wait on as no wait at
    all, and the rounds never end.) It's the list of the days it has seen begin: emptied, there's another round."""
    sleep, days = asyncio.sleep, []

    async def passing(delay, result=None):
        if delay >= DAY:
            days.append(delay)
            if len(days) > 1:
                raise asyncio.CancelledError
        return await sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", passing)
    return days


def hold(svc, since, until=None):
    """The live engine holds RIS's 20 routes for each of its prefixes since `since` (as seeded at a start), and has
    watched them, unchanged, until `until`."""
    for p in list(svc.cfg.prefixes) + list(svc.engine.controls):
        svc.engine.load_bgplay(answer(p, since, since, routes=ELSEWHERE if p in svc.engine.controls else ROUTES),
                               True, False)
    svc.engine.last_bucket = since
    if until:
        svc.engine.tick(until)


def stale(svc):
    """The routes recorded as gone without a withdrawal (source 'stale'), as (prefix, peer)."""
    return [(r["prefix"], r["peer"]) for r in svc.conn.execute(
        "SELECT prefix, peer FROM updates WHERE source='stale' ORDER BY prefix, peer")]


def lowest(svc, prefix, start, end):
    """The fewest RIS peers the chart shows for `prefix` between `start` and `end`."""
    return svc.conn.execute("SELECT MIN(visible) FROM buckets WHERE prefix=? AND ts>=? AND ts<?",
                            (prefix, start, end)).fetchone()[0]


def recent():
    """(now, a time six hours ago on a bucket's edge): the daily check and a start go by the clock."""
    now = time.time()
    return now, int(now // 30 * 30) - 6 * HOUR


# ------------------------------------------------------------------------------------------------ RIS's snapshot
def test_the_daily_check_leaves_a_prefix_alone_when_its_snapshot_didnt_arrive(scratch, archive, one_day):
    """Once a day the live routes are checked against RIS's snapshot (BGPlay's newest state, a few hours old), and
    a route it no longer lists is withdrawn. The request for the second prefix fails: its 20 routes were withdrawn,
    all of them, and with the next bucket that was an outage."""
    svc, (now, since) = scratch(), recent()
    hold(svc, since)
    svc.engine.apply(since, "rrc00", STRAY, 64510, P1, "A", SIDE)   # a route RIS dropped without saying so
    svc.engine.tick(now - 30)
    archive.upto, archive.broken = now - 3 * HOUR, lambda prefix, start, end: prefix == P2
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc._snapshot_reconcile_loop())
    assert stale(svc) == [(P1, STRAY)]                    # the prefix that was answered for is checked as before
    assert (svc.engine.visible(P1), svc.engine.visible(P2)) == (20, 20)
    svc.engine.tick(time.time() + 120)                    # ...and two minutes on
    assert events(svc.conn) == []


def test_a_snapshot_that_lists_none_of_a_prefixs_routes_is_believed(scratch, archive, one_day):
    """The other side of it, which stays as it was: when the snapshot did arrive and the routes aren't in it, they go,
    however many they are. A prefix withdrawn everywhere while RIS Live was down leaves the live engine holding all
    of its routes, and the looking glass may not withdraw that many at once (LG_MAX_WITHDRAW). This check is what
    tells the live engine; the outage that follows is checked against Route Views before it's alerted."""
    svc, (now, since) = scratch(), recent()
    hold(svc, since, now - 30)
    archive.upto, archive.routes[P2] = now - 3 * HOUR, {}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc._snapshot_reconcile_loop())
    assert stale(svc) == [(P2, p) for p in sorted(PEERS)]
    assert (svc.engine.visible(P1), svc.engine.visible(P2)) == (20, 0)


@pytest.mark.parametrize("unanswered", [P1, CONTROL])
def test_a_restart_doesnt_rewrite_the_history_of_a_prefix_it_got_no_snapshot_for(scratch, archive, no_waiting,
                                                                                 unanswered):
    """At a start the routes are seeded from RIS's snapshot: the monitored prefixes' and the control prefixes' in
    one answer. Then the stored history is checked against them: a route it still shows that RIS no longer has is
    recorded as withdrawn right after its last update. When the request for a monitored prefix failed and another
    went through (a control prefix's is enough), that was every route the prefix has, each withdrawn hours or weeks
    ago as far as the history pages could tell."""
    (now, since), was = recent(), scratch("restart", control_prefixes=[CONTROL])
    hold(was, since)
    was.engine.apply(since, "rrc00", STRAY, 64510, P2, "A", SIDE)   # a route RIS dropped without saying so
    was.engine.tick(now - 60)                                       # six hours of history, then it stops
    was.conn.commit()
    was.conn.close()
    svc = scratch("restart", control_prefixes=[CONTROL])
    archive.upto, archive.broken = now - 3 * HOUR, lambda prefix, start, end: prefix == unanswered
    archive.routes[CONTROL] = ELSEWHERE
    asyncio.run(svc._startup_history())
    assert stale(svc) == [(P2, STRAY)]                    # what was answered for is checked as before
    seeded = {p: svc.engine.seen(p) for p in (P1, P2, CONTROL)}
    assert seeded == {P1: 20, P2: 20, CONTROL: 20, unanswered: 0}   # the rest waits for the looking glass


def test_the_first_routes_arent_stored_from_some_of_the_prefixes(scratch, archive, no_waiting):
    """A database from before the routes at the start of the history were stored gets them once
    (Service._ensure_init_rows). With one prefix unanswered that was done for the others and filed as done."""
    from bgpcanary import db
    svc = scratch()
    svc.engine.last_bucket = T0
    svc.engine.tick(T0 + 300)                             # (a history, and nothing stored of how it began)
    first = lambda: [r[0] for r in svc.conn.execute("SELECT COUNT(*) FROM updates WHERE source='bgplay-init' "
                                                    "GROUP BY prefix ORDER BY prefix")]
    archive.broken = lambda prefix, start, end: prefix == P2
    asyncio.run(svc._ensure_init_rows())
    assert first() == [] and not db.kv_get(svc.conn, "init_rows_v2")   # nothing yet: the next start tries again
    archive.broken = None
    asyncio.run(svc._ensure_init_rows())
    assert first() == [20, 20] and db.kv_get(svc.conn, "init_rows_v2")


# ------------------------------------------------------------------------------------------------ replays
def test_a_gap_isnt_filled_from_some_of_the_prefixes(scratch, archive, no_waiting):
    """RIS Live was down for 35 minutes, and the archive covers them. The request for the second prefix's history
    fails: nothing happened to that prefix, and the fill recorded an outage of all 35 minutes for it, drew it on the
    chart, and took the gap off the list."""
    svc = scratch()
    hold(svc, T0, T0 + 2400)
    svc._add_gap(*GAP)
    archive.changes[P1], archive.broken = incident(), lambda prefix, start, end: (prefix, start, end) == (P2, *GAP)
    one_pass(svc)
    assert gaps(svc) == [GAP] and events(svc.conn) == []  # no fill from half an answer: the gap waits
    assert lowest(svc, P2, *GAP) == 20
    archive.broken = None
    one_pass(svc)                                         # half an hour later
    assert gaps(svc) == [] and kinds(svc.conn) == EVERYTHING and stored_twice(svc) == 0
    assert {e["prefix"] for e in events(svc.conn)} == {P1} and outages(svc) == [(900, 1140), (1800, 1980)]
    assert (lowest(svc, P1, *GAP), lowest(svc, P2, *GAP)) == (0, 20)


def test_a_replay_stops_at_a_week_that_didnt_arrive(scratch, archive, no_waiting):
    """History is asked for a week at a time. A replay went on past a week it got no answer for: the weeks after it
    were replayed on the routes as they were before it, what happened in it was never recorded, and the stretch
    counted as loaded all the same. Here a gap of 15 days (the service was off that long), with everything happening
    in the second week, which is the one that doesn't arrive."""
    svc, start = scratch(prefixes=[P1]), T0 + 300
    weeks = [(start, start + WEEK), (start + WEEK, start + 2 * WEEK), (start + 2 * WEEK, start + 15 * DAY)]
    hold(svc, T0, start)
    svc._add_gap(start, start + 15 * DAY)
    archive.changes[P1] = [(t + WEEK, peer, path) for t, peer, path in incident()]
    archive.broken = lambda prefix, s, e: (s, e) == weeks[1]
    one_pass(svc)
    assert archive.replays() == weeks[:2]                 # the third week wasn't asked for
    assert gaps(svc) == [(start + WEEK, start + 15 * DAY)] and events(svc.conn) == []
    archive.broken = None
    one_pass(svc)                                         # half an hour later
    assert gaps(svc) == [] and kinds(svc.conn) == EVERYTHING and stored_twice(svc) == 0
    assert outages(svc) == [(WEEK + 900, WEEK + 1140), (WEEK + 1800, WEEK + 1980)]


def test_a_first_start_carries_on_with_the_history_it_got(scratch, archive, no_waiting):
    """A new install loads its history before it goes live, here 15 days of it, and the second week doesn't arrive.
    The load ends there instead of going on without it, the start carries on, and the rest is noted as a gap like any
    other: the gap filler loads it once the archive answers (and covers the present: it runs hours behind)."""
    svc, before = scratch(prefixes=[P1], backfill_days=15), time.time()
    begins = before - 15 * DAY
    archive.upto = before - 3 * HOUR
    archive.broken = lambda prefix, s, e: abs(s - (begins + WEEK)) < 60 and e - s > 60
    asyncio.run(svc._startup_history())
    assert len(archive.replays()) == 2                    # the first week, the one that didn't arrive; no third
    (gap,) = gaps(svc)
    assert gap[0] == pytest.approx(begins + WEEK, abs=5) and gap[1] == pytest.approx(time.time(), abs=60)
    assert events(svc.conn) == [] and svc.engine.visible(P1) == 20
    assert lowest(svc, P1, begins, begins + WEEK) == 20 and lowest(svc, P1, begins + WEEK, before) is None


def test_a_new_prefixs_history_that_didnt_arrive_is_left_to_the_gap_filler(scratch, archive, no_waiting):
    """A prefix added later gets its history in the background, in an engine of its own. Here its first week doesn't
    arrive: the load ends before it began (the later weeks were replayed on no routes at all), and from there to the
    start of live monitoring is noted as a gap."""
    svc, before = scratch(backfill_days=15), time.time()
    begins = before - 15 * DAY
    svc.live_since, svc.pending_backfill = before, [P2]
    archive.upto = before - 3 * HOUR
    archive.broken = lambda prefix, s, e: abs(s - begins) < 60 and e - s > 60
    asyncio.run(svc._backfill_new_prefixes())
    assert svc.pending_backfill == [] and len(archive.replays(P2)) == 1 and archive.replays(P1) == []
    (gap,) = gaps(svc)
    assert gap[0] == pytest.approx(begins, abs=5) and gap[1] == before
    assert lowest(svc, P2, begins, before) is None and events(svc.conn) == []
