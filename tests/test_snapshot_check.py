"""The checks against RIS's snapshot don't rewrite a history that was right.

Once a day the live routes are checked against RIS's snapshot (BGPlay's newest state, about three hours old): a route
the snapshot no longer lists is withdrawn. Then the stored history is brought in line: a route it shows at the
snapshot's time that is gone gets a withdrawal, dated right after the route's last update (source 'stale'), so the
trees and replays drawn from the history don't go on showing it. A start does the same, with the routes it has just
seeded from the snapshot.

"Gone" used to mean "not among the routes the live engine holds now". At a start that's the snapshot. In the daily
check it's the snapshot plus three hours, and what the engine happens to hold:
- a route withdrawn in those three hours, with a withdrawal that's on record, got a second one dated right after its
  last announcement, days or weeks back: the history then showed it as gone all that time;
- a route the engine never held (a peer the looking glass omits, after a start that got no snapshot for the prefix)
  was recorded as withdrawn, though the snapshot lists it.
And a route the check itself took out of the live engine was stored as withdrawn right after the last update the
engine had for it, which can be older than the history's: a start stores the routes it finds in the snapshot, a gap
fill stores what RIS Live missed. The withdrawal then lay before those rows.

The rule these tests hold: what the engine has heard about a peer since the snapshot's time is left alone; where it
holds nothing on a peer, the snapshot says whether the route is gone; and a withdrawal recorded here comes after the
newest update the history has for the route.

And the history has to be all there before it's judged. A stretch the service didn't see (it was stopped, RIS Live
was down, a history load ended early) is a gap, filled from RIPE's archive hours later. Until then the history shows
a route withdrawn in that stretch as still held, and the snapshot shows it gone: it was recorded as withdrawn right
after its last update, long before the stretch, and the gap filler then stored the real withdrawal behind it. A
restart after more than three hours did that to every route withdrawn meanwhile. Such a route is now left to the gap
filler, and to the next check for what's still unexplained then.

The services here are scratch ones on their own databases (not the seeded app's), with BGPlay and the looking glass
answered by hand: no network."""
import asyncio
import time

import pytest

from conftest import P1, P2
from test_gap_fill import PEERS, ROUTES, STRAY, iso
from test_gap_list import WEEK, gaps, one_pass
from test_bgplay_partial import DAY, HOUR, SIDE, Archive, answer, hold, recent, stale
from test_bgplay_partial import archive, one_day, scratch   # noqa: F401  (fixtures)

LATER = {p: ROUTES[p] for p in PEERS[5:]}   # the routes left once five of the peers have withdrawn theirs


class Timeline(Archive):
    """An archive whose answers follow its changes: each starts from the routes as the changes before its start left
    them (an Archive starts every answer from the same routes). `silent`: the routes RIS drops without a withdrawal,
    per prefix, as (time, peer): gone from the answers that start later, and no change in any answer says so."""

    def __init__(self):
        super().__init__()
        self.silent = {}

    async def bgplay(self, prefix, start, end, sourceapp):
        self.asked.append((prefix, start, end))
        await asyncio.sleep(0)
        if self.broken and self.broken(prefix, start, end):
            raise OSError("timed out")
        start, end = min(start, self.upto), min(end, self.upto)
        changes = list(self.changes.get(prefix, ()))
        routes = dict(self.routes.get(prefix, ROUTES))
        for at, peer, path in sorted(changes + [(at, peer, None) for at, peer in self.silent.get(prefix, ())],
                                     key=lambda change: change[0]):
            if at < start and path:
                routes[peer] = path
            elif at < start:
                routes.pop(peer, None)
        return answer(prefix, start, end, changes, routes)


@pytest.fixture
def timeline(monkeypatch):
    from bgpcanary import ripe
    t = Timeline()
    monkeypatch.setattr(ripe, "bgplay", t.bgplay)
    return t


def looking_glass(prefix, at, routes):
    """RIPEstat's looking glass, answering by hand: the routes it lists for `prefix`, as of `at`."""
    return {"resource": prefix, "query_time": iso(at), "latest_time": iso(at),
            "rrcs": [{"rrc": "RRC00", "peers": [{"peer": p, "prefix": prefix, "as_path": " ".join(map(str, path)),
                                                 "asn_origin": str(path[-1])} for p, path in routes.items()]}]}


