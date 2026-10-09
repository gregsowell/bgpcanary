"""Transit filter check (informational).

For each of your direct upstreams U, look at how U's *own* prefixes reach the internet (RIPEstat
looking glass): every AS T seen directly in front of U is one of U's transits/peers. If T carries U's
own prefixes to a meaningful number of RIS peers but none of your prefix via U, then T isn't getting
(or isn't accepting) your route from U - typically a prefix-filter / IRR issue on U's or T's side.
"""
import ipaddress
import logging
import time

from . import db, paths, ripe

log = logging.getLogger("bgpcanary.transit")
KV_TOGGLE = "transit_filter_enabled"


def enabled(cfg, conn):
    return bool(db.kv_get(conn, KV_TOGGLE, cfg.transit_filter_check))


class TransitCheck:
    def __init__(self, svc):
        self.svc = svc
        self.cfg = svc.cfg
        self.ref_prefixes = {}   # (upstream, ip version) -> (fetched_ts, [prefixes])
        self.pending = {}        # key -> consecutive flagged checks
        self.clean = {}          # open key -> consecutive checks that no longer flag it
        self.last_run = None
        self.last_result = []    # rows for the settings page

    async def _reference(self, upstream, version):
        """Up to 3 of the upstream's own prefixes in the same address family as the monitored prefix."""
        cached = self.ref_prefixes.get((upstream, version))
        if cached and time.time() - cached[0] < 86400:
            return cached[1]
        d = await ripe.stat("announced-prefixes", self.svc.sourceapp, resource=f"AS{upstream}")
        pf = [p["prefix"] for p in d.get("prefixes", []) if (":" in p["prefix"]) == (version == 6)]
        pf.sort(key=lambda p: int(p.split("/")[1]))  # widest (usually best-propagated) first
        self.ref_prefixes[(upstream, version)] = (time.time(), pf[:3])
        return pf[:3]

    async def _ref_counts(self, upstream, version):
        """{transit_asn: max peers seeing T->U adjacency across U's reference prefixes}"""
        out = {}
        for pfx in await self._reference(upstream, version):
            lg = await ripe.looking_glass(pfx, self.svc.sourceapp)
            c = {}
            for rrc in lg.get("rrcs", []):
                for p in rrc.get("peers", []):
                    a = paths.parse_str(p["as_path"])
                    if len(a) >= 2 and a[-1] == upstream:
                        c[a[-2]] = c.get(a[-2], 0) + 1
            for t, n in c.items():
                out[t] = max(out.get(t, 0), n)
        return out

    def _our_counts(self, prefix):
        """({(upstream, transit): peers}, {asn: peers whose path contains it}) from the live RIB."""
        adj, seen = {}, {}
        for e in list(self.svc.engine.rib.get(prefix, {}).values()):
            p = e.path
            if not p or p[-1] != self.cfg.asn:
                continue
            if len(p) >= 3:
                adj[(p[-2], p[-3])] = adj.get((p[-2], p[-3]), 0) + 1
            for x in p[:-1]:
                seen[x] = seen.get(x, 0) + 1
        return adj, seen

    async def run(self):
        eng, conn = self.svc.engine, self.svc.aconn
        if not enabled(self.cfg, conn):
            self.close_all("check turned off")
            return
        self.last_run = time.time()
        rows = []
        flagged = set()
        skipped = set()
        unknown = set()   # (prefix, upstream) we couldn't evaluate this round: leave their notices as they are
        for prefix, mon in eng.mons.items():
            if mon.state == "down" or mon.dip_start:
                skipped.add(prefix)  # already degraded (an outage event covers it); keep its notices as they are
                continue
            version = ipaddress.ip_network(prefix, strict=False).version
            adj, seen = self._our_counts(prefix)
            ups = {u for (u, _t) in adj} | set(self.cfg.expected_upstreams)
            for u in sorted(ups):
                if not any(k[0] == u for k in adj):
                    unknown.add((prefix, u))
                    continue  # upstream not carrying us at all - upstream_missing covers that
                try:
                    ref = await self._ref_counts(u, version)
                except Exception as ex:
                    # A RIPEstat hiccup says nothing about the filter: without this, every failed lookup
                    # "cleared" the upstream's notices and the next good check re-opened them.
                    log.warning("transit check: reference lookup for AS%s failed (notices kept as they are): %s",
                                u, ex or type(ex).__name__)
                    unknown.add((prefix, u))
                    continue
                if not ref:   # an empty answer about a live network is a lookup problem, not a result
                    log.warning("transit check: no reference paths for AS%s (notices kept as they are)", u)
                    unknown.add((prefix, u))
                    continue
                for t, n in sorted(ref.items(), key=lambda x: -x[1]):
                    ours = adj.get((u, t), 0)
                    bad = n >= self.cfg.transit_filter_min_peers and ours == 0
                    rows.append({"prefix": prefix, "upstream": u, "transit": t, "ref": n, "ours": ours,
                                 "elsewhere": seen.get(t, 0), "flag": bad})
                    if bad:
                        flagged.add((prefix, u, t, n, seen.get(t, 0), version))
        self.last_result = rows
        names = {r["asn"]: r["name"] for r in conn.execute("SELECT asn, name FROM asn_names")}
        # transits that never appear in our own paths won't have been looked up by the names loop
        for asn in {r["upstream"] for r in rows} | {r["transit"] for r in rows}:
            if asn not in names:
                try:
                    names[asn] = await ripe.as_name(asn, self.svc.sourceapp)
                    conn.execute("INSERT OR REPLACE INTO asn_names(asn,name,fetched) VALUES(?,?,?)",
                                 (asn, names[asn], time.time()))
                except Exception:
                    pass
        conn.commit()
        nm = lambda a: f"AS{a}" + (f" ({names[a].split(' ')[0]})" if names.get(a) else "")
        open_now = {k: a for k, a in eng.anomalies.items() if k.startswith("tf|")}
        current = set()
        for prefix, u, t, n, elsewhere, version in flagged:
            key = f"tf|{prefix}|{u}|{t}"
            current.add(key)
            self.pending[key] = self.pending.get(key, 0) + 1
            if key in open_now or self.pending[key] < 2:
                continue  # needs two consecutive checks before it is reported
            how = (f"{nm(t)} reaches your prefix another way ({elsewhere} peers)" if elsewhere
                   else f"{nm(t)} doesn't carry your prefix at all")
            eid = eng._new_event(
                "transit_filter", "info", prefix, time.time(),
                f"{nm(t)} isn't carrying {prefix} from {nm(u)}",
                {"tf_key": key, "upstream": u, "transit": t, "ref_peers": n, "our_peers": 0,
                 "elsewhere_peers": elsewhere, "reference_prefixes": self.ref_prefixes.get((u, version), (0, []))[1],
                 "note": f"{nm(t)} carries {nm(u)}'s own prefixes to {n} RIS peers but none of {prefix} via "
                         f"{nm(u)}; {how}. Usually a prefix filter or IRR issue between {nm(u)} and {nm(t)}."})
            eng.anomalies[key] = {"id": eid, "zero_since": None, "sev": "info"}
        def undecided(k):   # tf|prefix|upstream|transit
            _, prefix, u, _t = k.split("|")
            return prefix in skipped or (prefix, int(u)) in unknown
        for key in list(self.pending):
            if key not in current and not undecided(key):
                del self.pending[key]
        for key, a in open_now.items():
            if key in current:
                self.clean.pop(key, None)
            elif not undecided(key):
                # like opening, clearing needs two consecutive checks, so one odd snapshot doesn't flap it
                self.clean[key] = self.clean.get(key, 0) + 1
                if self.clean[key] >= 2:
                    eng._close_event(a["id"], time.time())
                    del eng.anomalies[key]
                    del self.clean[key]

    def close_all(self, why):
        eng = self.svc.engine
        for key in [k for k in eng.anomalies if k.startswith("tf|")]:
            a = eng.anomalies.pop(key)
            # closed quietly: no notification when the check is switched off
            eng.conn.execute("UPDATE events SET end_ts=? WHERE id=? AND end_ts IS NULL", (time.time(), a["id"]))
            eng.conn.commit()
        self.pending.clear()
        self.clean.clear()
        self.last_result = []
