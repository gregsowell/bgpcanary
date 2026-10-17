"""Exchange peers: which of your BGP neighbours are peers rather than transit providers.

A neighbour (the AS right before yours in a path) is a peer when PeeringDB shows it at one of the internet
exchanges your own ASN is on, or when it's listed under Settings > Monitored network > Peers (private peering,
or exchanges PeeringDB doesn't know about). Expected upstreams (your transits) always count as transit, even when
they're at the same exchange. Peers carry your routes only to their own customers, so they're grouped instead
of being treated as upstreams: no "unexpected upstream" warnings for them, one "Exchanges" branch in the tree.

The exchange lists come from PeeringDB's public API, are kept in the database, and are refreshed once a day.
"""
import asyncio
import logging
import time

import httpx

from . import __version__, db

log = logging.getLogger("bgpcanary.peering")
KV = "peering_exchanges"
PRIVATE = "Private peering"
REFRESH_S = 86400        # PeeringDB data changes slowly
RETRY_S = 3600           # after a failed lookup
API = "https://www.peeringdb.com/api/netixlan"


class Peering:
    def __init__(self, cfg, conn):
        self.cfg, self.conn = cfg, conn
        self.data = db.kv_get(conn, KV, {}) or {}
        self._index()

    def _index(self):
        """neighbour ASN -> the first of your exchanges it's at (exchanges in name order), plus any others."""
        self.member_of, self.also = {}, {}
        for ix in self.exchanges():
            for a in ix["members"]:
                if a == self.cfg.asn:
                    continue
                if a in self.member_of:
                    self.also.setdefault(a, []).append(ix["name"])
                else:
                    self.member_of[a] = ix["name"]

    def exchanges(self):
        """Your exchanges, as last fetched from PeeringDB: [{"id", "name", "members": [asn, ...]}]."""
        if self.data.get("asn") != self.cfg.asn:   # fetched for a different ASN: not yours
            return []
        return self.data.get("ixes") or []

    def group(self, asn):
        """Where neighbour `asn` peers with you: an exchange's name, or PRIVATE for a listed peer that isn't at one
        of your exchanges. None when it isn't a peer: one of your transits, or an unknown neighbour."""
        if asn is None or asn in self.cfg.expected_upstreams:
            return None
        if asn in self.member_of:
            return self.member_of[asn]
        if asn in self.cfg.peers:
            return PRIVATE
        return None

    def label(self, asn):
        """'Metro-IX North' / 'Metro-IX North, also Metro-IX South' / 'Private peering' / None."""
        g = self.group(asn)
        if g and asn in self.also:
            return f"{g}, also {', '.join(self.also[asn])}"
        return g

    def status(self):
        """For the settings page."""
        return {"exchanges": [{"name": ix["name"], "members": len(ix["members"])} for ix in self.exchanges()],
                "fetched": self.data.get("fetched") if self.data.get("asn") == self.cfg.asn else None,
                "error": self.data.get("error")}

    async def refresh(self, force=False):
        """Fetch the exchanges your ASN is on, and each one's members, from PeeringDB. Does nothing while the
        stored list is fresh (a day), or for an hour after a failed attempt, unless `force`."""
        asn, now = self.cfg.asn, time.time()
        if not asn:
            return
        if not force and self.data.get("asn") == asn and now - self.data.get("fetched", 0) < REFRESH_S:
            return
        if not force and self.data.get("tried_asn") == asn and now - self.data.get("tried", 0) < RETRY_S:
            return   # (a changed ASN is looked up straight away)
        self.data.update(tried=now, tried_asn=asn)
        try:
            async with httpx.AsyncClient(timeout=60, headers={"User-Agent": f"bgpcanary/{__version__}"}) as c:
                r = await c.get(API, params={"asn": asn})
                r.raise_for_status()
                names = {}
                for x in r.json().get("data", []):
                    # "Metro-IX North: Metro-IX North Peering LAN" -> "Metro-IX North"
                    names.setdefault(x["ix_id"], (x.get("name") or f"IX {x['ix_id']}").split(":")[0].strip())
                ids = sorted(names, key=lambda i: names[i].lower())
                members = {i: set() for i in ids}
                for k in range(0, len(ids), 25):   # the members of up to 25 exchanges per request
                    await asyncio.sleep(2)   # PeeringDB rate-limits anonymous clients
                    r = await c.get(API, params={"ix_id__in": ",".join(map(str, ids[k:k + 25])), "fields": "asn,ix_id"})
                    r.raise_for_status()
                    for m in r.json().get("data", []):
                        if m.get("asn") and m.get("ix_id") in members:
                            members[m["ix_id"]].add(int(m["asn"]))
                ixes = [{"id": i, "name": names[i], "members": sorted(members[i])} for i in ids]
            self.data = {"asn": asn, "fetched": now, "tried": now, "tried_asn": asn, "ixes": ixes}
            log.info("PeeringDB: AS%s is at %d exchange(s)%s", asn, len(ixes),
                     ": " + ", ".join(f"{ix['name']} ({len(ix['members'])} members)" for ix in ixes) if ixes else "")
        except Exception as ex:
            log.warning("PeeringDB exchange lookup for AS%s failed (keeping what was stored): %s", asn, ex)
            self.data["error"] = (str(ex) or type(ex).__name__)[:200]
        db.kv_set(self.conn, KV, self.data)
        self.conn.commit()
        self._index()