def daily(svc):
    """One round of the daily check (with the one_day fixture; empty that for another round)."""
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc._snapshot_reconcile_loop())


def shown(svc, prefix, at):
    """How many routes the stored history shows for `prefix` at the time `at`."""
    from bgpcanary import db
    return sum(1 for r in db.state_at(svc.conn, prefix, at) if r["kind"] == "A")


def restarted(scratch, since, until, heard):
    """A service starting again on the database of one that held the 20 routes of each prefix from `since`, plus one
    more to the second prefix, last heard about at `heard`, and stopped at `until`."""
    was = scratch("restart")
    hold(was, since)
    was.engine.apply(heard, "rrc00", STRAY, 64510, P2, "A", SIDE)
    was.engine.tick(until)
    was.conn.commit()
    was.conn.close()
    return scratch("restart")


@pytest.mark.parametrize("told_by", ["RIS Live", "the looking glass"])
def test_the_daily_check_doesnt_date_back_a_withdrawal_thats_on_record(scratch, archive, one_day, told_by):
    """Five peers withdrew their routes an hour ago, and the live engine was told: by RIS Live as it happened, or by
    the looking glass at a resync. The snapshot is three hours old and lists all 20. Each of the five was recorded
    as withdrawn a second time, right after its last announcement six hours ago: for the five hours it was there,
    the history showed 15 routes."""
    svc, (now, since) = scratch(), recent()
    hold(svc, since, now - 2 * HOUR)
    if told_by == "RIS Live":
        for peer in PEERS[:5]:
            svc.engine.apply(now - HOUR, "rrc00", peer, 64510, P2, "W", None)
    else:
        svc.engine.resync_lg(looking_glass(P2, now - 2 * HOUR, ROUTES))   # it listed all 20, and now 15
        svc.engine.resync_lg(looking_glass(P2, now - HOUR, LATER))
    svc.engine.tick(now - 30)
    archive.upto = now - 3 * HOUR
    daily(svc)
    assert stale(svc) == []
    assert (shown(svc, P2, now - 2 * HOUR), shown(svc, P2, now)) == (20, 15)


def test_the_daily_check_doesnt_take_a_route_the_engine_never_held_for_one_thats_gone(scratch, archive, one_day):
    """A start got no snapshot for the second prefix (the request failed), so it seeded none of its routes and left
    its history alone. The looking glass then brings the routes of the peers it lists; the others aren't held until
    they send an update (here that's all 20: nothing is asked of the looking glass). The next daily check does get
    the snapshot, which lists the 20. They were recorded as withdrawn all the same, each right after its last
    update: the history showed a prefix with no routes at all. What the snapshot doesn't list (one route RIS dropped
    without saying so) is recorded as before."""
    (now, since) = recent()
    svc = restarted(scratch, since, now - 60, heard=since)
    archive.upto, archive.broken = now - 3 * HOUR, lambda prefix, start, end: prefix == P2
    asyncio.run(svc._startup_history())
    assert stale(svc) == [] and svc.engine.seen(P2) == 0   # left as it was
    archive.broken = None
    daily(svc)
    assert stale(svc) == [(P2, STRAY)]
    assert shown(svc, P2, now) == 20


def test_a_route_dropped_for_being_stale_is_still_dropped_from_the_history(scratch, archive, one_day):
    """What the check goes on doing, and why the history isn't simply held against the snapshot. RIS can list a
    route long after the peer lost it (the withdrawal never arrived). A start seeds it from the snapshot and stores
    it as held then. The looking-glass resync drops it from the live engine, once it's weeks old and the looking
    glass has never listed it, and dates that right after the route's last real update: before the row the start
    stored, so the history goes on showing it. The daily check is what records it as gone there too, though the
    snapshot lists it. A gap that's still to be filled doesn't hold that up: the snapshot lists the route, so no
    withdrawal of it is waiting in the archive."""
    (now, since) = recent()
    svc = restarted(scratch, since, now - 60, heard=now - 30 * DAY)
    archive.upto, archive.routes[P2] = now - 3 * HOUR, {**ROUTES, STRAY: SIDE}
    asyncio.run(svc._startup_history())
    svc.engine.resync_lg(looking_glass(P2, now - 60, ROUTES))
    svc._add_gap(since + HOUR, since + 1.5 * HOUR)
    assert (svc.engine.visible(P2), shown(svc, P2, now)) == (20, 21)
    daily(svc)
    assert shown(svc, P2, now) == 20
    assert stale(svc) == [(P2, STRAY)] * 2   # by the looking-glass resync, and after the row the start stored


