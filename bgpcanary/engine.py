"""Routing state + detection.

The engine keeps an in-memory RIB of every RIS peer's current path for the monitored
prefixes (plus more-specifics and anything else our ASN originates). Updates come from
RIS Live (live), BGPlay (backfill) and the RIPEstat looking glass (resync). Every
`bucket_seconds` the state is written to the `buckets` table and detection runs.

All methods here are synchronous and must be called from one thread at a time.
"""
import ipaddress
import json
import logging
import time
from collections import deque
from dataclasses import dataclass

from . import db, paths
from .peering import Peering

log = logging.getLogger("bgpcanary.engine")
XL_HOLD = 900   # "exchange peers lost" closes once the peers have been back this long (s)



@dataclass
class Entry:
    path: list | None     # normalized path, None = withdrawn (tombstone)
    ts: float
    peer_asn: int | None


class PrefixMon:
    def __init__(self, prefix):
        self.prefix = prefix
        self.state = "ok"          # ok | down
        self.dip_start = None
        self.dip_min = None
        self.rec_start = None
        self.event_id = None
        self.event_sev = None
        self.min_vis = None
        self.ref_base = None       # while down: the baseline the outage opened with (see current_baseline)
        self.samples = deque()     # (ts, visible) for baseline; changed only through the *_sample(s) methods below
        self._hist = {}            # visible -> how many samples have it, so baseline() needn't sort every sample
        self._ver = 0              # bumped whenever the samples change
        self._base = (-1, None)    # (version, baseline) last computed
        self.ups_hist = deque()    # (ts, {upstream: peers}) per bucket, for upstream-shift detection
        self.shift_hits = 0        # consecutive buckets over the shift threshold
        self.bucket_min = None
        self.bucket_updates = 0
        self.bucket_withdrawals = 0
        self.stored = None         # (ts, chains, upstreams) of the last bucket written (see Engine.tick)
        self.last_update = 0.0

    def add_sample(self, ts, v):
        self.samples.append((ts, v))
        self._hist[v] = self._hist.get(v, 0) + 1
        self._ver += 1

    def drop_samples_before(self, cutoff):
        while self.samples and self.samples[0][0] < cutoff:
            _, v = self.samples.popleft()
            if self._hist.get(v, 0) > 1:
                self._hist[v] -= 1
            else:
                self._hist.pop(v, None)
            self._ver += 1

    def clear_samples(self):
        self.samples.clear()
        self._hist.clear()
        self._ver += 1

    def baseline(self):
        """The 95th percentile of visibility over the window (the highest reading while there are fewer than 20).
        Kept as counts per value and cached until the samples change: replaying 90 days of history asks for it
        several times per 30-second bucket."""
        ver = self._ver   # (read first: a page asking while the history replay adds samples can't cache a stale value)
        if self._base[0] != ver:
            n, b, acc = len(self.samples), None, 0
            rank = int(n * 0.95) if n >= 20 else n
            for v in sorted(self._hist):
                acc += self._hist.get(v, 0)
                if n and acc >= rank:
                    b = float(v)
                    break
            self._base = (ver, b)
        return self._base[1]

    def current_baseline(self):
        """The baseline to judge visibility against. During an outage that's the healthy baseline it opened with:
        the rolling one stops learning while down (its samples age out, and after a restart it would be rebuilt
        from the outage's own low readings), so a long outage could otherwise never be seen to recover."""
        if self.state == "down" and self.ref_base:
            return self.ref_base
        return self.baseline()


