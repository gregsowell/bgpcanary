"""Gap fills: history replayed from BGPlay records what happened in it once.

RIS Live drops now and then. The service notes the gap and, once RIPE's archive covers it, replays it in a scratch
engine (engine.for_gap) that rewrites its buckets and records the events it finds, marked historical ("backfill").
Two things went wrong together and these tests hold both: a gap began at the last message's own timestamp, which
RIS Live sometimes sets weeks back, so weeks that had been watched live were replayed; and a replay recorded every
event it found, also the ones the database already held.

The engines here are scratch ones on their own databases (not the seeded app's), fed by hand: no network."""
import asyncio
import dataclasses
import json
import time

import pytest

from conftest import CONFIG, P1

T0 = 1_750_000_000 // 3600 * 3600                      # the scenario's start: on the hour, so on a bucket's edge
PEERS = [f"203.0.113.{i}" for i in range(1, 21)]       # 20 RIS peers on one collector
ROUTES = {p: [64510, 64496, 64500] if i < 10 else [64511, 64497, 64500] for i, p in enumerate(PEERS)}
STRAY = "203.0.113.50"                                 # a peer that reaches you through AS64499 for a while


def incident(frac=0.0):
    """What happens between T0 and T0 + 2400 s, as [(time, peer, path or None for a withdrawal)]: a route through an
    unexpected upstream for three minutes, an outage of four, two dips right after each other, a second outage.
    `frac`: RIS Live's times have fractions (pass one); BGPlay's are whole seconds."""
    out = [(T0 + 420 + frac, STRAY, [64499, 64500]), (T0 + 600 + frac, STRAY, None)]
    for down, up in ((900, 1140), (1800, 1980)):                       # everyone loses the prefix, then has it back
        out += [(T0 + down + frac, p, None) for p in PEERS] + [(T0 + up + frac, p, ROUTES[p]) for p in PEERS]
    for down, up in ((1260, 1262), (1262, 1265)):                      # half of them, for a few seconds: a dip
        out += [(T0 + down + frac, p, None) for p in PEERS[:10]] + [(T0 + up + frac, p, ROUTES[p]) for p in PEERS[:10]]
    return sorted(out, key=lambda c: c[0])


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))


def bgplay(start, end, changes=()):
    """RIPEstat's BGPlay answer for [start, end): the 20 routes at the start, then the changes inside it."""
    src = lambda p: f"00-{p}"
    return {"query_starttime": iso(start), "query_endtime": iso(end),
            "sources": [{"id": src(p), "as_number": 64510} for p in PEERS + [STRAY]],
            "initial_state": [{"source_id": src(p), "target_prefix": P1, "path": path} for p, path in ROUTES.items()],
            "events": [{"type": "A" if path else "W", "timestamp": iso(t),
                        "attrs": {"source_id": src(p), "target_prefix": P1, **({"path": path} if path else {})}}
                       for t, p, path in changes if start <= t < end]}


@pytest.fixture
def monitor(tmp_path):
    """(cfg, conn, live engine) for 192.0.2.0/24 on a fresh database: live since T0 with the 20 routes."""
    from bgpcanary import config, db
    from bgpcanary.engine import Engine
    cfg = dataclasses.replace(config.load(CONFIG), prefixes=[P1], control_prefixes=[],
                              db_path=str(tmp_path / "gaps.db"))
    db.init(cfg.db_path)
    conn = db.connect(cfg.db_path)
    live = Engine(cfg, conn)
    live.load_bgplay(bgplay(T0, T0), True, False)   # as at a start: the routes RIS holds, no history
    live.last_bucket = T0
    yield cfg, conn, live
    conn.close()


def watch(live, until, changes=()):
    """The live engine through to `until`: each change as RIS Live delivers it, buckets closing as time passes."""
    for t, peer, path in changes:
        live.tick(t)
        live.apply(t, "rrc00", peer, 64510, P1, "A" if path else "W", path)
    live.tick(until)


def fill(cfg, conn, live, start, end, changes):
    """What Service._fill_gaps does for one gap, with BGPlay's answer handed over."""
    from bgpcanary.engine import close_open, for_gap
    f = for_gap(cfg, conn, live.peering, live, start)
    f.load_bgplay(bgplay(start, end, changes), True)
    close_open(f, end)


def events(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]


def kinds(conn):
    return sorted(e["kind"] for e in events(conn))


# ------------------------------------------------------------------------------------------------ the replay
def test_a_gap_fill_records_what_happened_while_the_stream_was_down(monitor):
    cfg, conn, live = monitor
    watch(live, T0 + 2400)                                   # the stream was down: the live engine saw none of it
    assert events(conn) == []
    fill(cfg, conn, live, T0 + 300, T0 + 2400, incident())
    evs = events(conn)
    assert kinds(conn) == ["blip", "blip", "chain_change", "outage", "outage", "unexpected_upstream"]
    assert all(e["historical"] == 1 and e["end_ts"] is not None for e in evs)
    assert [(e["start_ts"] - T0, e["end_ts"] - T0) for e in evs if e["kind"] == "outage"] == [(900, 1140), (1800, 1980)]
    assert [(e["start_ts"] - T0, e["end_ts"] - T0) for e in evs if e["kind"] == "blip"] == [(1260, 1262), (1262, 1265)]


def test_the_same_gap_filled_again_records_nothing_twice(monitor):
    """A gap that's recorded twice, or filled again because the service stopped before the list was saved."""
    cfg, conn, live = monitor
    watch(live, T0 + 2400)
    fill(cfg, conn, live, T0 + 300, T0 + 2400, incident())
    once = events(conn)
    fill(cfg, conn, live, T0 + 300, T0 + 2400, incident())
    fill(cfg, conn, live, T0 + 840, T0 + 2400, incident())   # and one that begins further in
    assert events(conn) == once


