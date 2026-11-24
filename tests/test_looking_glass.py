"""RIPE's looking glass only takes routes out of bgpcanary's view when its answers can be trusted.

The looking glass (RIPEstat) is a cross-check on RIS Live, which reports withdrawals as they happen: a resync every few
minutes catches the odd one RIS Live missed (Engine.resync_lg). Its answers can be wrong. From 2026-11-20 it was two
days behind and listed none of a prefix's routes; that was taken for an incomplete answer, and nothing was withdrawn.
Then it filled in again a little at a time, over a day. An answer missing 24 of about 330 routes was under the limit
then (a tenth of them), and it came half an hour after the last incomplete one: the 24 were withdrawn though RIS still
had them, and the prefix's visibility read 302 to 319 for twelve hours instead of about 330. Once it was back, routes
were withdrawn minutes after RIS Live had announced them, and put back minutes later: it catches up with each peer's
changes some minutes after RIS Live reports them.

The rules these tests hold:
- an answer missing more than ten routes is incomplete, however many the prefix has, and withdraws none;
- for six hours after an answer that was incomplete, or far behind, nothing is withdrawn (the data-sources panel says
  how much longer);
- a route that changed in the hour before the looking glass's data is left as RIS Live has it.

The services here are scratch ones on their own databases, with the looking glass answered by hand: no network."""
import asyncio
import time

import pytest

from conftest import P1
from test_gap_fill import PEERS, ROUTES, iso
from test_bgplay_partial import DAY, HOUR
from test_bgplay_partial import scratch   # noqa: F401  (a fixture)
from test_unheard_stretches import clock   # noqa: F401  (a fixture: the time of day, set by hand)

NEW_PATH = [64511, 64497, 64500]       # a peer's path once it moved to the other upstream


def answer(prefix, at, routes, behind=0):
    """RIPEstat's looking glass, answering by hand at `at`: the routes it lists for `prefix`, from data `behind` old."""
    return {"resource": prefix, "query_time": iso(at), "latest_time": iso(at - behind),
            "rrcs": [{"rrc": "RRC00", "peers": [{"peer": p, "prefix": prefix, "as_path": " ".join(map(str, path)),
                                                 "asn_origin": str(path[-1])} for p, path in routes.items()]}]}


def seed(svc, prefix, routes, at):
    """The live engine holds `routes` for `prefix` since `at`, seeded as a start seeds them from RIS's snapshot
    (quietly: that's no news), unchanged since."""
    src = lambda p: f"00-{p}"
    svc.engine.load_bgplay({"query_starttime": iso(at), "query_endtime": iso(at),
                            "sources": [{"id": src(p), "as_number": path[0]} for p, path in routes.items()],
                            "initial_state": [{"source_id": src(p), "target_prefix": prefix, "path": path}
                                              for p, path in routes.items()], "events": []}, True, False)


def first(n, routes=ROUTES):
    return dict(list(routes.items())[:n])


class Glass:
    """ripe.looking_glass, answered by hand: what it lists for each prefix right now, from data `behind` old."""

    def __init__(self):
        self.routes, self.behind = {}, 0

    async def looking_glass(self, prefix, sourceapp):
        await asyncio.sleep(0)
        return answer(prefix, time.time(), self.routes.get(prefix, {}), self.behind)


@pytest.fixture
def glass(monkeypatch):
    from bgpcanary import ripe
    g = Glass()
    monkeypatch.setattr(ripe, "looking_glass", g.looking_glass)
    return g


def resync(svc):
    """One round of the service's looking-glass resync, every prefix (Service._resync_all)."""
    asyncio.run(svc._resync_all())


def test_an_answer_missing_more_than_ten_routes_withdraws_none_of_them(scratch):
    """A prefix 120 RIS peers have a route to, and an answer that lists 108 of them. A tenth of its routes (12) was
    the limit, so the 12 were withdrawn. Ten is the limit now, whatever the prefix: the answer is incomplete. A later
    one missing three of them (RIS dropped them without a withdrawal) does withdraw those."""
    svc, now = scratch(prefixes=[P1]), time.time()
    routes = {f"198.18.{i // 200}.{i % 200 + 1}": [64510 + i % 2, 64496, 64500] for i in range(120)}
    seed(svc, P1, routes, now - DAY)
    svc.engine.resync_lg(answer(P1, now - 600, routes))              # it lists them all...
    svc.engine.resync_lg(answer(P1, now - 300, dict(list(routes.items())[12:])))   # ...then 12 fewer
    assert svc.engine.visible(P1) == 120
    assert svc.engine.lg_stats[P1]["incomplete"] and svc.engine.lg_stats[P1]["last_doubt"]
    svc.engine.resync_lg(answer(P1, now, dict(list(routes.items())[3:])))
    assert svc.engine.visible(P1) == 117
    assert not svc.engine.lg_stats[P1]["incomplete"]


