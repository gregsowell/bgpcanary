"""The list of gaps: what the gap filler still has to fill, kept right while RIS Live keeps dropping.

Each time RIS Live comes back from a drop the service notes the gap (Service._add_gap, the kv entry `gaps`), and every
half hour a pass of Service._fill_gaps replays the gaps RIPE's archive covers by then. A pass read the list once,
worked for a second or so per gap, and wrote back what was left of its own copy at the very end. So a gap noted while
it worked was overwritten and never filled, and a pass that was cut short (the service stopping, an error) had saved
nothing: the next one replayed the gaps it had filled all over again. These tests hold the list to what's true at
every moment: a gap leaves it when it has been filled, as far as it has been filled, and for no other reason.

The last two are about something else that's the live engine's alone: saying that the tracking limit is reached.

The services here are scratch ones on their own databases (not the seeded app's), with BGPlay answered by hand:
no network."""
import asyncio
import dataclasses

import pytest

from conftest import CONFIG, P1
from test_gap_fill import PEERS, T0, bgplay, events, incident, kinds, watch
from test_gap_fill import monitor   # noqa: F401  (a fixture: a scratch live engine)

GAP = (T0 + 300, T0 + 2400)            # RIS Live was down for these 35 minutes: all of incident() happened meanwhile
WEEK = 7 * 86400                       # history is asked for a week at a time (Service._replay)
EVERYTHING = ["blip", "blip", "chain_change", "outage", "outage", "unexpected_upstream"]   # what incident() holds


class Archive:
    """RIPEstat's BGPlay, answering ripe.bgplay by hand. `changes`: what happened (as incident() lists it). `upto`:
    how far its data reaches (it runs hours behind). `asked`: every request, as (start, end). `on_request`: called
    with (start, end) while a request is being answered, for what else happens on the event loop meanwhile.
    `broken`, `garbled`: say for which requests (start, end) no answer comes, or one that can't be read."""

    def __init__(self):
        self.changes, self.upto, self.asked = [], float("inf"), []
        self.on_request = self.broken = self.garbled = None

    async def bgplay(self, prefix, start, end, sourceapp):
        self.asked.append((start, end))
        if self.on_request:
            self.on_request(start, end)
        await asyncio.sleep(0)             # the service waits for the answer here: whatever else is due runs
        if self.broken and self.broken(start, end):
            raise OSError("timed out")
        if self.garbled and self.garbled(start, end):
            return {"events": []}
        return bgplay(start, min(end, self.upto), self.changes)

    def replays(self):
        """The stretches whose history was asked for (not the one-minute probes of whether a gap is covered yet)."""
        return [(s, e) for s, e in self.asked if round(e - s, 3) != 60]


@pytest.fixture
def archive(monkeypatch):
    from bgpcanary import ripe
    a = Archive()
    monkeypatch.setattr(ripe, "bgplay", a.bgplay)
    return a


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A scratch Service for 192.0.2.0/24 on a fresh database, its engine live since T0 with the 20 routes. Building a
    Service sets two module globals (routeviews.on_usage and tree.HISTORY: the seeded app's service owns them when
    the whole suite runs), so they're put back afterwards."""
    from bgpcanary import config, routeviews, tree
    from bgpcanary.service import Service
    monkeypatch.setattr(routeviews, "on_usage", routeviews.on_usage)
    monkeypatch.setattr(tree, "HISTORY", tree.HISTORY)
    svc = Service(dataclasses.replace(config.load(CONFIG), prefixes=[P1], control_prefixes=[],
                                      db_path=str(tmp_path / "gaps.db")))
    svc.engine.load_bgplay(bgplay(T0, T0), True, False)   # as at a start: the routes RIS holds, no history
    svc.engine.last_bucket = T0
    yield svc
    svc.conn.close()


@pytest.fixture
def no_waiting(monkeypatch):
    """The service's pauses (ten seconds before it asks BGPlay again) pass at once."""
    sleep = asyncio.sleep

    async def at_once(delay, result=None):
        return await sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", at_once)


def down(svc, start, end):
    """RIS Live was down from `start` to `end`: the live engine saw nothing of it, and the gap is noted."""
    watch(svc.engine, end)
    svc._add_gap(start, end)


def one_pass(svc):
    """A pass of the gap filler, as Service._gap_loop starts one every half hour."""
    asyncio.run(svc._fill_gaps())


def gaps(svc):
    from bgpcanary import db
    return [tuple(g) for g in db.kv_get(svc.conn, "gaps", [])]


