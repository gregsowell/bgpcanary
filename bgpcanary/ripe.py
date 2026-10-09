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

    def _subscriptions(self):
        subs = []
        for p in self.engine.cfg.prefixes:
            subs.append({"type": "UPDATE", "prefix": p, "moreSpecific": True, "lessSpecific": True})
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
                    self.engine.status["ws_since"] = time.time()
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
                self.on_disconnect(self.engine.status.get("last_msg") or time.time())
            self.engine.status["ws_connected"] = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def handle(self, d):
        eng = self.engine
        mid = d.get("id")
        if mid:
            if mid in self.recent_ids:
                return  # duplicate (matched by two subscriptions)
            self.recent_ids[mid] = 1
            if len(self.recent_ids) > 5000:
                self.recent_ids.popitem(last=False)
        eng.status["msgs"] += 1
        eng.status["last_msg"] = d.get("timestamp")
        ts = float(d.get("timestamp", time.time()))
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