def test_a_gap_over_time_that_was_watched_live_repeats_none_of_its_events(monitor):
    """The gap that began weeks back: everything in it had been seen live, and was recorded again as backfill."""
    cfg, conn, live = monitor
    watch(live, T0 + 2400, incident(frac=0.37))
    seen = events(conn)
    assert kinds(conn) == ["blip", "blip", "chain_change", "outage", "outage", "unexpected_upstream"]
    assert all(e["historical"] == 0 for e in seen)
    fill(cfg, conn, live, T0 + 300, T0 + 2400, incident())   # BGPlay's whole seconds: each start a little earlier
    assert events(conn) == seen


def test_a_gap_fill_adds_only_what_the_live_engine_missed(monitor):
    """The stream dropped after the first outage: that one is in the database, the second is news."""
    cfg, conn, live = monitor
    watch(live, T0 + 1500, [c for c in incident(frac=0.37) if c[0] < T0 + 1500])
    watch(live, T0 + 2400)
    before = events(conn)
    fill(cfg, conn, live, T0 + 840, T0 + 2400, incident())
    after = events(conn)
    assert after[:len(before)] == before
    assert [(e["kind"], e["start_ts"] - T0, e["end_ts"] - T0, e["historical"])
            for e in after[len(before):]] == [("outage", 1800, 1980, 1)]


def test_only_the_same_anomaly_counts_as_recorded(monitor):
    """Another network's event at the same time isn't this one: the key tells them apart."""
    cfg, conn, live = monitor
    watch(live, T0 + 2400)
    conn.execute("INSERT INTO events(kind,severity,prefix,start_ts,end_ts,title,detail) VALUES(?,?,?,?,?,?,?)",
                 ("unexpected_upstream", "warning", P1, T0 + 400, T0 + 700, "192.0.2.0/24 seen via unexpected upstream "
                  "AS64498", json.dumps({"key": f"unexpected_upstream|{P1}|64498"})))
    conn.commit()
    fill(cfg, conn, live, T0 + 300, T0 + 2400, incident())
    assert [json.loads(e["detail"])["key"].rsplit("|", 1)[1] for e in events(conn)
            if e["kind"] == "unexpected_upstream"] == ["64498", "64499"]


# ------------------------------------------------------------------------------------------------ where a gap begins
def message(ts, n=1):
    return {"type": "UPDATE", "timestamp": ts, "id": f"00-{n}", "host": "rrc00.ripe.net", "peer": PEERS[n],
            "peer_asn": "64510", "path": ROUTES[PEERS[n]], "announcements": [{"next_hop": PEERS[n], "prefixes": [P1]}]}


def reader(live, up_for=3600):
    """RIS Live's reader as it is `up_for` seconds into a connection."""
    from bgpcanary import ripe
    rl = ripe.RisLive(live, None, None)
    rl.up = rl.heard = time.time() - up_for
    return rl


def test_a_gap_begins_at_the_newest_update_heard(monitor):
    live, now = monitor[2], time.time()
    rl = reader(live)
    assert rl.down_since() == rl.up                       # nothing arrived: since the connection came up
    rl.handle(message(now - 2.5))
    assert rl.down_since() == pytest.approx(now - 2.5)    # everything up to then was delivered
    assert live.status["last_msg"] == pytest.approx(now, abs=5)


def test_an_old_message_doesnt_move_a_gaps_start_back(monitor):
    """RIS Live passes on updates stamped days or weeks ago at times. Such a message says the stream works, not that
    nothing newer than three weeks ago was delivered."""
    from bgpcanary import ripe
    live, now = monitor[2], time.time()
    rl = reader(live)
    rl.handle(message(now - 2.5, 1))
    rl.handle(message(now - 20 * 86400, 2))
    assert rl.down_since() == pytest.approx(now - 2.5)
    assert live.status["last_msg"] == pytest.approx(now, abs=5)   # when the last message arrived (docs/API.md)
    rl = reader(live)                                     # a connection that delivered nothing but an old one
    rl.handle(message(now - 20 * 86400, 3))
    assert rl.down_since() == pytest.approx(now - ripe.LAG_MAX, abs=5)
    rl = reader(live, up_for=30)                          # ...never before the connection itself
    rl.handle(message(now - 20 * 86400, 4))
    assert rl.down_since() == pytest.approx(now - 30, abs=5)


def test_the_stream_dropping_after_an_old_message_reports_a_gap_from_now(monitor, monkeypatch):
    """The whole path: a connection delivers a current update and one stamped 20 days ago, then drops."""
    from bgpcanary import ripe
    live, now = monitor[2], time.time()
    frames = [json.dumps({"type": "ris_message", "data": message(now - 1, 1)}),
              json.dumps({"type": "ris_message", "data": message(now - 20 * 86400, 2)})]

    class Socket:
        async def send(self, text):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not frames:
                raise ConnectionError("no close frame received or sent")
            return frames.pop(0)

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(ripe.websockets, "connect", lambda *a, **k: Connection())

    async def drop():
        down, done = [], asyncio.Event()

        async def run_sync(fn):
            return fn()

        async def on_connect():
            pass

        def on_disconnect(since):
            down.append(since)
            done.set()

        task = asyncio.create_task(ripe.RisLive(live, run_sync, on_connect, on_disconnect).run())
        await asyncio.wait_for(done.wait(), 10)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return down[0]

    since = asyncio.run(drop())
    assert since == pytest.approx(time.time(), abs=10)    # not 20 days ago