def outages(svc):
    return [(e["start_ts"] - T0, e["end_ts"] - T0) for e in events(svc.conn) if e["kind"] == "outage"]


def stored_twice(svc):
    """How many of the updates the replays stored are in the database more than once."""
    return svc.conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM updates WHERE source LIKE 'bgplay%' GROUP BY ts, "
                            "source, collector, peer, prefix, kind, path HAVING COUNT(*) > 1)").fetchone()[0]


# ------------------------------------------------------------------------------------------------ filled, or not yet
def test_a_gap_waits_for_ripes_archive_then_leaves_the_list(service, archive):
    svc, gap = service, (T0 + 300, T0 + 2400.75)
    down(svc, *gap)
    archive.changes, archive.upto = incident(), T0 + 2399
    one_pass(svc)
    assert gaps(svc) == [gap] and archive.replays() == [] and events(svc.conn) == []   # not covered yet
    archive.upto = T0 + 2400               # BGPlay answers in whole seconds: this covers a gap that ends at 2400.75
    one_pass(svc)
    assert gaps(svc) == []                 # ...all of it: no three quarters of a second left to ask for forever
    assert kinds(svc.conn) == EVERYTHING and outages(svc) == [(900, 1140), (1800, 1980)]
    one_pass(svc)
    assert archive.replays() == [gap]      # nothing left to do


def test_a_gap_noted_while_the_filler_works_isnt_lost(service, archive):
    """RIS Live drops again and comes back while a pass is under way: on a bad day that's every few minutes, and a
    pass takes a second per gap. The pass saved its own, older copy of the list over the new gap."""
    svc, again = service, (T0 + 4000.5, T0 + 4030.25)
    down(svc, *GAP)
    archive.changes = incident()

    def reconnect(start, end):             # ...while BGPlay is asked for the first gap's history
        if (start, end) == GAP and again not in gaps(svc):
            svc._add_gap(*again)

    archive.on_request = reconnect
    one_pass(svc)
    assert gaps(svc) == [again]
    assert kinds(svc.conn) == EVERYTHING   # (and the first gap was filled)


def test_a_gap_that_grew_while_it_was_filled_keeps_the_rest(service, archive):
    """A drop within a minute of the last one makes one gap of the two (_add_gap). When that happens to the gap that's
    being filled, what was filled leaves the list and the new part stays."""
    svc, first, more = service, (T0 + 300, T0 + 1500), (T0 + 1530, T0 + 2400)
    watch(svc.engine, T0 + 2400)
    svc._add_gap(*first)
    archive.changes = incident()

    def reconnect(start, end):
        if (start, end) == first and gaps(svc) == [first]:
            svc._add_gap(*more)
            assert gaps(svc) == [(T0 + 300, T0 + 2400)]

    archive.on_request = reconnect
    one_pass(svc)
    assert gaps(svc) == [(T0 + 1500, T0 + 2400)]
    assert outages(svc) == [(900, 1140)]
    one_pass(svc)                          # half an hour later
    assert gaps(svc) == []
    assert outages(svc) == [(900, 1140), (1800, 1980)] and kinds(svc.conn) == EVERYTHING
    assert archive.replays() == [first, (T0 + 1500, T0 + 2400)] and stored_twice(svc) == 0


@pytest.mark.parametrize("how", ["the service stops", "an answer can't be read"])
def test_a_pass_cut_short_doesnt_fill_a_gap_again(service, archive, how):
    """Two gaps, and the pass ends in the middle of the second. The first was filled: it's out of the list by then,
    so the next pass (or the next start) doesn't replay it again and store its updates a second time."""
    svc, one, two = service, (T0 + 300, T0 + 1500), (T0 + 1700, T0 + 2400)
    watch(svc.engine, T0 + 2400)
    svc._add_gap(*one)
    svc._add_gap(*two)
    archive.changes = incident()
    if how == "the service stops":         # Service.stop() cancels its tasks: this one while it waits for BGPlay
        archive.on_request = lambda start, end: (start, end) == two and asyncio.current_task().cancel()
    else:
        archive.garbled = lambda start, end: (start, end) == two
    with pytest.raises((asyncio.CancelledError, KeyError)):
        one_pass(svc)
    assert gaps(svc) == [two]
    assert outages(svc) == [(900, 1140)]
    archive.on_request = archive.garbled = None
    one_pass(svc)
    assert gaps(svc) == []
    assert archive.replays() == [one, two, two] and stored_twice(svc) == 0
    assert outages(svc) == [(900, 1140), (1800, 1980)] and kinds(svc.conn) == EVERYTHING


