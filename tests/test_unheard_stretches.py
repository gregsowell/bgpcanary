"""Every stretch the live engine didn't hear is noted as a gap.

The live engine hears of changes from RIS Live. What it misses goes on a list of gaps, and the gap filler loads each
from RIPE's archive once that covers it (Service._add_gap, _fill_gaps). A gap was noted in two places: at a start,
from the last bucket stored to the moment the start began, and when RIS Live came back, from its drop to then. Three
stretches fell between those:
- RIS Live drops, and the service is stopped before it's back. The drop's start was kept in memory only, and buckets
  go on being stored while RIS Live is down, so the next start's gap began at the stop;
- from a start to RIS Live's first sync: seconds at a restart, as long as it takes when RIS Live can't be reached,
  and on a new install the whole history load, which comes after the moment the start's gap ends at;
- RIS Live drops again while the looking glass is still being asked after a reconnect, as a connection that comes
  and goes within seconds does: the gap was noted up to the end of the asking, and what followed wasn't.
Nothing that happened in such a stretch reached the charts or the Events list, and the check of the stored history
against RIS's snapshot, which goes by the list of gaps, took the history for complete there.

In these tests the clock is set by hand (time.time), the looking glass doesn't answer and BGPlay is answered by hand:
no network."""
import asyncio
import time

import pytest

from test_gap_fill import T0
from test_gap_list import gaps, one_pass
from test_bgplay_partial import HOUR, hold
from test_bgplay_partial import archive, scratch   # noqa: F401  (fixtures)

NOW = T0 + 30 * 24 * HOUR              # the time these tests begin at: on the hour, so on a bucket's edge


class Clock:
    """The time of day, set by hand: `now` is what time.time() says."""

    def __init__(self, now):
        self.now = now


@pytest.fixture
def clock(monkeypatch):
    c = Clock(NOW)
    monkeypatch.setattr(time, "time", lambda: c.now)
    return c


class Sync:
    """sync(svc): RIS Live (re)connects and the service syncs with it (Service._on_ws_connect). Every sync asks the
    looking glass first, which doesn't answer here, so the routes stay as they are. `meanwhile`: called once, while
    it's being asked."""

    def __init__(self):
        self.meanwhile = None

    async def looking_glass(self, prefix, sourceapp):
        if self.meanwhile:
            happens, self.meanwhile = self.meanwhile, None
            happens()
        await asyncio.sleep(0)
        raise OSError("timed out")

    def __call__(self, svc):
        asyncio.run(svc._on_ws_connect())


@pytest.fixture
def sync(monkeypatch):
    from bgpcanary import ripe
    s = Sync()
    monkeypatch.setattr(ripe, "looking_glass", s.looking_glass)
    return s


def stopped(svc):
    """The service stops, as Service.stop has it, and lets go of its database."""
    asyncio.run(svc.stop())
    svc.conn.close()


def test_a_drop_still_open_when_the_service_stops_is_a_gap_at_the_next_start(scratch, archive, clock, sync):
    """RIS Live dropped two hours ago and hasn't come back; a minute ago the service was stopped. The start that
    follows found its last bucket a minute old and noted that minute: the two hours were never loaded. They're on
    the list now, and once: when they've been filled, a later start doesn't put them back."""
    was = scratch()
    hold(was, NOW - 6 * HOUR, NOW - 2 * HOUR)
    sync(was)                                              # live, and in sync
    was._on_ws_disconnect(NOW - 2 * HOUR)
    was.engine.tick(NOW - 60)                              # (the buckets go on while RIS Live is down)
    stopped(was)
    svc = scratch()
    archive.upto = NOW - 3 * HOUR
    asyncio.run(svc._startup_history())
    assert gaps(svc) == [(NOW - 2 * HOUR, NOW)]
    sync(svc)
    archive.upto = clock.now = NOW + 4 * HOUR              # hours later the archive covers the gap, and it's filled
    one_pass(svc)
    assert gaps(svc) == []
    svc.engine.last_bucket = NOW
    svc.engine.tick(clock.now)
    stopped(svc)
    again = scratch()
    asyncio.run(again._startup_history())
    assert gaps(again) == []


@pytest.mark.parametrize("start", ["a restart", "a new install"])
def test_from_a_start_to_ris_lives_first_sync_is_a_gap(scratch, archive, clock, sync, start):
    """The gap a start notes ends at the moment the start begins. Then the history is loaded (minutes on a new
    install; a restart seeds its routes in seconds) and RIS Live is reached, or isn't for a while. Nothing is heard
    until its first sync, which comes 20 minutes after the start here. Those 20 minutes weren't on the list."""
    archive.upto = NOW - 3 * HOUR
    if start == "a restart":
        was = scratch()
        hold(was, NOW - 6 * HOUR, NOW - 60)
        stopped(was)
    svc = scratch(backfill_days=1)
    asyncio.run(svc._startup_history())
    ((begins, ends),) = gaps(svc)
    assert (begins, ends) == (NOW - 60 if start == "a restart" else NOW - 3 * HOUR, NOW)
    clock.now = NOW + 20 * 60
    sync(svc)
    assert gaps(svc) == [(begins, NOW + 20 * 60)]


@pytest.mark.parametrize("then", ["RIS Live is back for good", "the service is stopped"])
def test_a_drop_while_the_looking_glass_is_asked_doesnt_end_the_gap(scratch, archive, clock, sync, then):
    """RIS Live dropped ten minutes ago and is back. A sync asks the looking glass first, which takes seconds, and
    notes the gap when that's done. Five seconds in, RIS Live drops again. The gap was noted up to the end of the
    asking and the drop forgotten with it: what followed was never on the list, here the five minutes until RIS Live
    is back for good, or until the service is stopped and started again."""
    svc = scratch()
    hold(svc, NOW - 6 * HOUR, NOW - 600)
    sync(svc)                                              # live, and in sync
    svc._on_ws_disconnect(NOW - 600)

    def drops_again():
        clock.now = NOW + 5
        svc._on_ws_disconnect(NOW + 5)

    sync.meanwhile = drops_again
    sync(svc)
    assert gaps(svc) == [(NOW - 600, NOW + 5)]
    clock.now = NOW + 305
    if then == "the service is stopped":
        svc.engine.tick(NOW + 300)
        stopped(svc)
        svc = scratch()
        archive.upto = NOW - 3 * HOUR
        asyncio.run(svc._startup_history())
    else:
        sync(svc)
    assert gaps(svc) == [(NOW - 600, NOW + 305)]