def test_the_daily_check_doesnt_date_back_a_withdrawal_the_gap_filler_recorded(scratch, archive, one_day):
    """RIS Live was down for half an hour, and in it a peer withdrew its route. The live engine never heard of it
    (the looking glass doesn't list every peer), so it holds the route until the daily check finds it missing from
    the snapshot. By then the gap filler has recorded the withdrawal, at its own time. The check took the route out
    of the live engine, which is right, and stored a withdrawal for it right after the last update the engine had
    heard, six hours ago: the history showed 19 routes for the hour before RIS Live dropped, when there were 20."""
    svc, (now, since) = scratch(), recent()
    down, back, gone_at = since + HOUR, since + 1.5 * HOUR, since + 1.25 * HOUR
    hold(svc, since, now - 60)
    svc._add_gap(down, back)
    archive.upto, archive.changes[P2] = now - 3 * HOUR, [(gone_at, PEERS[0], None)]
    one_pass(svc)                                          # the gap is filled from the archive
    assert gaps(svc) == [] and (shown(svc, P2, back), svc.engine.visible(P2)) == (19, 20)
    archive.routes[P2] = {p: ROUTES[p] for p in PEERS[1:]}   # what RIS holds since
    daily(svc)
    assert svc.engine.visible(P2) == 19 and stale(svc) == []
    assert [shown(svc, P2, at) for at in (down - 60, gone_at - 60, back)] == [20, 20, 19]


def test_a_route_dropped_without_a_withdrawal_isnt_dated_before_a_start_that_found_it(scratch, archive, one_day):
    """A route last heard about five days ago is in RIS's snapshot at a start: it's seeded and stored as held then,
    and its age in the live engine is set back to the five days (how a stale route is told from a fresh one). Later
    RIS drops it without a withdrawal, and the daily check finds it missing. It was recorded as withdrawn twice:
    right after the row the start stored, and right after the update of five days ago, so the history showed it as
    gone for the five days before the start, when it was held."""
    (now, since) = recent()
    svc = restarted(scratch, since, now - 60, heard=now - 5 * DAY)
    archive.upto, archive.routes[P2] = now - 3 * HOUR, {**ROUTES, STRAY: SIDE}
    asyncio.run(svc._startup_history())
    assert svc.engine.visible(P2) == 21
    archive.routes[P2] = ROUTES                            # RIS no longer lists it
    daily(svc)
    assert svc.engine.visible(P2) == 20 and stale(svc) == [(P2, STRAY)]
    assert [shown(svc, P2, at) for at in (now - 4 * HOUR, now)] == [21, 20]   # an hour before that start, and now


