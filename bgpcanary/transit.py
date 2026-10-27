"""Transit filter check (informational).

For each of your direct upstreams U, look at how U's *own* prefixes reach the internet (RIPEstat
looking glass): every AS T seen directly in front of U is one of U's transits/peers. If T carries U's
own prefixes to a meaningful number of RIS peers but none of your prefix straight from U, then T isn't getting
(or isn't accepting) your route from U - typically a prefix-filter / IRR issue on U's or T's side.

Unless T reaches you another way. A network passes on only its best route, so T being your own neighbour, or
carrying you through another upstream of yours, explains the gap by itself and says nothing about the U-T
session; the connectivity tree reads those the same way ("reaches you another way"). T carrying you only the
long way round, through U but not straight from it, is the suspicious case: the direct path would normally win.
"""
import ipaddress
import logging
import time

from . import db, paths, ripe

log = logging.getLogger("bgpcanary.transit")
KV_TOGGLE = "transit_filter_enabled"


def enabled(cfg, conn):
    return bool(db.kv_get(conn, KV_TOGGLE, cfg.transit_filter_check))


def reach_of(paths, asn, known=()):
    """How the networks on your paths reach you: ({asn: {tail: RIS peers}}, your direct neighbours). The tail is
    the path from that network to you (it first, you last); the direct neighbours are `known` (your configured
    upstreams and peers) plus whoever is right in front of you. The reading the tree uses for "not carrying you"."""
    tails, direct = {}, set(known)
    for p in paths:
        if len(p) < 2 or p[-1] != asn:
            continue
        direct.add(p[-2])
        for i, a in enumerate(p[:-1]):
            t = tails.setdefault(a, {})
            k = tuple(p[i:])
            t[k] = t.get(k, 0) + 1
    return tails, direct


def verdict(tails, direct, asn, u, t):
    """How `t`, a transit or peer of your upstream `u`, carries you, from reach_of's reading:
      how: "carries" (straight from u) / "direct" (your own neighbour, with its own path to you) / "other"
           (through someone else, not u at all) / "round" (through u, but not straight from it) / "never";
      ours: RIS peers seeing it carry you straight from u; n: the peers behind the verdict; elsewhere: peers whose
      path has it anywhere; via: its next hops toward you on the paths behind the verdict, most common first.
    "direct" and "other" are a network using its best route: nothing to report. "round" is: it takes the long way
    round although the direct path would normally win, so your prefix is probably not accepted on the u-t session."""
    tl = tails.get(t, {})
    ours = sum(n for k, n in tl.items() if len(k) >= 3 and k[1] == u)
    other = {k: n for k, n in tl.items() if u not in k[1:]}
    rnd = {k: n for k, n in tl.items() if u in k[1:] and k[1] != u}
    how = "carries" if ours else "direct" if t in direct else "other" if other else "round" if rnd else "never"
    on = other if how == "other" else rnd if how == "round" else {}
    via = {}
    for k, n in on.items():
        via[k[1]] = via.get(k[1], 0) + n
    n = ours if how == "carries" else tl.get((t, asn), 0) if how == "direct" else sum(on.values())
    return {"how": how, "ours": ours, "n": n, "elsewhere": sum(tl.values()),
            "via": sorted(via, key=lambda a: -via[a])}


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

    def _mine(self, prefix):
        """Your paths for `prefix` from the live RIB (origin last)."""
        return [e.path for e in list(self.svc.engine.rib.get(prefix, {}).values())
                if e.path and e.path[-1] == self.cfg.asn]

    async def run(self):
        eng, conn = self.svc.engine, self.svc.aconn
        if not enabled(self.cfg, conn):
            self.close_all("check turned off")
            return
        self.last_run = time.time()
        rows = []
        flagged = {}      # (prefix, upstream, transit) -> (reference peers, verdict, ip version)
        skipped = set()
        unknown = set()   # (prefix, upstream) we couldn't evaluate this round: leave their notices as they are
        known = set(self.cfg.expected_upstreams) | set(self.cfg.peers)
        for prefix, mon in eng.mons.items():
            if mon.state == "down" or mon.dip_start:
                skipped.add(prefix)  # already degraded (an outage event covers it); keep its notices as they are
                continue
            version = ipaddress.ip_network(prefix, strict=False).version
            mine = self._mine(prefix)
            tails, direct = reach_of(mine, self.cfg.asn, known)
            # (not peers: they're meant to pass your routes only to their customers, never to their transits)
            ups = {p[-2] for p in mine if len(p) >= 3 and not eng.peer_group(p[-2])} | set(self.cfg.expected_upstreams)
            for u in sorted(ups):
                if not any(len(p) >= 3 and p[-2] == u for p in mine):
                    unknown.add((prefix, u))
                    continue  # upstream not carrying us at all - upstream_missing covers that
                if f"unexpected_upstream|{prefix}|{u}" in eng.anomalies:
                    # not known to be an upstream of yours (a leak, or a neighbour not filed yet): its transits aren't
                    # judged until it's filed as a transit or a peer (the warning's event page)
                    unknown.add((prefix, u))
                    continue
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
                    v = verdict(tails, direct, self.cfg.asn, u, t)
                    # one that reaches you another way isn't a gap: it's using its best route
                    bad = n >= self.cfg.transit_filter_min_peers and v["how"] in ("round", "never")
                    rows.append({"prefix": prefix, "upstream": u, "transit": t, "ref": n, "flag": bad, **v})
                    if bad:
                        flagged[(prefix, u, t)] = (n, v, version)
        self.last_result = rows
        names = {r["asn"]: r["name"] for r in conn.execute("SELECT asn, name FROM asn_names")}
        # transits that never appear in our own paths won't have been looked up by the names loop (nor, on a quiet
        # instance, the network a roundabout path goes through)
        for asn in {r["upstream"] for r in rows} | {r["transit"] for r in rows} | {a for r in rows for a in r["via"][:1]}:
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
        for (prefix, u, t), (n, v, version) in flagged.items():
            key = f"tf|{prefix}|{u}|{t}"
            current.add(key)
            self.pending[key] = self.pending.get(key, 0) + 1
            if key in open_now or self.pending[key] < 2:
                continue  # needs two consecutive checks before it is reported
            if v["how"] == "round":
                title = f"{nm(t)} isn't carrying {prefix} straight from {nm(u)}"
                how = (f"{prefix} only the long way round, via {nm(v['via'][0])} and then {nm(u)} ({v['n']} peers). "
                       f"The direct path would normally win, so your prefix is probably not accepted on the "
                       f"{nm(u)}–{nm(t)} session itself")
            else:
                title = f"{nm(t)} isn't carrying {prefix} from {nm(u)}"
                how = f"none of {prefix}: it doesn't carry your prefix at all"
            eid = eng._new_event(
                "transit_filter", "info", prefix, time.time(), title,
                {"tf_key": key, "upstream": u, "transit": t, "ref_peers": n, "our_peers": 0, "how": v["how"],
                 "via": v["via"][:3], "elsewhere_peers": v["elsewhere"],
                 "reference_prefixes": self.ref_prefixes.get((u, version), (0, []))[1],
                 "note": f"{nm(t)} carries {nm(u)}'s own prefixes to {n} RIS peers straight from {nm(u)}, but {how}. "
                         f"Usually a prefix filter or IRR issue between {nm(u)} and {nm(t)}."})
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
