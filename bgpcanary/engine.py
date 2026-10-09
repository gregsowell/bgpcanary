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

log = logging.getLogger("bgpcanary.engine")



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
        self.samples = deque()     # (ts, visible) for baseline
        self.ups_hist = deque()    # (ts, {upstream: peers}) per bucket, for upstream-shift detection
        self.shift_hits = 0        # consecutive buckets over the shift threshold
        self.bucket_min = None
        self.bucket_updates = 0
        self.bucket_withdrawals = 0
        self.last_update = 0.0

    def baseline(self):
        if not self.samples:
            return None
        vals = sorted(v for _, v in self.samples)
        if len(vals) < 20:
            return float(vals[-1])
        return float(vals[int(len(vals) * 0.95) - 1])


class Engine:
    def __init__(self, cfg, conn, restore=True):
        self.cfg = cfg
        self.conn = conn
        self.asn = cfg.asn
        self.nets = {p: ipaddress.ip_network(p) for p in cfg.prefixes}
        self.owned = [ipaddress.ip_network(p, strict=False) for p in cfg.owned_space]
        self.mons = {p: PrefixMon(p) for p in cfg.prefixes}
        self.rib: dict[str, dict[tuple, Entry]] = {}
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
            elif detail.get("tf_key") or detail.get("ext_key"):   # managed by transit.py / registry.py
                k = detail.get("tf_key") or detail.get("ext_key")
                self.anomalies[k] = {"id": r["id"], "zero_since": None, "sev": r["severity"]}
            elif detail.get("key"):
                self.anomalies[detail["key"]] = {"id": r["id"], "zero_since": None, "sev": r["severity"]}

    def seed_baseline(self, at=None):
        at = at or time.time()
        cutoff = at - self.cfg.baseline_hours * 3600
        for p, m in self.mons.items():
            m.samples.clear()
            for r in self.conn.execute(
                    "SELECT ts, visible FROM buckets WHERE prefix=? AND ts>=? AND ts<? ORDER BY ts", (p, cutoff, at)):
                m.samples.append((r["ts"], r["visible"]))

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
        return sum(1 for e in self.rib.get(prefix, {}).values() if e.path and e.path[-1] == self.asn)

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
        if up is not None and (prefix, up) not in self.seen_upstreams:
            self.seen_upstreams.add((prefix, up))
            self.conn.execute("INSERT OR IGNORE INTO seen_upstreams(prefix,asn,first_seen,last_seen) VALUES(?,?,?,?)",
                              (prefix, up, ts, ts))
            if self.status.get("seeding") is not True and not self.cfg.expected_upstreams:
                self._new_event("new_upstream", "warning", prefix, ts,
                                f"New upstream AS{up} seen for {prefix}",
                                {"asn": up, "path": p}, end_ts=ts)
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
        base = m.baseline()
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
            base = m.baseline()
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
                    m.event_id = self._new_event(
                        "outage", sev, p, m.dip_start,
                        f"{p} visibility dropped to {m.dip_min}/{base:.0f} RIS peers",
                        {"baseline": base, "threshold_pct": self.cfg.outage_threshold_pct},
                        min_visible=m.dip_min, baseline=base)
            elif m.state == "down" and m.rec_start is not None and now - m.rec_start >= S:
                self._close_event(m.event_id, m.rec_start, min_visible=m.min_vis, severity=m.event_sev,
                                  title=f"{p} outage: visibility fell to {m.min_vis}/{base:.0f} RIS peers")
                m.state, m.event_id, m.dip_start, m.dip_min, m.rec_start = "ok", None, None, None, None
            if m.state == "ok" and m.dip_start is None and v > 0:
                m.samples.append((now, v))
            while m.samples and m.samples[0][0] < now - self.cfg.baseline_hours * 3600:
                m.samples.popleft()
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
                    elif exp and paths.upstream(e.path) not in exp and paths.upstream(e.path) is not None:
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
            if key in counts or key.startswith(("tf|", "reg|", "shift|")):   # managed elsewhere
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
                chains, ups = {}, {}
                for e in self.rib.get(p, {}).values():
                    if e.path and e.path[-1] == self.asn:
                        c = paths.chain(e.path)
                        chains[c] = chains.get(c, 0) + 1
                        u = str(paths.upstream(e.path))
                        ups[u] = ups.get(u, 0) + 1
                self.conn.execute(
                    "INSERT OR REPLACE INTO buckets(prefix,ts,visible,baseline,min_visible,chains,upstreams,updates,withdrawals)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (p, t, v, m.baseline(), m.bucket_min if m.bucket_min is not None else v,
                     json.dumps(chains), json.dumps(ups), m.bucket_updates, m.bucket_withdrawals))
                m.bucket_min = v
                m.bucket_updates = m.bucket_withdrawals = 0
                self._detect_shift(p, m, t + self.B, v, {int(k): n for k, n in ups.items() if k != "None"})
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
                m.samples.append((ts, v))

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