def test_a_route_that_just_changed_is_left_as_ris_live_has_it(scratch):
    """Five minutes ago RIS Live reported one peer's new path, and another peer's withdrawal. The looking glass hasn't
    caught up with either yet: it doesn't list the first peer, and still lists the second one's route. The first was
    withdrawn and the second put back (RIS Live set them right again minutes later, until the next resync). An hour on,
    with nothing more from RIS Live, it's the looking glass that's taken at its word."""
    svc, now = scratch(prefixes=[P1]), time.time()
    moved, gone = PEERS[0], PEERS[1]
    seed(svc, P1, ROUTES, now - DAY)
    svc.engine.resync_lg(answer(P1, now - 2 * HOUR, ROUTES))       # (it has listed them all)
    svc.engine.apply(now - 300, "rrc00", moved, 64511, P1, "A", NEW_PATH)
    svc.engine.apply(now - 300, "rrc00", gone, 64510, P1, "W", None)
    lagging = {p: path for p, path in ROUTES.items() if p != moved}   # (and `gone` still on its old path)
    assert svc.engine.resync_lg(answer(P1, now, lagging)) == 0
    rib = svc.engine.rib[P1]
    assert rib[("rrc00", moved)].path == NEW_PATH and not rib[("rrc00", gone)].path
    assert svc.engine.resync_lg(answer(P1, now + 2 * HOUR, lagging)) == 2
    assert not rib[("rrc00", moved)].path and rib[("rrc00", gone)].path == ROUTES[gone]


def test_a_looking_glass_back_from_an_outage_withdraws_nothing_until_it_has_been_right_for_six_hours(
        scratch, clock, glass):
    """It answers as RIPE's did from 2026-11-20 (hours from the first answer, the routes it lists, its data's age):
    two days behind and listing nothing; current again, still empty; then filling in, until an answer is short by
    fewer than ten routes. That one came 36 minutes after the last incomplete answer, and the eight it lacked were
    withdrawn though RIS had them all along. Nothing is withdrawn now until six hours after the last answer that was
    incomplete or behind, and the data-sources panel says so. By then it lists every route; one RIS drops later
    without a withdrawal is withdrawn as before."""
    svc, start = scratch(prefixes=[P1]), clock.now
    seed(svc, P1, ROUTES, start - DAY)
    glass.routes[P1] = ROUTES
    resync(svc)                                                     # as usual: it lists all 20
    for hours, listed, behind in [(1, 0, 2 * DAY), (6, 0, 2 * DAY), (30, 0, 0), (31, 5, 0), (32, 9, 0),
                                  (32.6, 12, 0), (35, 16, 0), (37, 19, 0), (37.5, 20, 0)]:
        clock.now = start + hours * HOUR
        glass.routes[P1], glass.behind = first(listed), behind
        resync(svc)
        assert svc.engine.visible(P1) == 20, f"{hours} h: {listed} listed"
    notes = svc.health.state()["notes"]
    assert any("may not withdraw routes for another 30 min" in n for n in notes), notes   # (32 h + 6 h - 37.5 h)
    clock.now = start + 38.5 * HOUR
    glass.routes[P1] = {p: path for p, path in ROUTES.items() if p != PEERS[0]}
    resync(svc)
    assert svc.engine.visible(P1) == 19
    assert not any("may not withdraw" in n for n in svc.health.state()["notes"])


def test_a_looking_glass_that_was_far_behind_waits_six_hours_too(scratch, clock, glass):
    """It listed every route, from data three hours old. An hour later it has caught up, and one route is missing:
    RIS dropped it without a withdrawal, or the looking glass hasn't filled that one in yet. It's left until six hours
    after the answer that was behind, then withdrawn."""
    svc, start = scratch(prefixes=[P1]), clock.now
    seed(svc, P1, ROUTES, start - DAY)
    glass.routes[P1], glass.behind = ROUTES, 3 * HOUR
    resync(svc)
    clock.now, glass.behind = start + HOUR, 0
    glass.routes[P1] = {p: path for p, path in ROUTES.items() if p != PEERS[0]}
    resync(svc)
    assert svc.engine.visible(P1) == 20
    clock.now = start + 6.5 * HOUR
    resync(svc)
    assert svc.engine.visible(P1) == 19