# ------------------------------------------------------------------------------------------------ a history with a gap
@pytest.mark.parametrize("starts", [1, 2])
def test_a_restart_after_a_long_stop_leaves_what_went_meanwhile_to_the_gap_filler(scratch, timeline, one_day, starts):
    """The service was stopped for six hours. An hour into that, five peers withdrew their routes and RIS logged it;
    later RIS dropped a sixth route without a withdrawal. The restart seeds the 15 routes of RIS's snapshot and finds
    six in its history that aren't among them. It recorded each as withdrawn right after its last update, two hours
    before the stop; the gap filler then stored the routes as they stood at the stop, and the five withdrawals: the
    history showed 15 routes for the two hours before the stop, in which there were 21.
    The history ends where the stop began and what happened since is still to be loaded, so the six are left alone.
    They are at a second start a minute later too, before the archive has caught up (`starts`): the history reaches
    the snapshot's time then, with the hours of the stop still missing from it. Once the gap is filled, the next
    daily check records the one route nothing explains, right after the last row the history has for it."""
    now = time.time()
    edge = int(now // 30 * 30)
    since, stop = edge - 8 * HOUR, edge - 6 * HOUR
    timeline.routes[P2], timeline.upto = {**ROUTES, STRAY: SIDE}, now - 3 * HOUR
    timeline.changes[P2] = [(stop + HOUR, peer, None) for peer in PEERS[:5]]
    timeline.silent[P2] = [(stop + 1.5 * HOUR, STRAY)]
    svc = restarted(scratch, since, stop, heard=since)
    asyncio.run(svc._startup_history())
    if starts == 2:
        svc.engine.last_bucket = edge - 60                 # live for a minute, then it starts again
        svc.engine.tick(edge)
        svc.conn.commit()
        svc.conn.close()
        svc = scratch("restart")
        asyncio.run(svc._startup_history())
    assert svc.engine.visible(P2) == 15 and stale(svc) == []
    assert shown(svc, P2, stop - HOUR) == 21
    timeline.upto = float("inf")                           # hours later the archive covers the stop
    one_pass(svc)
    assert gaps(svc) == []
    daily(svc)
    assert stale(svc) == [(P2, STRAY)]
    assert [shown(svc, P2, at) for at in (stop - HOUR, stop + HOUR / 2, stop + 2 * HOUR)] == [21, 20, 15]


def test_the_daily_check_waits_for_a_gap_that_is_still_to_be_filled(scratch, timeline, one_day):
    """RIS Live was down for half an hour, and in it a peer withdrew its route; the live engine never heard, as in
    the test further up. This time the daily check comes before the gap filler has got to that gap: the archive
    covers it, the filler's next pass is minutes away. The check takes the route out of the live engine. It also
    recorded it as withdrawn right after its last update, six hours ago, and the gap filler put the real withdrawal
    behind that. While a gap from before the snapshot's time is still to be filled, a route last heard about before
    the gap ends is left for the next day's check: by then its withdrawal is on record, or it went without one."""
    svc, (now, since) = scratch(), recent()
    down, back, gone_at = since + HOUR, since + 1.5 * HOUR, since + 1.25 * HOUR
    hold(svc, since, now - 60)
    svc._add_gap(down, back)
    timeline.upto, timeline.changes[P2] = now - 3 * HOUR, [(gone_at, PEERS[0], None)]
    daily(svc)
    assert svc.engine.visible(P2) == 19 and stale(svc) == []
    one_pass(svc)                                          # the gap is filled
    one_day.clear()
    daily(svc)                                             # the next day
    assert gaps(svc) == [] and stale(svc) == []
    assert [shown(svc, P2, at) for at in (down - 60, gone_at - 60, back)] == [20, 20, 19]


def test_a_first_start_that_stopped_early_leaves_the_rest_to_the_gap_filler(scratch, timeline, one_day):
    """A new install loads eight days of history, and what comes after the first week doesn't arrive: the load ends
    there and the rest is a gap. The routes it holds are then checked against RIS's snapshot. One that was withdrawn
    in the stretch still to be loaded is taken out of the live engine, and it was recorded as withdrawn right after
    its last row, on the first day of the history: a week before the gap filler stored when it really went."""
    svc, before = scratch(prefixes=[P1], backfill_days=8, bucket_seconds=60), time.time()
    begins = before - 8 * DAY
    timeline.upto, timeline.changes[P1] = before - 3 * HOUR, [(begins + 7.5 * DAY, PEERS[0], None)]
    timeline.broken = lambda prefix, start, end: abs(start - (begins + WEEK)) < 60 and end - start > 60
    asyncio.run(svc._startup_history())
    assert svc.engine.visible(P1) == 19 and stale(svc) == []
    timeline.upto, timeline.broken = float("inf"), None    # the archive answers, and has caught up
    one_pass(svc)
    daily(svc)
    assert gaps(svc) == [] and stale(svc) == []
    assert [shown(svc, P1, at) for at in (begins + DAY, begins + 7.25 * DAY, before)] == [20, 20, 19]