class Engine:
    def __init__(self, cfg, conn, restore=True, peering=None):
        self.cfg = cfg
        self.conn = conn
        self.peering = peering or Peering(cfg, conn)   # which neighbours are peers (exchanges, listed peers)
        self.xhist = {}   # (prefix, exchange) -> peer counts over the baseline window, for "exchange peers lost"
        self.lhist = {}   # (prefix, peer asn) -> its usual reach, for "peer leak"
        self.asn = cfg.asn
        self.nets = {p: ipaddress.ip_network(p) for p in cfg.prefixes}
        self.owned = [ipaddress.ip_network(p, strict=False) for p in cfg.owned_space]
        self.mons = {p: PrefixMon(p) for p in cfg.prefixes}
        self.rib: dict[str, dict[tuple, Entry]] = {}
        # per prefix, bumped whenever one of its paths changes (apply() is the only place that changes the RIB), so
        # what's counted from the paths every bucket is only counted again after a change: see visible(), _rib_summary()
        self.rib_ver: dict[str, int] = {}
        self._vis, self._summ = {}, {}
        self.anomalies: dict[str, dict] = {}
        self.B = cfg.bucket_seconds
        self.last_bucket = None
        self.historical = False
        self.quiet = False     # suppress per-update outage tracking during bulk loads
        self.seen_upstreams = set()
        self.seen_chains = set()
        self.listeners = []  # callables(event_id, phase) for live events
        self.status = {"phase": "starting", "ws_connected": False, "last_msg": None,
                       "msgs": 0, "last_resync": None, "resync_changes": None, "backfill": None}
        self.lg_peers = set()   # (prefix, collector, peer) ever present in a looking-glass snapshot
        self._load_seen()
        if restore:
            self._restore_open_events()

    # ------------------------------------------------------------------ setup
    def _load_seen(self):
        for r in self.conn.execute("SELECT prefix, asn FROM seen_upstreams"):
            self.seen_upstreams.add((r["prefix"], r["asn"]))
        for r in self.conn.execute("SELECT prefix, chain FROM seen_chains"):
            self.seen_chains.add((r["prefix"], r["chain"]))
            # chain-change events key on "transit upstream" pairs; older rows stored 3 hops
            self.seen_chains.add((r["prefix"], " ".join(r["chain"].split()[-2:])))

    def _restore_open_events(self):
        for r in self.conn.execute("SELECT * FROM events WHERE end_ts IS NULL"):
            detail = json.loads(r["detail"] or "{}")
            if r["kind"] == "outage" and r["prefix"] in self.mons:
                m = self.mons[r["prefix"]]
                m.state, m.event_id, m.event_sev = "down", r["id"], r["severity"]
                m.min_vis = r["min_visible"] if r["min_visible"] is not None else 0
                m.ref_base = r["baseline"] or detail.get("baseline")   # stored when the outage opened
            elif detail.get("tf_key") or detail.get("ext_key"):   # managed by transit.py / registry.py
                k = detail.get("tf_key") or detail.get("ext_key")
                self.anomalies[k] = {"id": r["id"], "zero_since": None, "sev": r["severity"]}
            elif detail.get("key"):
                self.anomalies[detail["key"]] = {"id": r["id"], "zero_since": None, "sev": r["severity"],
                                                 # exchange peers lost / peer leak: what they're judged against
                                                 "ref": detail.get("baseline"), "thr": detail.get("threshold"),
                                                 "peak": detail.get("reach", 0), "low": detail.get("carrying")}

    def seed_baseline(self, at=None):
        at = at or time.time()
        cutoff = at - self.cfg.baseline_hours * 3600
        for p, m in self.mons.items():
            m.clear_samples()
            for r in self.conn.execute(
                    "SELECT ts, visible FROM buckets WHERE prefix=? AND ts>=? AND ts<? ORDER BY ts", (p, cutoff, at)):
                m.add_sample(r["ts"], r["visible"])

    def last_bucket_ts(self):
        r = self.conn.execute("SELECT MAX(ts) AS t FROM buckets").fetchone()
        return r["t"]

    # ------------------------------------------------------------------ helpers
    def relevant(self, prefix: str, path: list | None) -> bool:
        try:
            net = ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            return False
        for n in self.nets.values():
            if net.version == n.version and (net.subnet_of(n) or net.supernet_of(n)):
                return True   # ours, more-specific, or covering
        if not path or self.asn not in path:
            return False
        if path[-1] == self.asn and self._owned(net):
            return False   # our own announcement inside space we hold (owned_space) - expected, not tracked
        if prefix in self.rib:
            return True
        # Anything else our ASN originates or appears in is tracked, up to a cap, so a large network that
        # only lists some of its prefixes can't flood the RIB/DB. Hitting the cap posts one FYI notice.
        unmonitored = sum(1 for p in self.rib if self._parent(p) is None and p not in self.mons)
        if unmonitored >= self.cfg.max_unmonitored:
            if not self.status.get("unmonitored_capped"):
                self.status["unmonitored_capped"] = True
                self._new_event(
                    "tracking_limit", "info", self.cfg.prefixes[0] if self.cfg.prefixes else None, time.time(),
                    f"Tracking limit reached: AS{self.asn} appears in more than {self.cfg.max_unmonitored} "
                    f"unmonitored prefixes", {"note": "Add the address space you hold under Settings > Monitoring "
                                                      "('Other address space you hold') so your own announcements "
                                                      "aren't tracked as unexpected. New unmonitored prefixes are "
                                                      "ignored until then."}, end_ts=time.time())
            return False
        return True

    def _owned(self, net):
        return any(net.version == o.version and (net == o or net.subnet_of(o)) for o in self.owned)

    def _parent(self, prefix):
        cache = self.__dict__.setdefault("_parent_cache", {})
        if prefix not in cache:
            net = ipaddress.ip_network(prefix, strict=False)
            # exact match wins; otherwise the most specific monitored prefix that contains it
            # (so a monitored /24 inside a monitored /22 is treated as itself, not as a more-specific)
            covering = [(n.prefixlen, p) for p, n in self.nets.items()
                        if net.version == n.version and net.subnet_of(n)]
            cache[prefix] = max(covering)[1] if covering else None
        return cache[prefix]

    def visible(self, prefix):
        ver = self.rib_ver.get(prefix, 0)   # (read first, as in PrefixMon.baseline)
        c = self._vis.get(prefix)
        if c is None or c[0] != ver:
            c = self._vis[prefix] = (ver, sum(1 for e in self.rib.get(prefix, {}).values()
                                              if e.path and e.path[-1] == self.asn))
        return c[1]

    def _rib_summary(self, prefix):
        """For a bucket row: (chains JSON, upstreams JSON, {neighbour: RIS peers reaching you through it}). Counted
        again only after the prefix's paths change: a replayed week has 20,160 buckets, most of them quiet."""
        ver = self.rib_ver.get(prefix, 0)
        c = self._summ.get(prefix)
        if c is None or c[0] != ver:
            chains, ups = {}, {}
            for e in self.rib.get(prefix, {}).values():
                if e.path and e.path[-1] == self.asn:
                    ch = paths.chain(e.path)
                    chains[ch] = chains.get(ch, 0) + 1
                    u = str(paths.upstream(e.path))
                    ups[u] = ups.get(u, 0) + 1
            c = self._summ[prefix] = (ver, json.dumps(chains), json.dumps(ups),
                                      {int(k): n for k, n in ups.items() if k != "None"})
        return c[1:]

    def peer_group(self, asn):
        """Where neighbour `asn` peers with you: an exchange's name or "Private peering". None for transit or an
        unknown neighbour (see peering.py)."""
        return self.peering.group(asn)

    def current_paths(self, prefix):
        out = []
        for (coll, peer), e in sorted(self.rib.get(prefix, {}).items()):
            if e.path:
                out.append({"collector": coll, "peer": peer, "peer_asn": e.peer_asn, "path": e.path, "ts": e.ts})
        return out

    def _emit(self, event_id, phase):
        if self.historical:
            return
        for fn in self.listeners:
            try:
                fn(event_id, phase)
            except Exception:
                log.exception("listener failed")

    def _new_event(self, kind, severity, prefix, start_ts, title, detail=None, end_ts=None,
                   min_visible=None, baseline=None):
        cur = self.conn.execute(
            "INSERT INTO events(kind,severity,prefix,start_ts,end_ts,title,detail,min_visible,baseline,historical)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kind, severity, prefix, start_ts, end_ts, title, json.dumps(detail or {}), min_visible, baseline,
             1 if self.historical else 0))
        eid = cur.lastrowid
        self.conn.commit()
        log.info("event %s [%s] %s: %s", eid, severity, kind, title)
        self._emit(eid, "open")
        return eid

    def _close_event(self, eid, end_ts, **fields):
        sets = ["end_ts=?"]
        vals = [end_ts]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(eid)
        self.conn.execute(f"UPDATE events SET {', '.join(sets)} WHERE id=?", vals)
        self.conn.commit()
        self._emit(eid, "close")

    # ------------------------------------------------------------------ updates
    def apply(self, ts, collector, peer, peer_asn, prefix, kind, path, communities=None, source="ris-live",
              store=True):
        """Apply one announcement (kind 'A') or withdrawal ('W'). Returns True if state changed."""
        collector = paths.collector_name(collector)
        norm = paths.normalize(path) if kind == "A" else None
        table = self.rib.setdefault(prefix, {})
        key = (collector, peer)
        cur = table.get(key)
        if cur and cur.ts > ts:
            return False  # older than what we already know
        changed = (cur.path if cur else None) != norm
        if kind == "W" and not cur:
            changed = False
        table[key] = Entry(norm, ts, peer_asn if peer_asn is not None else (cur.peer_asn if cur else None))
        if changed:
            self.rib_ver[prefix] = self.rib_ver.get(prefix, 0) + 1
        if store and (changed or source not in ("resync",)):
            self.conn.execute(
                "INSERT INTO updates(ts,source,collector,peer,peer_asn,prefix,kind,path,communities)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (ts, source, collector, peer, peer_asn, prefix, kind,
                 " ".join(map(str, norm)) if norm else None,
                 json.dumps(communities) if communities else None))
        m = self.mons.get(prefix)
        if m:
            m.last_update = max(m.last_update, ts)
            if kind == "W":
                m.bucket_withdrawals += 1
            else:
                m.bucket_updates += 1
            if norm and norm[-1] == self.asn:
                self._check_new_hops(prefix, norm, ts)
            if changed:
                v = self.visible(prefix)
                m.bucket_min = v if m.bucket_min is None else min(m.bucket_min, v)
                if not self.quiet:
                    self._track(m, ts, v)
        return changed

    def withdraw_peer(self, ts, collector, peer, source="peer-down"):
        collector = paths.collector_name(collector)
        for prefix, table in self.rib.items():
            e = table.get((collector, peer))
            if e and e.path:
                self.apply(ts, collector, peer, e.peer_asn, prefix, "W", None, source=source)

    def _check_new_hops(self, prefix, p, ts):
        up = paths.upstream(p)
        group = self.peer_group(up) if up is not None else None
        if up is not None and (prefix, up) not in self.seen_upstreams:
            self.seen_upstreams.add((prefix, up))
            self.conn.execute("INSERT OR IGNORE INTO seen_upstreams(prefix,asn,first_seen,last_seen) VALUES(?,?,?,?)",
                              (prefix, up, ts, ts))
            if self.status.get("seeding") is not True:
                if group:
                    self._new_event("new_peer", "info", prefix, ts, f"New peer AS{up} ({group}) carries {prefix}",
                                    {"asn": up, "exchange": group, "path": p}, end_ts=ts)
                elif not self.cfg.expected_upstreams:
                    self._new_event("new_upstream", "warning", prefix, ts,
                                    f"New upstream AS{up} seen for {prefix}",
                                    {"asn": up, "path": p}, end_ts=ts)
        if group:
            return   # a peer's path isn't an upstream chain
        ch = paths.chain(p, 2)  # transit -> upstream; 3 hops would fire once per peer network
        if ch and (prefix, ch) not in self.seen_chains:
            self.seen_chains.add((prefix, ch))
            self.conn.execute("INSERT OR IGNORE INTO seen_chains(prefix,chain,first_seen,last_seen) VALUES(?,?,?,?)",
                              (prefix, ch, ts, ts))
            if self.status.get("seeding") is not True:
                self._new_event("chain_change", "info", prefix, ts,
                                f"New upstream chain {ch} for {prefix}", {"chain": ch, "path": p}, end_ts=ts)

    # ------------------------------------------------------------------ outage state machine
    def _track(self, m, ts, v):
        base = m.current_baseline()
        if base is None or base <= 0:
            return
        th = base * self.cfg.outage_threshold_pct / 100
        rth = base * self.cfg.recover_threshold_pct / 100
        if m.state == "ok":
            if v < th:
                if m.dip_start is None:
                    m.dip_start, m.dip_min = ts, v
                else:
                    m.dip_min = min(m.dip_min, v)
            elif m.dip_start is not None and ts - m.dip_start < 2:
                m.dip_start = m.dip_min = None   # same-instant churn, not a real dip
            elif m.dip_start is not None:
                self._new_event("blip", "info", m.prefix, m.dip_start,
                                f"Brief visibility dip on {m.prefix}: {m.dip_min}/{base:.0f} peers "
                                f"for {ts - m.dip_start:.0f}s",
                                {"duration": ts - m.dip_start}, end_ts=ts, min_visible=m.dip_min, baseline=base)
                m.dip_start = m.dip_min = None
        else:
            if m.min_vis is None or v < m.min_vis:
                m.min_vis = v
                self.conn.execute("UPDATE events SET min_visible=? WHERE id=?", (v, m.event_id))
            if v >= rth:
                if m.rec_start is None:
                    m.rec_start = ts
            else:
                m.rec_start = None
            if m.event_sev == "warning" and v < base * 0.5:
                m.event_sev = "critical"
                self.conn.execute("UPDATE events SET severity='critical', min_visible=?, title=? WHERE id=?",
                                  (v, f"{m.prefix} visibility dropped to {v}/{base:.0f} RIS peers", m.event_id))
                self.conn.commit()
                self._emit(m.event_id, "escalate")

    def _detect(self, now):
        S = self.cfg.sustain_seconds
        for p, m in self.mons.items():
            base = m.current_baseline()
            v = self.visible(p)
            if base:
                # catch state changes that happened without per-update tracking (bulk loads)
                if m.state == "ok" and m.dip_start is None and v < base * self.cfg.outage_threshold_pct / 100:
                    m.dip_start, m.dip_min = now, v
                elif m.state == "down" and m.rec_start is None and v >= base * self.cfg.recover_threshold_pct / 100:
                    m.rec_start = now
            if m.state == "ok" and m.dip_start is not None and base and v >= base * self.cfg.outage_threshold_pct / 100:
                # recovered via a bulk load (per-update recoveries are handled in _track)
                m.dip_start = m.dip_min = None
            if m.state == "ok" and m.dip_start is not None and now - m.dip_start >= S and base:
                th = base * self.cfg.outage_threshold_pct / 100
                if v < th:
                    sev = "critical" if m.dip_min < base * 0.5 else "warning"
                    m.state, m.event_sev, m.min_vis, m.rec_start = "down", sev, m.dip_min, None
                    m.ref_base = base
                    m.event_id = self._new_event(
                        "outage", sev, p, m.dip_start,
                        f"{p} visibility dropped to {m.dip_min}/{base:.0f} RIS peers",
                        {"baseline": base, "threshold_pct": self.cfg.outage_threshold_pct},
                        min_visible=m.dip_min, baseline=base)
            elif m.state == "down" and m.rec_start is not None and now - m.rec_start >= S:
                self._close_event(m.event_id, m.rec_start, min_visible=m.min_vis, severity=m.event_sev,
                                  title=f"{p} outage: visibility fell to {m.min_vis}/{base:.0f} RIS peers")
                m.state, m.event_id, m.dip_start, m.dip_min, m.rec_start = "ok", None, None, None, None
                m.ref_base = None
            if m.state == "ok" and m.dip_start is None and v > 0:
                m.add_sample(now, v)
            m.drop_samples_before(now - self.cfg.baseline_hours * 3600)
        self._detect_anomalies(now)

    def _anomaly_counts(self):
        """Returns {key: (kind, severity, prefix, title, count, first_ts, detail)}."""
        out = {}
        exp = set(self.cfg.expected_upstreams)
        for prefix, table in self.rib.items():
            parent = self._parent(prefix)
            for (coll, peer), e in table.items():
                if not e.path:
                    continue
                org = e.path[-1]
                items = []
                if parent == prefix:
                    if org != self.asn:
                        items.append(("origin_hijack", "critical",
                                      f"{prefix} originated by AS{org} (expected AS{self.asn})"))
                    elif (exp and paths.upstream(e.path) not in exp and paths.upstream(e.path) is not None
                          and not self.peer_group(paths.upstream(e.path))):   # (peers have checks of their own)
                        up = paths.upstream(e.path)
                        items.append(("unexpected_upstream", "warning",
                                      f"{prefix} seen via unexpected upstream AS{up}"))
                elif parent:
                    sev = "warning" if org == self.asn else "critical"
                    items.append(("more_specific", sev, f"More-specific {prefix} of {parent} originated by AS{org}"))
                elif org == self.asn:
                    if not self._owned(ipaddress.ip_network(prefix, strict=False)):
                        items.append(("unexpected_prefix", "warning",
                                      f"AS{self.asn} originating unmonitored {prefix}"))
                elif self.asn in e.path:
                    # our ASN in the middle of someone else's path: a leak through us, or a forged path
                    items.append(("asn_in_path", "warning",
                                  f"AS{self.asn} appears in the path to {prefix} (origin AS{org})"))
                else:
                    net = ipaddress.ip_network(prefix, strict=False)
                    covered = [p for p, n in self.nets.items() if net.version == n.version and net.supernet_of(n)]
                    # default routes / very short prefixes leaked into a RIS feed are noise, not a covering hijack
                    if covered and prefix not in self.cfg.covering_ignore \
                            and net.prefixlen >= self.cfg.covering_min_length:
                        items.append(("covering_prefix", "warning",
                                      f"{prefix} (covering {covered[0]}) announced by AS{org}"))
                for kind, sev, title in items:
                    extra = paths.upstream(e.path) if kind == "unexpected_upstream" else org
                    key = f"{kind}|{prefix}|{extra}"
                    cur = out.get(key)
                    if cur:
                        cur[4] += 1
                        cur[5] = min(cur[5], e.ts)
                    else:
                        out[key] = [kind, sev, prefix, title, 1, e.ts,
                                    {"key": key, "example_path": e.path, "collector": coll, "peer": peer}]
        # Many hits of the same broad kind -> one summary event instead of one per prefix
        for kind, label in (("unexpected_prefix", f"AS{self.asn} is originating {{n}} unmonitored prefixes"),
                            ("asn_in_path", f"AS{self.asn} appears in the paths of {{n}} other prefixes")):
            keys = [k for k, v in out.items() if v[0] == kind]
            if len(keys) > 10:
                rows = [out.pop(k) for k in keys]
                pfxs = sorted({r[2] for r in rows})
                skey = f"{kind}|summary"
                out[skey] = [kind, "warning", self.cfg.prefixes[0] if self.cfg.prefixes else pfxs[0],
                             label.format(n=len(pfxs)), sum(r[4] for r in rows), min(r[5] for r in rows),
                             {"key": skey, "prefixes": pfxs[:50],
                              "note": "Examples: " + ", ".join(pfxs[:10]) + (" ..." if len(pfxs) > 10 else "")
                                      + ". If these are yours, list them as monitored prefixes or under "
                                        "'Other address space you hold' in Settings > Monitoring."}]
        return out

    def _missing_upstreams(self, now):
        """Expected upstreams that currently carry none of a monitored prefix's paths (live mode only)."""
        out = {}
        if self.historical or not self.cfg.expected_upstreams:
            return out
        for p in self.mons:
            ups = {}
            for e in self.rib.get(p, {}).values():
                if e.path and e.path[-1] == self.asn:
                    u = paths.upstream(e.path)
                    ups[u] = ups.get(u, 0) + 1
            if not ups:
                continue  # prefix not visible at all - that's an outage, not a missing upstream
            for u in self.cfg.expected_upstreams:
                if not ups.get(u):
                    key = f"upstream_missing|{p}|{u}"
                    out[key] = ["upstream_missing", "warning", p,
                                f"Expected upstream AS{u} is not carrying {p}", 0, now, {"key": key, "asn": u}]
        return out

    def _detect_anomalies(self, now):
        counts = self._anomaly_counts()
        counts.update(self._missing_upstreams(now))
        for key, (kind, sev, prefix, title, n, first, detail) in counts.items():
            a = self.anomalies.get(key)
            if a:
                a["zero_since"] = None
                continue
            detail["peers"] = n
            if kind != "upstream_missing" and not key.endswith("|summary"):
                title = f"{title} ({n} peers)"
            eid = self._new_event(kind, sev, prefix, first, title, detail)
            self.anomalies[key] = {"id": eid, "zero_since": None, "sev": sev}
        for key, a in list(self.anomalies.items()):
            if key in counts or key.startswith(("tf|", "reg|", "shift|", "xl|", "pl|")):   # managed elsewhere
                continue
            if a["zero_since"] is None:
                a["zero_since"] = now
            elif now - a["zero_since"] >= self.cfg.sustain_seconds:
                self._close_event(a["id"], a["zero_since"])
                del self.anomalies[key]

    def _detect_shift(self, p, m, now, v, ups):
        """Upstream shift: many RIS peers moving between your upstreams within a few minutes while visibility
        holds (a session flap / re-route an outage alert wouldn't catch). Warning; checks run from service."""
        W, pct = self.cfg.shift_window_seconds, self.cfg.shift_pct
        if m.state != "ok" or m.dip_start is not None:
            m.ups_hist.clear()   # an outage / dip is alerted on its own; don't compare across it
            m.shift_hits = 0
        m.ups_hist.append((now, ups))
        while m.ups_hist and m.ups_hist[0][0] < now - W:
            m.ups_hist.popleft()
        key = f"shift|{p}"
        open_ = self.anomalies.get(key)
        base = m.baseline() or v
        then = m.ups_hist[0][1]
        moved = sum(max(0, n - then.get(u, 0)) for u, n in ups.items())
        step = lambda a, b: sum(max(0, n - a.get(u, 0)) for u, n in b.items())   # peers moved between two buckets
        prev = m.ups_hist[-2][1] if len(m.ups_hist) > 1 else ups
        moving = base and step(prev, ups) >= base * pct / 400   # this bucket itself saw real movement
        big = (pct > 0 and base and len(m.ups_hist) > 1 and moved >= base * pct / 100 and m.state == "ok"
               and v >= base * self.cfg.outage_threshold_pct / 100)
        m.shift_hits = m.shift_hits + 1 if big else 0
        nm = lambda u: f"AS{u}"
        if open_:
            open_.setdefault("last_big", now)   # restored after a restart: count the quiet period from now
            if moving:
                open_["last_move"] = now
            if big:
                open_["last_big"] = now
                open_["peak"] = max(open_.get("peak", 0), moved)
            elif now - open_.get("last_big", now) >= self.cfg.shift_quiet_seconds:
                before = open_.get("before") or {}
                back = sum(abs(ups.get(u, 0) - before.get(u, 0)) for u in set(ups) | set(before)) / 2
                how = ("traffic went back to its previous upstreams" if back < base * pct / 200
                       else "it settled on a new mix: " + ", ".join(f"{nm(u)} {n}" for u, n in
                                                                    sorted(ups.items(), key=lambda x: -x[1])))
                self._close_event(open_["id"], open_.get("last_move") or open_.get("last_big", now),
                                  title=f"{p}: upstream shift of up to {open_.get('peak', 0)} RIS peers ended; {how}")
                del self.anomalies[key]
            return
        if m.shift_hits < 2:   # two buckets in a row, so a single resync or odd snapshot doesn't count
            return
        gained = max(ups, key=lambda u: ups[u] - then.get(u, 0))
        lost = min(then, key=lambda u: ups.get(u, 0) - then[u]) if then else None
        desc = (f"{nm(lost)} {then.get(lost, 0)}→{ups.get(lost, 0)}, {nm(gained)} {then.get(gained, 0)}→{ups[gained]}"
                if lost is not None and lost != gained else f"{nm(gained)} {then.get(gained, 0)}→{ups[gained]}")
        # start: the first bucket in the window where peers began to move (not the window's edge)
        hist = list(m.ups_hist)
        began = next((hist[i][0] - self.B for i in range(1, len(hist))
                      if step(hist[i - 1][1], hist[i][1]) >= base * pct / 400), hist[0][0])
        eid = self._new_event(
            "upstream_shift", "warning", p, began,
            f"{p}: {moved} of {base:.0f} RIS peers moved between upstreams ({desc})",
            {"key": key, "mix_before": {str(u): n for u, n in then.items()}, "mix_after": {str(u): n for u, n in ups.items()},
             "moved": moved, "baseline": base, "window_s": W,
             "note": "Visibility stayed up, but a large share of the internet changed which of your upstreams it "
                     "uses to reach you, within minutes. Usually a BGP session reset or a re-route at or behind "
                     "one upstream. Brief packet loss is common while routes converge."})
        self.anomalies[key] = {"id": eid, "zero_since": None, "sev": "warning", "last_big": now, "peak": moved,
                               "before": then, "last_move": now}

    def _detect_peering(self, p, m, now, v, nb):
        """Per bucket, for one prefix (`nb`: neighbour -> RIS peers reaching you through it):
        - exchange peers lost: at one exchange, many of the peers that normally carry the prefix stop at once;
        - peer leak: one peer carries the prefix far beyond its own customers.
        Warnings that open and close after the sustain time. An outage or dip of the prefix itself is alerted on
        its own, so neither opens then."""
        S, W = self.cfg.sustain_seconds, self.cfg.baseline_hours * 3600
        healthy = m.state == "ok" and m.dip_start is None
        groups = {}
        for u, n in nb.items():
            g = self.peer_group(u)
            if g:
                groups.setdefault(g, {})[u] = n
        # exchange peers lost: compared with how many peers there normally carry it (95th percentile, like visibility).
        # Not for private peers: each has a link of its own, so there's no one place for them to fail together.
        xp = self.cfg.exchange_loss_pct
        ixes = {ix["name"] for ix in self.peering.exchanges()}
        for k in [k for k in self.anomalies if k.startswith(f"xl|{p}|") and k.split("|", 2)[2] not in ixes]:
            self._close_event(self.anomalies.pop(k)["id"], now,   # PeeringDB doesn't list you there any more
                              title=f"{p}: {k.split('|', 2)[2]} is no longer one of your exchanges")
        for g in (set(groups) | {k[1] for k in self.xhist if k[0] == p}) & ixes:
            # samples: (ts, peers) over the window; hist: how often each count occurs in it (no sorting per bucket,
            # which mattered when replaying 90 days of history)
            h = self.xhist.setdefault((p, g), {"samples": deque(), "hist": {}, "seen": {}, "since": None})
            here = groups.get(g, {})
            for u in here:
                h["seen"][u] = now
            cnt, key = len(here), f"xl|{p}|{g}"
            a = self.anomalies.get(key)
            if a:
                ref = a.get("ref") or 0
                a["low"] = cnt if a.get("low") is None else min(a["low"], cnt)
                back = xp <= 0 or cnt >= ref * (1 - xp / 200)   # at most half the alert's share still missing
                h["since"] = (h["since"] or now) if back else None
                # closes once they've been back for 15 minutes (or the sustain time, if longer): a session that
                # flaps is one event, not one per flap
                if back and now - h["since"] >= max(S, XL_HOLD):
                    self._close_event(a["id"], h["since"],
                                      title=f"{p}: {g} peers carrying it fell to {a['low']} of {ref:.0f}")
                    del self.anomalies[key]
                    h["since"] = None
                continue
            hist = h["hist"]
            if healthy:
                h["samples"].append((now, cnt))
                hist[cnt] = hist.get(cnt, 0) + 1
            while h["samples"] and h["samples"][0][0] < now - W:
                _, old = h["samples"].popleft()
                hist[old] -= 1
                if not hist[old]:
                    del hist[old]
            base, n = 0, len(h["samples"])   # 95th percentile, as for visibility (the highest count while few)
            if n:
                rank, acc = (int(n * 0.95) if n >= 20 else n), 0
                for c in sorted(hist):
                    acc += hist[c]
                    if acc >= rank:
                        base = c
                        break
            low = xp > 0 and healthy and base >= 3 and cnt <= base * (1 - xp / 100)
            h["since"] = (h["since"] or now) if low else None
            if low and now - h["since"] >= S:
                gone = sorted(u for u, t in h["seen"].items() if u not in here and t >= now - W)
                eid = self._new_event(
                    "exchange_loss", "warning", p, h["since"], f"{p}: {base - cnt} of {base} {g} peers stopped carrying it",
                    {"key": key, "exchange": g, "baseline": base, "carrying": cnt, "lost": gone[:200],
                     "note": "Many peers at one exchange stopped carrying this prefix at the same time. That usually "
                             "points at your port or BGP sessions there, the exchange's route servers, or the exchange "
                             "itself. Those networks now reach you through your transit providers instead."})
                self.anomalies[key] = {"id": eid, "zero_since": None, "sev": "warning", "ref": base, "low": cnt}
                h["since"] = None
        # peer leak: reach far beyond its usual (the average while it's normal: over everything seen for the first
        # 6 hours, then a moving average over about 6 hours). Nothing is judged in the first 10 minutes of a peer, so
        # a big peer with many customers of its own isn't taken for a leak before its usual reach is known.
        lp, alpha = self.cfg.peer_leak_pct, min(1.0, self.B / (6 * 3600))
        current = {u: n for grp in groups.values() for u, n in grp.items()}
        open_ = {int(k.rsplit("|", 1)[1]) for k in self.anomalies if k.startswith(f"pl|{p}|")}
        for u in set(current) | open_:
            n = current.get(u, 0)
            h = self.lhist.setdefault((p, u), {"typ": None, "obs": 0, "since": None, "peak": 0})
            key = f"pl|{p}|{u}"
            a = self.anomalies.get(key)
            if a and not self.peer_group(u):   # filed as one of your transits since (or left your exchanges)
                self._close_event(a["id"], now, title=f"{p}: AS{u} isn't counted as a peer any more")
                del self.anomalies[key]
                h["since"], h["peak"] = None, 0
                continue
            if a:
                h["peak"] = max(h["peak"], a.get("peak") or 0, n)
                back = lp <= 0 or n < (a.get("thr") or 0) * 0.7
                h["since"] = (h["since"] or now) if back else None
                if back and now - h["since"] >= S:
                    self._close_event(a["id"], h["since"], title=f"{p}: peer AS{u} back to its usual reach "
                                                                 f"(at most {h['peak']} RIS peers during the leak)")
                    del self.anomalies[key]
                    h["since"], h["peak"] = None, 0
                continue
            if not healthy or v <= 0:   # an outage or dip of the prefix itself: neither judged nor learned from
                h["since"] = None
                continue
            typ = h["typ"] or 0
            thr = max(lp / 100 * v, 3 * typ, 10)
            high = lp > 0 and h["obs"] * self.B >= 600 and n >= thr
            h["since"] = (h["since"] or now) if high else None
            if not high:
                h["typ"] = n if h["typ"] is None else h["typ"] + max(alpha, 1 / (h["obs"] + 1)) * (n - h["typ"])
                h["obs"] += 1
            elif now - h["since"] >= S:
                where = self.peering.label(u)
                usual = (f" (AS{u} usually reaches about {max(1, round(typ))} of them)" if typ else "")
                eid = self._new_event(
                    "peer_leak", "warning", p, h["since"], f"{p}: peer AS{u} ({where}) carries it to {n} of {v} RIS peers",
                    {"key": key, "asn": u, "exchange": where, "reach": n, "usual": round(typ, 1), "visible": v,
                     "threshold": thr,
                     "note": f"A peer normally passes your routes only to its own customers{usual}. Carrying them to "
                             f"this much of the internet usually means it's leaking them to its own transits or other "
                             f"peers, which pulls your traffic through a network that isn't paid to carry it. If "
                             f"AS{u} is in fact one of your transit providers, mark it as one (button below)."})
                self.anomalies[key] = {"id": eid, "zero_since": None, "sev": "warning", "thr": thr, "peak": n}
                h["since"], h["peak"] = None, n

    # ------------------------------------------------------------------ buckets
    def tick(self, now):
        """Close every bucket that ended at or before `now`."""
        if self.last_bucket is None:
            self.last_bucket = int(now // self.B * self.B)
            for m in self.mons.values():
                m.bucket_min = self.visible(m.prefix)
            return
        closed = False
        while self.last_bucket + self.B <= now:
            t = self.last_bucket
            for p, m in self.mons.items():
                v = self.visible(p)
                chains, ups, nb = self._rib_summary(p)   # nb: neighbour -> RIS peers via it
                # The chains / upstreams JSON (6 KB for a prefix seen through hundreds of paths) is stored only when
                # it changes: NULL means "the same as the bucket before" (db.fill_mix fills it in). It's stored anyway
                # in the first bucket of each UTC day and after any break, so it's never more than a day back.
                same = m.stored and m.stored[0] == t - self.B and t // 86400 == m.stored[0] // 86400
                self.conn.execute(
                    "INSERT OR REPLACE INTO buckets(prefix,ts,visible,baseline,min_visible,chains,upstreams,updates,withdrawals)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (p, t, v, m.current_baseline(), m.bucket_min if m.bucket_min is not None else v,
                     None if same and chains == m.stored[1] else chains, None if same and ups == m.stored[2] else ups,
                     m.bucket_updates, m.bucket_withdrawals))
                m.stored = (t, chains, ups)
                m.bucket_min = v
                m.bucket_updates = m.bucket_withdrawals = 0
                # peers coming and going isn't an upstream shift; they have checks of their own
                self._detect_shift(p, m, t + self.B, v, {u: n for u, n in nb.items() if not self.peer_group(u)})
                self._detect_peering(p, m, t + self.B, v, nb)
            self.last_bucket += self.B
            self._detect(self.last_bucket)
            closed = True
        if not self.historical:
            self.conn.commit()   # live: called every ~2 s, keeps write transactions short

    # ------------------------------------------------------------------ bulk loaders
    def load_bgplay(self, data, seed=False, ticks=True):
        """Replay a RIPEstat BGPlay response in historical mode."""
        src_asn = {s["id"]: s.get("as_number") for s in data.get("sources", [])}
        start = _parse_ts(data["query_starttime"])
        end = _parse_ts(data["query_endtime"])
        if seed:
            self.status["seeding"] = True
            self.quiet = True
            for s in data.get("initial_state", []):
                sid = s["source_id"]
                coll, peer = sid.split("-", 1)
                if self.relevant(s["target_prefix"], paths.normalize(s["path"])):
                    self.apply(start, coll, peer, src_asn.get(sid), s["target_prefix"], "A", s["path"],
                               s.get("community"), source="bgplay-init")
            self.status["seeding"] = False
            self.quiet = False
            for m in self.mons.values():
                # loading the starting state isn't activity: without this the first bucket recorded a
                # "lowest" of 1 peer and a churn spike of every initial route
                m.bucket_min = self.visible(m.prefix)
                m.bucket_updates = m.bucket_withdrawals = 0
            if not ticks:
                self.conn.commit()
                return
            if self.last_bucket is None:
                self.last_bucket = int(start // self.B * self.B)
                self.seed_samples_now(start)
        for n, ev in enumerate(sorted(data.get("events", []), key=lambda e: e["timestamp"])):
            if n % 500 == 0:
                self.conn.commit()   # gap fills run in a thread; keep their write locks short
            ts = _parse_ts(ev["timestamp"])
            self.tick(ts)
            a = ev["attrs"]
            sid = a["source_id"]
            coll, peer = sid.split("-", 1)
            path = a.get("path") if ev["type"] == "A" else None
            if not self.relevant(a["target_prefix"], paths.normalize(path) if path else None) \
                    and a["target_prefix"] not in self.rib:
                continue
            self.apply(ts, coll, peer, src_asn.get(sid), a["target_prefix"], ev["type"], path,
                       a.get("community"), source="bgplay")
        self.tick(end)
        self.conn.commit()

    def seed_samples_now(self, ts):
        for p, m in self.mons.items():
            v = self.visible(p)
            if v:
                m.add_sample(ts, v)

    def resync_lg(self, lg, source="resync"):
        """Reconcile the RIB against a RIPEstat looking-glass snapshot. Returns number of changes."""
        latest = _parse_ts(lg.get("latest_time") or lg.get("query_time"))
        prefix = lg["resource"] if "/" in str(lg.get("resource", "")) else None
        target = {}
        for rrc in lg.get("rrcs", []):
            coll = paths.collector_name(rrc["rrc"])
            for p in rrc.get("peers", []):
                pref = p.get("prefix") or prefix
                target[(pref, coll, p["peer"])] = (p["as_path"], p.get("community"), p.get("asn_origin"))
        changes = 0
        self.lg_peers_before = set(self.lg_peers)
        self.lg_peers.update(target.keys())
        for (pref, coll, peer), (asp, comm, _) in target.items():
            norm = paths.parse_str(asp)
            e = self.rib.get(pref, {}).get((coll, peer))
            if e is None or (e.path != norm and e.ts < latest):
                peer_asn = norm[0] if norm else None
                if self.apply(latest, coll, peer, peer_asn, pref, "A", norm, comm, source=source):
                    changes += 1
        stale_before = latest - self.cfg.stale_days * 86400
        self.stale_removed = getattr(self, "stale_removed", 0)
        for pref in [prefix] if prefix else list(self.rib):
            for (coll, peer), e in list(self.rib.get(pref, {}).items()):
                if not e.path or (pref, coll, peer) in target or e.ts >= latest:
                    continue
                if (pref, coll, peer) in self.lg_peers_before:
                    # the looking glass used to show this route and no longer does: withdrawn
                    if self.apply(latest, coll, peer, e.peer_asn, pref, "W", None, source=source):
                        changes += 1
                elif self.cfg.stale_days > 0 and e.ts < stale_before:
                    # not refreshed for weeks and not confirmed by the looking glass: a stale ("zombie") entry
                    # left over from a missed withdrawal - drop it so it doesn't skew counts and the tree
                    if self.apply(e.ts + 1, coll, peer, e.peer_asn, pref, "W", None, source="stale"):
                        changes += 1
                        self.stale_removed += 1
                        log.info("dropped stale route %s via %s/%s (last update %s): %s", pref, coll, peer,
                                 time.strftime("%Y-%m-%d", time.gmtime(e.ts)), " ".join(map(str, e.path)))
        self.conn.commit()
        return changes


def close_open(engine, ts, note="data gap ended"):
    """Close outages/anomalies still open in a (gap-filler) engine at `ts`."""
    for m in engine.mons.values():
        if m.state == "down" and m.event_id:
            engine.conn.execute("UPDATE events SET end_ts=?, min_visible=?, title=title || ' (' || ? || ')' "
                                "WHERE id=?", (ts, m.min_vis, note, m.event_id))
    for a in engine.anomalies.values():
        engine.conn.execute("UPDATE events SET end_ts=? WHERE id=? AND end_ts IS NULL", (ts, a["id"]))
    engine.conn.commit()


def _parse_ts(s):
    from datetime import datetime, timezone
    if isinstance(s, (int, float)):
        return float(s)
    return datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=timezone.utc).timestamp()
