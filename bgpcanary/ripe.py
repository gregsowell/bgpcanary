"""RIPE RIS Live stream + RIPEstat HTTP APIs."""
import asyncio
import json
import logging
import time
from collections import OrderedDict

import httpx
import websockets

from . import paths

log = logging.getLogger("bgpcanary.ripe")

STAT = "https://stat.ripe.net/data"
UA = {"User-Agent": "bgpcanary (open-source BGP visibility monitor; RIPEstat/RIS consumer)"}
# RIS Live normally delivers an update within seconds of a collector hearing it (a few arrive minutes late), but now
# and then it passes on updates stamped days or weeks ago. So a message's own timestamp doesn't say when the stream
# last worked: this is how far behind the stream is taken to run at most, when working out what a drop may have
# missed (RisLive.down_since)
LAG_MAX = 900


async def stat(call, sourceapp, **params):
    params["sourceapp"] = sourceapp
    async with httpx.AsyncClient(timeout=120, headers=UA) as c:
        r = await c.get(f"{STAT}/{call}/data.json", params=params)
        r.raise_for_status()
        return r.json()["data"]


async def looking_glass(prefix, sourceapp):
    return await stat("looking-glass", sourceapp, resource=prefix)


async def bgplay(prefix, start, end, sourceapp):
    fmt = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    return await stat("bgplay", sourceapp, resource=prefix, starttime=fmt(start), endtime=fmt(end))


async def as_name(asn, sourceapp):
    d = await stat("as-overview", sourceapp, resource=f"AS{asn}")
    return d.get("holder") or ""


class RisLive:
    """Maintains a RIS Live websocket and feeds the engine. Reconnects forever."""

    URL = "wss://ris-live.ripe.net/v1/ws/?client={client}"

    def __init__(self, engine, run_sync, on_connect, on_disconnect=None):
        self.engine = engine
        self.run_sync = run_sync          # coroutine fn(callable) -> runs callable on engine thread
        self.on_connect = on_connect      # coroutine called after (re)subscribe, e.g. LG resync
        self.on_disconnect = on_disconnect
        self.recent_ids = OrderedDict()
        self.client = f"bgpcanary-as{engine.asn}"
        # the current connection: when it came up, when its last message arrived, and the newest time any carried
        self.up = self.heard = self.newest = None

    def down_since(self):
        """From when updates may be missing, asked when the stream drops: the start of the gap the service fills from
        BGPlay later. It's the newest update time this connection delivered, though never more than LAG_MAX before
        its last message arrived (that message shows the stream worked then) and never before the connection came
        up (the drop before it has a gap of its own).
        It used to be the last message's own timestamp. When that message was one of RIS Live's old ones, the "gap"
        began weeks back, all of it was replayed, and every event in it was recorded again."""
        if self.heard is None:   # no connection since the last drop (a failed attempt to reconnect)
            return time.time()
        if self.newest is None:  # nothing arrived on it
            return self.heard
        return min(self.heard, max(self.newest, self.heard - LAG_MAX, self.up or 0))

    def _subscriptions(self):
        subs = []
        for p in self.engine.cfg.prefixes:
            subs.append({"type": "UPDATE", "prefix": p, "moreSpecific": True, "lessSpecific": True})
        for p in self.engine.controls:   # other networks' prefixes, as a check on RIS itself: just the prefix
            subs.append({"type": "UPDATE", "prefix": p, "moreSpecific": False, "lessSpecific": False})
        subs.append({"type": "UPDATE", "path": f"{self.engine.asn}"})   # our ASN anywhere in a path
        subs.append({"type": "RIS_PEER_STATE"})
        return subs

    async def run(self):
        backoff = 2
        while True:
            try:
                async with websockets.connect(self.URL.format(client=self.client), ping_interval=20,
                                              ping_timeout=30, max_size=None, open_timeout=30) as ws:
                    for s in self._subscriptions():
                        await ws.send(json.dumps({"type": "ris_subscribe", "data": s}))
                    self.engine.status["ws_connected"] = True
                    self.engine.status["ws_since"] = self.up = self.heard = time.time()
                    self.newest = None
                    log.info("RIS Live connected")
                    backoff = 2
                    asyncio.create_task(self.on_connect())
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "ris_message":
                            await self.run_sync(lambda d=msg["data"]: self.handle(d))
                        elif msg.get("type") == "ris_error":
                            log.warning("RIS Live error: %s", msg.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("RIS Live disconnected: %s", e)
            if self.on_disconnect:
                self.on_disconnect(self.down_since())
            self.up = self.heard = self.newest = None
            self.engine.status["ws_connected"] = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def handle(self, d):
        eng = self.engine
        now = time.time()
        ts = float(d.get("timestamp") or now)
        self.heard, self.newest = now, max(self.newest or 0, ts)   # (a duplicate shows the stream working too)
        mid = d.get("id")
        if mid:
            if mid in self.recent_ids:
                return  # duplicate (matched by two subscriptions)
            self.recent_ids[mid] = 1
            if len(self.recent_ids) > 5000:
                self.recent_ids.popitem(last=False)
        eng.status["msgs"] += 1
        eng.status["last_msg"] = now   # when it arrived: the message's own time can be weeks old (see LAG_MAX)
        coll = paths.collector_name(d.get("host", "rrc?"))
        peer = d.get("peer")
        peer_asn = int(d["peer_asn"]) if d.get("peer_asn") else None
        if d.get("type") == "RIS_PEER_STATE":
            if d.get("state") != "connected":
                eng.withdraw_peer(ts, coll, peer)
            return
        if d.get("type") != "UPDATE":
            return
        path = d.get("path") or []
        norm = paths.normalize(path)
        comm = d.get("community")
        for ann in d.get("announcements", []):
            for pfx in ann.get("prefixes", []):
                if eng.relevant(pfx, norm):
                    eng.apply(ts, coll, peer, peer_asn, pfx, "A", path, comm)
        for pfx in d.get("withdrawals", []):
            if pfx in eng.rib or eng.relevant(pfx, None):
                eng.apply(ts, coll, peer, peer_asn, pfx, "W", None)