# ------------------------------------------------------------------------------------------------ filled as far as...
def test_a_gap_whose_history_didnt_arrive_stays_in_the_list(service, archive, no_waiting):
    """BGPlay says the gap is covered, then the request for the gap itself fails, three times over. Nothing was
    replayed, so nothing was filled: it used to leave the list all the same, for good."""
    svc = service
    down(svc, *GAP)
    archive.changes, archive.broken = incident(), lambda start, end: (start, end) == GAP
    one_pass(svc)
    assert gaps(svc) == [GAP] and events(svc.conn) == []
    archive.broken = None
    one_pass(svc)
    assert gaps(svc) == [] and kinds(svc.conn) == EVERYTHING and stored_twice(svc) == 0


def test_a_replay_that_got_part_of_the_way_leaves_the_rest(service, archive, no_waiting):
    """A gap of eight days (the service was off that long) takes two requests, and the second fails: the first week
    is filled and leaves the list, the eighth day stays for the next pass."""
    svc, gap = service, (T0 + 300, T0 + 300 + 8 * 86400)
    watch(svc.engine, T0 + 300)            # (then it was off: no buckets of its own in the gap)
    svc._add_gap(*gap)
    archive.changes, archive.broken = incident(), lambda start, end: start == T0 + 300 + WEEK
    one_pass(svc)
    assert gaps(svc) == [(T0 + 300 + WEEK, gap[1])]
    assert kinds(svc.conn) == EVERYTHING
    archive.broken = None
    one_pass(svc)
    assert gaps(svc) == [] and kinds(svc.conn) == EVERYTHING and stored_twice(svc) == 0
    assert archive.replays()[-1] == (T0 + 300 + WEEK, gap[1])   # only the day that was missing


def test_a_gap_with_nothing_in_it_leaves_the_list(service, archive):
    """The clock was set back between a drop and the reconnect: a gap that ends before it begins. There's nothing in
    it to fill, and it isn't asked about at every pass from then on."""
    svc = service
    down(svc, *GAP)
    svc._add_gap(T0 + 5000, T0 + 4900)
    archive.changes, archive.upto = incident(), T0 + 2000
    one_pass(svc)
    assert gaps(svc) == [GAP, (T0 + 5000, T0 + 4900)]   # BGPlay covers neither end yet: both wait
    archive.upto = float("inf")
    one_pass(svc)
    assert gaps(svc) == [] and kinds(svc.conn) == EVERYTHING


# ------------------------------------------------------------------------------------------------ the tracking limit
STRAYS = ["198.51.100.0/25", "198.51.100.128/25", "203.0.113.0/25"]   # yours by their paths, but not monitored


def strays(eng):
    """`eng` comes across three unmonitored prefixes with your AS in their paths, the way RisLive.handle and
    Engine.load_bgplay pass routes on. It may track two (max_unmonitored, set low here): the third is over the
    limit."""
    path = [64510, 64496, 64500]
    for p in STRAYS:
        if eng.relevant(p, path):
            eng.apply(T0, "rrc00", PEERS[0], 64510, p, "A", path)


def notices(conn):
    return [e for e in events(conn) if e["kind"] == "tracking_limit"]


def test_only_the_live_engine_says_the_tracking_limit_is_reached(monitor):
    """An engine posts the notice once, and every gap fill and every new prefix's history load makes an engine of
    its own. (RIPEstat's BGPlay answers hold the prefix that was asked for and nothing else, so as things are a
    replay never comes across a stray prefix: this holds the rule in case one ever does.)"""
    from bgpcanary.engine import Engine, for_gap
    cfg, conn, live = monitor
    cfg.max_unmonitored = 2
    strays(live)
    strays(live)
    assert len(notices(conn)) == 1                                       # once, however often it's over the limit
    strays(for_gap(cfg, conn, live.peering, live, T0 + 300))             # a gap filler (Service._fill_gaps)
    assert len(notices(conn)) == 1
    history = Engine(dataclasses.replace(cfg, prefixes=[P1]), conn, restore=False, peering=live.peering)
    history.historical = True                                            # (Service._backfill_new_prefixes)
    strays(history)
    assert len(notices(conn)) == 1


def test_the_live_engine_says_so_while_it_loads_history_too(monitor):
    """At a first start the live engine replays the history itself (Service._startup_history). It's the live engine
    then as well: `historical` isn't what tells a replay engine apart."""
    cfg, conn, live = monitor
    cfg.max_unmonitored = 2
    live.historical = True
    strays(live)
    assert [e["historical"] for e in notices(conn)] == [1]
