"""Orchestrates the engine, data sources, probes and alerts inside the web process."""
import asyncio
import dataclasses
import json
import logging
import os
import shutil
import time

from . import (alerts, atlas, config, db, pathfinder, paths, registry, ripe, routeviews, sanity, transit, tree, updates,
               watch)
from .engine import Engine, _parse_ts as ripe_ts, close_open
from .peering import Peering

log = logging.getLogger("bgpcanary.service")
# events that trigger an immediate Route Views snapshot + ping/traceroute burst
BURST_KINDS = ("outage", "upstream_missing", "origin_hijack", "more_specific", "covering_prefix", "upstream_shift",
               "peer_leak")
# events about routes going missing: what RIPE's data going missing looks like too
ABSENCE_KINDS = ("blip", "upstream_missing", "upstream_shift", "exchange_loss")


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        db.init(cfg.db_path)
        self.conn = db.connect(cfg.db_path)      # engine writer
        # Everything on the event loop writes through this one connection. A second writer on the
        # same thread can deadlock against the first's open transaction (sqlite busy-waits, which
        # blocks the loop). Web requests read through their own connections.
        self.aconn = self.conn
        config.apply_monitor_settings(cfg, db.kv_get(self.conn, config.MONITOR_KV))   # GUI-saved ASN/prefixes
        config.apply_alert_settings(cfg, db.kv_get(self.conn, config.ALERT_KV))       # GUI-saved alert settings
        config.apply_tuning(cfg, db.kv_get(self.conn, config.TUNING_KV))              # GUI-saved thresholds etc.
        self.peering = Peering(cfg, self.conn)   # your exchanges (PeeringDB) and listed peers, shared by every engine
        self.engine = Engine(cfg, self.conn, peering=self.peering)
        self.engine.listeners.append(self._on_event)
        self.sourceapp = f"bgpcanary-as{cfg.asn}"
        self.tasks = []
        self.live = False
        self.first_sync = asyncio.Event()
        self.transit = transit.TransitCheck(self)
        self.registry = registry.RegistryWatch(self)
        self.atlas = atlas.Atlas(self)   # optional RIPE Atlas data-plane checks (idle until an API key is set)
        pathfinder.init_db(self.conn)    # Path finder: watched targets and what each lookup / traceroute showed
        self.pending_backfill = []   # prefixes added since the database was created
        self.replays = 0             # history loads / gap fills running (they rewrite buckets; see _compact_buckets)
        self.last_burst = 0          # last event-triggered Route Views burst (merge simultaneous events)
        self.health = sanity.SourceHealth(self)   # how RIS, the looking glass, Route Views etc. look right now
        self.opening = {}            # outage id -> set once its "opened" alert is out (held while it's checked)
        # Route Views telnet budget per collector per UTC day (their rate limiter blocks an IP for the day
        # once it exceeds 100 connections / 500 KB down / 100 KB up per collector)
        self.rv_budget = db.kv_get(self.conn, "rv_budget", {}) or {}
        routeviews.on_usage = self._rv_bytes
        tree.HISTORY = self.aconn    # upstream neighbour history ("no longer peered"), written through this connection
        self.live_since = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        if not self.cfg.configured:
            # fresh install: nothing to monitor until an admin sets the ASN and prefixes (Settings > Monitoring)
            self.engine.status["phase"] = "setup required"
            log.warning("no ASN/prefixes configured yet - open the web UI to finish setup")
            return
        self.tasks.append(asyncio.create_task(self._main()))
        self.tasks.append(asyncio.create_task(self._rv_loop()))
        self.tasks.append(asyncio.create_task(self._probe_loop()))
        self.tasks.append(asyncio.create_task(self._names_loop()))
        self.tasks.append(asyncio.create_task(self._prune_loop()))
        self.tasks.append(asyncio.create_task(self.atlas.loop()))
        self.tasks.append(asyncio.create_task(updates.loop(self.aconn)))
        self.tasks.append(asyncio.create_task(watch.loop(self)))

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.conn.commit()

    async def _main(self):
        eng, B = self.engine, self.cfg.bucket_seconds
        try:   # before the history: so peers in it are recognised as peers
            await asyncio.wait_for(self.peering.refresh(), 120)
        except Exception as ex:
            log.warning("PeeringDB lookup skipped: %s", ex)
        self.tasks.append(asyncio.create_task(self._peering_loop()))
        try:
            await self._startup_history()
            await self._ensure_init_rows()
        except Exception:
            log.exception("history load failed; continuing live")
        if not self.first_sync.is_set():   # (the pages say so until the first RIS Live sync: then "live")
            eng.status.update(phase="connecting to RIS Live", connecting_since=time.time())
        eng.historical = False
        eng.last_bucket = int(time.time() // B * B)
        for m in eng.mons.values():
            m.bucket_min = None
        self.ws_down_since = None
        rl = ripe.RisLive(eng, self._run_sync, self._on_ws_connect, self._on_ws_disconnect)
        self.tasks.append(asyncio.create_task(rl.run()))
        self.tasks.append(asyncio.create_task(self._tick_loop()))
        self.tasks.append(asyncio.create_task(self._reconcile_loop()))
        self.tasks.append(asyncio.create_task(self._gap_loop()))
        self.tasks.append(asyncio.create_task(self._compact_buckets()))
        self.tasks.append(asyncio.create_task(self._neighbour_history()))
        self.tasks.append(asyncio.create_task(self._transit_loop()))
        self.tasks.append(asyncio.create_task(self._registry_loop()))
        self.tasks.append(asyncio.create_task(self._snapshot_reconcile_loop()))
        self.live_since = time.time()
        if self.pending_backfill:
            self.tasks.append(asyncio.create_task(self._backfill_new_prefixes()))

    async def _backfill_new_prefixes(self):
        """Load history for prefixes added after the database was created, one at a time, in a separate
        engine (the live engine is already monitoring them from now on)."""
        await asyncio.sleep(30)
        B = self.cfg.bucket_seconds
        for p in list(self.pending_backfill):
            now = time.time()
            try:   # through the single writer connection, as in _fill_gaps
                f = Engine(dataclasses.replace(self.cfg, prefixes=[p]), self.aconn, restore=False, peering=self.peering)
                f.historical = True
                avail = await self._replay(f, now - self.cfg.backfill_days * 86400, self.live_since or now,
                                           label=f"history for {p}", prefixes=[p], show=p)
                close_open(f, avail)
                if self.live_since and self.live_since - avail > B:
                    self._add_gap(avail, self.live_since)   # BGPlay lags a few hours; the gap filler finishes it
            except Exception:
                log.exception("history load for %s failed", p)
            finally:
                self.pending_backfill.remove(p)

    async def _peering_loop(self):
        """Keep your exchanges' member lists fresh (PeeringDB is only asked once a day, or an hour after a failure)."""
        while True:
            await asyncio.sleep(3600)
            try:
                await self.peering.refresh()
            except Exception:
                log.exception("PeeringDB refresh failed")

    async def _run_sync(self, fn):
        return fn()

    async def _on_ws_connect(self):
        await self._resync_all()
        if self.ws_down_since:
            self._add_gap(self.ws_down_since, time.time())
            self.ws_down_since = None
        if not self.first_sync.is_set():
            self.first_sync.set()
            self.engine.status["phase"] = "live"
            self.live = True

    def _on_ws_disconnect(self, ts):
        self.health.ris_disconnected()
        if self.ws_down_since is None and self.first_sync.is_set():
            self.ws_down_since = ts

    # ------------------------------------------------------------------ history / gaps
    async def _bgplay_merged(self, s, e, prefixes=None):
        merged = None
        for p in prefixes or self.cfg.prefixes:
            d = None
            for attempt in range(3):
                try:
                    d = await ripe.bgplay(p, s, e, self.sourceapp)
                    break
                except Exception as ex:
                    log.warning("bgplay %s %s attempt %d failed: %s", p,
                                time.strftime("%m-%d %H:%M", time.gmtime(s)), attempt, ex)
                    await asyncio.sleep(10)
            if d is None:
                continue
            if merged is None:
                merged = d
            else:
                merged["initial_state"] += d.get("initial_state", [])
                merged["events"] += d.get("events", [])
                merged["sources"] += d.get("sources", [])
        return merged

    async def _replay(self, eng, start, end, label="backfill", prefixes=None, show=None):
        """Replay BGPlay history [start, end) into `eng` in 7-day chunks. Returns the end of available data.
        `show`: report progress in status["history"] for the web pages: "all" for the first start's history load,
        or the prefix whose history loads in the background."""
        spans, s = [], start
        while s < end:
            spans.append((s, min(s + 7 * 86400, end)))
            s += 7 * 86400
        log.info("%s: %d chunk(s) from %s", label, len(spans), time.strftime("%Y-%m-%d %H:%M", time.gmtime(start)))
        avail, t0 = start, time.time()
        self.replays += 1
        try:
            # buckets this rewrites (e.g. the live engine's while RIS Live was cut off) may be what later ones rely
            # on for their chains / upstreams: store those in the later ones first
            for p in eng.mons:
                db.pin_mix(eng.conn, p, start, end + self.cfg.bucket_seconds)
            eng.conn.commit()
            for i, (s, e) in enumerate(spans):
                if show:
                    self.engine.status["history"] = {"for": show, "done": i, "total": len(spans), "started": t0,
                                                      "start": start, "end": end, "upto": s}
                if eng is self.engine:
                    eng.status["phase"] = f"loading history {i + 1}/{len(spans)}"
                d = await self._bgplay_merged(s, e, prefixes)
                if not d:
                    continue
                await asyncio.to_thread(eng.load_bgplay, d, i == 0)
                avail = ripe_ts(d["query_endtime"])
                log.info("%s chunk %d/%d: %d events", label, i + 1, len(spans), len(d.get("events", [])))
        finally:
            self.replays -= 1
            if show:
                self.engine.status["history"] = None
        return avail

    async def _startup_history(self):
        eng, B = self.engine, self.cfg.bucket_seconds
        now = time.time()
        eng.seed_baseline(now)
        last = eng.last_bucket_ts()
        eng.historical = True
        if last is None and self.cfg.backfill_days > 0:
            gap_start = await self._replay(eng, now - self.cfg.backfill_days * 86400, now, show="all")
            eng.status["phase"] = "finishing the history load"
            # first install: drop routes the replay still holds but RIS no longer has (silent drops)
            d = await self._bgplay_merged(now - 60, now)
            if d:
                self._reconcile_snapshot(d)
            if eng.controls:   # the control prefixes have no history: just their current routes
                d = await self._bgplay_merged(now - 60, now, list(eng.controls))
                if d:
                    await asyncio.to_thread(eng.load_bgplay, d, True, False)
        else:
            if last is not None and self.cfg.backfill_days > 0:
                self.pending_backfill = [p for p in self.cfg.prefixes if not self.conn.execute(
                    "SELECT 1 FROM buckets WHERE prefix=? LIMIT 1", (p,)).fetchone()]
                if self.pending_backfill:
                    log.info("new prefixes without history: %s (loading in the background)", self.pending_backfill)
            # Seed the RIB from BGPlay's newest snapshot (a few hours old, but it includes peers the
            # looking glass omits). The looking-glass resync then brings it up to date.
            d = await self._bgplay_merged(now - 60, now, list(self.cfg.prefixes) + list(eng.controls))
            if d:
                await asyncio.to_thread(eng.load_bgplay, d, True, False)
                self._restore_route_ages()
                self._reconcile_history(ripe_ts(d["query_starttime"]))
            gap_start = last + B if last is not None else None
        if gap_start is not None and now - gap_start > B:
            self._add_gap(gap_start, now)

    def _restore_route_ages(self):
        """Seeding stamps every route with the snapshot time. Where a peer's path is unchanged from what our
        own history last recorded, restore the real 'last changed' time, so stale ('zombie') routes that RIS
        still holds can be recognised and dropped by the looking-glass resync."""
        eng, restored = self.engine, 0
        for prefix in self.cfg.prefixes:
            last = {(r["collector"], r["peer"]): r for r in self.conn.execute(
                "SELECT collector, peer, path, ts FROM (SELECT collector, peer, path, ts, ROW_NUMBER() OVER "
                "(PARTITION BY collector, peer ORDER BY ts DESC, id DESC) rn FROM updates WHERE prefix=? AND "
                "source NOT IN ('bgplay-init', 'resync', 'stale') AND kind='A') WHERE rn=1", (prefix,))}
            for key, e in eng.rib.get(prefix, {}).items():
                r = last.get(key)
                if e.path and r and r["path"] == " ".join(map(str, e.path)) and r["ts"] < e.ts:
                    e.ts = r["ts"]
                    restored += 1
        log.info("restored real route ages for %d seeded routes", restored)

    def _reconcile_snapshot(self, d):
        """Align the live RIB and the stored history with a RIS (BGPlay) snapshot: any peer route we hold
        that RIS's snapshot doesn't is withdrawn (recorded as source 'stale'). Routes we heard about after
        the snapshot time are left alone."""
        eng, ts, n = self.engine, ripe_ts(d["query_starttime"]), 0
        snap = {(s["target_prefix"], paths.collector_name(s["source_id"].split("-", 1)[0]), s["source_id"].split("-", 1)[1])
                for s in d.get("initial_state", [])}
        for prefix in self.cfg.prefixes:
            for (coll, peer), e in list(eng.rib.get(prefix, {}).items()):
                if e.path and e.ts < ts and (prefix, coll, peer) not in snap:
                    if eng.apply(e.ts + 1, coll, peer, e.peer_asn, prefix, "W", None, source="stale"):
                        n += 1
                        log.info("RIB: %s via %s/%s not in RIS snapshot (last update %s) - withdrawn", prefix, coll,
                                 peer, time.strftime("%Y-%m-%d", time.gmtime(e.ts)))
        self.conn.commit()
        self._reconcile_history(ts)
        if n:
            log.info("snapshot reconcile: %d stale routes withdrawn from the live RIB", n)

    async def _snapshot_reconcile_loop(self):
        """Once a day, re-align with RIS's snapshot so silently dropped routes don't linger for weeks."""
        while True:
            await asyncio.sleep(24 * 3600)
            try:
                d = await self._bgplay_merged(time.time() - 60, time.time())
                if d:
                    self._reconcile_snapshot(d)
            except Exception:
                log.exception("daily snapshot reconcile failed")

    def _reconcile_history(self, ts):
        """Record a withdrawal for every peer our stored history still thinks has a route at `ts` but RIS's
        own snapshot (just seeded) doesn't. Peers sometimes drop routes without RIS logging a withdrawal
        (session resets); without this, history-based views (trees, replays) would keep showing those
        long-gone paths."""
        eng, n = self.engine, 0
        for prefix in self.cfg.prefixes:
            live = {k for k, e in eng.rib.get(prefix, {}).items() if e.path}
            for r in db.state_at(self.conn, prefix, ts):
                key = (r["collector"], r["peer"])
                if r["kind"] == "A" and r["path"] and key not in live:
                    self.conn.execute("INSERT INTO updates(ts,source,collector,peer,peer_asn,prefix,kind,path,"
                                      "communities) VALUES(?,?,?,?,?,?,?,?,?)",
                                      # dated just after the last real update: nothing shows it existed later
                                      (r["ts"] + 1, "stale", r["collector"], r["peer"], r["peer_asn"], prefix, "W",
                                       None, None))
                    n += 1
                    log.info("history: %s via %s/%s not in RIS snapshot (last seen %s: %s) - recorded withdrawal",
                             prefix, r["collector"], r["peer"], time.strftime("%Y-%m-%d", time.gmtime(r["ts"])),
                             r["path"])
        self.conn.commit()
        if n:
            log.info("history reconciled: %d stale routes withdrawn", n)

    async def _ensure_init_rows(self):
        """One-time migration: store the RIB snapshot at the start of recorded history so event replays
        can reconstruct peers whose path never changed (older versions didn't store it)."""
        if db.kv_get(self.conn, "init_rows_v2"):
            return
        first = self.conn.execute("SELECT MIN(ts) t FROM buckets").fetchone()["t"]
        have = first is not None and self.conn.execute(
            "SELECT 1 FROM updates WHERE source='bgplay-init' AND ts <= ? LIMIT 1", (first + 60,)).fetchone()
        if first is not None and not have:
            d = await self._bgplay_merged(first, first + 60)
            if not d:
                return  # try again next start
            start = ripe_ts(d["query_starttime"])
            asn_of = {s["id"]: s.get("as_number") for s in d.get("sources", [])}
            rows = []
            for s in d.get("initial_state", []):
                coll, peer = s["source_id"].split("-", 1)
                rows.append((start, "bgplay-init", paths.collector_name(coll), peer, asn_of.get(s["source_id"]),
                             s["target_prefix"], "A", " ".join(map(str, paths.normalize(s["path"]))), None))
            self.conn.executemany("INSERT INTO updates(ts,source,collector,peer,peer_asn,prefix,kind,path,"
                                  "communities) VALUES(?,?,?,?,?,?,?,?,?)", rows)
            log.info("stored %d initial RIB rows at %s", len(rows), time.strftime("%Y-%m-%d %H:%M", time.gmtime(start)))
        db.kv_set(self.conn, "init_rows_v2", True)
        self.conn.commit()

    async def _compact_buckets(self):
        """One-time migration: buckets stored before 1.3.2 keep their chains / upstreams JSON in every row, about 6 KB
        each for a prefix seen through hundreds of paths. Drop it wherever it's the same as the bucket before, as
        Engine.tick now does, a thousand buckets at a time in the background (the pages show the same at every step),
        then give the space back to the disk once. Carries on where it was after a restart. It can't run in
        db.init: on a big database it takes minutes, and the updater gives a new version a minute to answer."""
        at = db.kv_get(self.aconn, db.COMPACT_KV)
        if at is True:
            return
        await asyncio.sleep(120)   # after the startup work
        try:
            if at != "vacuum":
                at, n, t0 = tuple(at or ("", 0)), 0, time.time()
                log.info("compacting stored buckets: their chains / upstreams are now kept only where they change")
                while at:
                    while self.replays:   # a history load or gap fill is rewriting buckets: wait for it
                        await asyncio.sleep(10)
                    at, k = await asyncio.to_thread(db.compact_mix, self.aconn, self.cfg.bucket_seconds, at)
                    n += k
                    db.kv_set(self.aconn, db.COMPACT_KV, list(at) if at else "vacuum")
                    self.aconn.commit()
                    await asyncio.sleep(0.2)   # (live monitoring shares the connection)
                log.info("compacted %d stored buckets in %.0f s", n, time.time() - t0)
            if await self._vacuum():
                db.kv_set(self.aconn, db.COMPACT_KV, True)
                self.aconn.commit()
        except Exception:
            log.exception("bucket compaction failed (it carries on at the next start)")

    async def _neighbour_history(self):
        """Shortly after startup: each upstream without a neighbour history yet gets its past month looked up
        (tree.backfill_neighbors), then today's list, so a neighbour it lost recently shows on the tree as "no longer
        peered" before anyone opens it."""
        await self.first_sync.wait()
        await asyncio.sleep(60)
        try:
            ups = set(self.cfg.expected_upstreams)
            for table in (self.engine.rib.get(p, {}) for p in self.cfg.prefixes):
                for e in list(table.values()):
                    if e.path and len(e.path) >= 2 and e.path[-1] == self.cfg.asn:
                        ups.add(e.path[-2])
            ups = {u for u in ups if not self.engine.peer_group(u)}   # (peers have no tree branch of their own)
            for u in sorted(ups):
                await tree.backfill_neighbors(self.sourceapp, u)
            await tree.upstream_neighbors(self.sourceapp, sorted(ups))
        except Exception:
            log.exception("upstream neighbour history failed")

    async def _vacuum(self):
        """Give the space the compaction freed inside the database file back to the disk: VACUUM rewrites the file,
        which pauses bgpcanary for a few seconds. Skipped when it would free less than a quarter of the file (the
        space is reused as new data comes in). Returns False when it's worth doing but there isn't room for the
        copies it makes (it's tried again at the next start)."""
        c, here = self.aconn, os.path.dirname(os.path.abspath(self.cfg.db_path))
        page = c.execute("PRAGMA page_size").fetchone()[0]
        size = c.execute("PRAGMA page_count").fetchone()[0] * page
        free = c.execute("PRAGMA freelist_count").fetchone()[0] * page
        if free < size / 4 or free < 50e6:
            return True
        # VACUUM writes the data (size - free) to a temporary file in SQLite's temp directory, then through the WAL
        # next to the database
        tmp = next((d for d in (os.environ.get("SQLITE_TMPDIR"), os.environ.get("TMPDIR"), "/var/tmp", "/usr/tmp", "/tmp")
                    if d and os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK)), here)
        need = {}   # filesystem -> (a directory on it, bytes needed there)
        for d in (here, tmp):
            d0, n = need.get(os.stat(d).st_dev, (d, 100e6))
            need[os.stat(d).st_dev] = (d0, n + size - free)
        short = [(d, n) for d, n in need.values() if shutil.disk_usage(d).free < n]
        if short:
            log.warning("the database file has %.0f MB of free space inside it, but there isn't room to compact it "
                        "now: that needs %s. Make room there, or set SQLITE_TMPDIR to a directory on a disk that has "
                        "it; it's tried again at the next start.", free / 1e6,
                        ", ".join(f"{n / 1e6:.0f} MB free in {d}" for d, n in short))
            return False
        log.info("compacting the database file (%.0f MB, %.0f MB of it free space); bgpcanary pauses meanwhile",
                 size / 1e6, free / 1e6)
        t0 = time.time()
        await asyncio.to_thread(db.vacuum, c)
        log.info("database file compacted to %.0f MB in %.0f s", os.path.getsize(self.cfg.db_path) / 1e6,
                 time.time() - t0)
        return True

    def _add_gap(self, s, e):
        gaps = db.kv_get(self.aconn, "gaps", [])
        gaps.append([s, e])
        gaps.sort()
        merged = []
        for g in gaps:
            if merged and g[0] <= merged[-1][1] + 60:
                merged[-1][1] = max(merged[-1][1], g[1])
            else:
                merged.append(g)
        db.kv_set(self.aconn, "gaps", merged)
        self.aconn.commit()
        log.info("recorded data gap %s -> %s UTC", time.strftime("%m-%d %H:%M:%S", time.gmtime(s)),
                 time.strftime("%m-%d %H:%M:%S", time.gmtime(e)))

    async def _transit_loop(self):
        await self.first_sync.wait()
        await asyncio.sleep(180)   # let the RIB settle after startup
        while True:
            try:
                await self.transit.run()
            except Exception:
                log.exception("transit filter check failed")
            await asyncio.sleep(max(self.cfg.transit_filter_minutes, 2) * 60)

    async def _registry_loop(self):
        await self.first_sync.wait()
        await asyncio.sleep(60)
        while True:
            try:
                await self.registry.run()
            except Exception:
                log.exception("registry watch failed")
            await asyncio.sleep(max(self.cfg.registry_minutes, 10) * 60)

    async def _gap_loop(self):
        await asyncio.sleep(120)
        while True:
            try:
                await self._fill_gaps()
            except Exception:
                log.exception("gap fill failed")
            await asyncio.sleep(1800)

    async def _fill_gaps(self):
        """Rebuild buckets/events for recorded gaps once RIPE's BGPlay data covers them (lags a few hours)."""
        gaps = db.kv_get(self.aconn, "gaps", [])
        if not gaps:
            return
        B = self.cfg.bucket_seconds
        remaining = []
        for s, e in gaps:
            probe = await self._bgplay_merged(e - 60, e)
            # BGPlay reports whole seconds; gap ends carry fractions, so compare at second resolution
            # (comparing raw values kept every gap "not covered yet" forever)
            if not probe or ripe_ts(probe["query_endtime"]) < int(e):
                remaining.append([s, e])
                continue
            # The filler writes through the service's single writer connection: a second connection on
            # this thread can deadlock against the live engine's open transaction ("database is locked").
            f = Engine(self.cfg, self.aconn, restore=False, peering=self.peering)
            f.historical = True
            f.seed_baseline(s)
            f.last_bucket = int(-(-s // B) * B)
            await self._replay(f, s, e, label="gap fill")
            close_open(f, e)
            log.info("gap %s -> %s filled from BGPlay", time.strftime("%m-%d %H:%M:%S", time.gmtime(s)),
                     time.strftime("%H:%M:%S", time.gmtime(e)))
        db.kv_set(self.aconn, "gaps", remaining)
        self.aconn.commit()

    # ------------------------------------------------------------------ live loops
    async def _resync_all(self):
        eng = self.engine
        total = 0
        trusted = self.health.lg_trusted()   # (decided before this round: its own answers count from the next one)
        for p in list(self.cfg.prefixes) + list(eng.controls):
            try:
                lg = await ripe.looking_glass(p, self.sourceapp)
                lg["resource"] = p
                empty = not any(e.path for e in eng.rib.get(p, {}).values())
                eng.quiet = empty
                total += eng.resync_lg(lg, allow_withdraw=trusted)
            except Exception as ex:
                log.warning("looking-glass resync failed for %s: %s", p, ex)
            finally:
                eng.quiet = False
        eng.status["last_resync"] = time.time()
        eng.status["resync_changes"] = total
        log.info("looking-glass resync: %d changes", total)

    async def _tick_loop(self):
        await self.first_sync.wait()
        while True:
            try:
                self.engine.tick(time.time() - 3)
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(2)

    async def _reconcile_loop(self):
        while True:
            await asyncio.sleep(self.cfg.reconcile_minutes * 60)
            quiet = all(time.time() - m.last_update > 120 for m in self.engine.mons.values())
            if quiet:
                await self._resync_all()

    # ------------------------------------------------------------------ events
    def _on_event(self, eid, phase):
        asyncio.get_running_loop().create_task(self._handle_event(eid, phase))

    async def _handle_event(self, eid, phase):
        ev = dict(self.aconn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        checked = False
        if phase == "open" and ev["kind"] == "outage" and self.cfg.confirm_outages and not ev.get("historical"):
            # checked against Route Views etc. before it's alerted (up to ~2 min); its clearing waits for this alert
            self.opening[eid] = asyncio.Event()
            try:
                await asyncio.wait_for(self._confirm_outage(ev), 150)
                checked = True
            except Exception as ex:
                log.warning("outage %s: checking it against Route Views failed: %s", eid, ex or type(ex).__name__)
                m = self.engine.mons.get(ev["prefix"])
                if m and m.event_id == eid and m.confirm == "pending":
                    m.confirm = None
            ev = dict(self.aconn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        elif phase in ("close", "escalate") and eid in self.opening:
            try:
                await asyncio.wait_for(self.opening[eid].wait(), 200)
            except asyncio.TimeoutError:
                pass
            ev = dict(self.aconn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        if phase == "open" and ev["kind"] in ABSENCE_KINDS and not ev.get("historical"):
            self._mark_doubtful(ev)
        try:
            # per-type choices decide. An escalation (warning -> critical) is the first alert for "critical only"
            # subscribers, and news for everyone who was told about the warning.
            notify = phase in ("open", "escalate", "close") and config.should_notify(self.cfg, ev, phase)
            if notify:
                images = []
                if self.cfg.email.enabled and self.cfg.email.recipients:
                    try:   # before/now connectivity-tree snapshots for the email
                        images = await tree.event_images(self.cfg, self.sourceapp, ev, phase, peering=self.peering)
                    except Exception:
                        log.exception("tree images for event %s failed; sending without them", eid)
                await alerts.send(self.cfg, self.aconn, ev, phase, images=images)
                self.aconn.execute("UPDATE events SET alerted=1 WHERE id=?", (eid,))
                self.aconn.commit()
        finally:
            if phase == "open" and eid in self.opening:
                self.opening.pop(eid).set()
        if atlas.enabled(self.aconn) and (ev["kind"] == "outage" and phase in ("open", "close")
                                          or ev["kind"] == "upstream_shift" and phase == "open"):
            asyncio.get_running_loop().create_task(self._atlas_burst(eid, phase, ev["kind"]))
        if self.cfg.probe_on_event and phase in ("open", "close") and ev["kind"] in BURST_KINDS:
            asyncio.get_running_loop().create_task(self._event_burst(eid, ev["kind"], phase, ev.get("prefix"),
                                                                     snapshot=not checked))

    async def _confirm_outage(self, ev):
        """Check an outage RIS shows against Route Views (every collector at once, just this prefix), the control
        prefixes and RIPE Atlas's latest results (sanity.judge), and record the verdict on the event before it's
        alerted. If they still see the prefix, it's a warning, "probably a problem with RIPE's data", that doesn't
        turn critical (Engine._track)."""
        eng, p, eid = self.engine, ev["prefix"], ev["id"]
        m = eng.mons.get(p)
        if m and m.event_id == eid:
            m.confirm = "pending"
        trouble = eng.ris_trouble()
        if self.cfg.rv_servers:
            self.last_burst = time.time()   # (the event's Route Views burst doesn't ask them again)
            await asyncio.gather(*(self.snapshot_server(srv, [p], "event", eid) for srv in self.cfg.rv_servers),
                                 return_exceptions=True)
        rv = sanity.route_views_view(self.aconn, p, self.cfg.asn, eid, ev["start_ts"])
        at = (sanity.atlas_view(self.aconn, sanity.targets_in(p, self.atlas.targets()), ev["start_ts"])
              if atlas.enabled(self.aconn) else None)
        verdict, why = sanity.judge(trouble, rv, at)
        row = self.aconn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        detail = json.loads(row["detail"] or "{}")
        detail["confirm"] = {"verdict": verdict, "why": why, "route_views": rv, "atlas": at,
                             "controls": eng.control_status(), "health": self.health.state()["reasons"],
                             "at": time.time()}
        sev, title = row["severity"], row["title"]
        if verdict == "contradicted":
            mv, base = row["min_visible"], row["baseline"] or 0
            sev = "warning"
            title = (f"{p}: RIS lost sight of it ({mv}/{base:.0f} peers), but {why}: a problem with RIPE's data"
                     if row["end_ts"] else
                     f"{p} visibility in RIS dropped to {mv}/{base:.0f} peers, but {why}: probably a problem with "
                     f"RIPE's data")
            detail["note"] = (f"RIS shows this prefix's visibility dropping, but {why}. That points to a problem with "
                              f"RIPE's data rather than your network, so this is a warning, not critical, and it won't "
                              f"turn critical. It closes by itself when RIS sees the prefix again.")
        elif verdict == "confirmed":
            detail["note"] = f"Confirmed: {why}."
        else:
            detail["note"] = f"Couldn't be checked independently: {why}."
        self.aconn.execute("UPDATE events SET severity=?, title=?, detail=? WHERE id=?",
                           (sev, title, json.dumps(detail), eid))
        self.aconn.commit()
        if m and m.event_id == eid:
            m.confirm, m.confirm_why = verdict, why
            if verdict == "contradicted":
                m.event_sev = "warning"
        log.info("outage %s on %s checked: %s (%s)", eid, p, verdict, why)

    def _mark_doubtful(self, ev):
        """A route-gone-missing event while RIPE's data looks unreliable (sanity.SourceHealth): say so on it and in
        its alert."""
        h = self.health.state()
        if h["ok"]:
            return
        detail = json.loads(ev["detail"] or "{}")
        detail["health"] = h["reasons"]
        detail["note"] = ((detail.get("note") + " ") if detail.get("note") else "") + (
            "RIPE's data looked unreliable when this opened (" + "; ".join(h["reasons"]) + "), so it may not be real.")
        ev["title"] = ev["title"] + " (while RIPE's data looked unreliable)"
        ev["detail"] = json.dumps(detail)
        self.aconn.execute("UPDATE events SET title=?, detail=? WHERE id=?", (ev["title"], ev["detail"], ev["id"]))
        self.aconn.commit()

    async def _atlas_burst(self, eid, phase, kind="outage"):
        """One-off RIPE Atlas measurements: traceroutes when an outage opens / clears; pings + traceroutes when an
        upstream shift opens (to find out whether your space stayed reachable through it)."""
        try:
            if kind == "upstream_shift":
                await self.atlas.shift_check(eid)
            else:
                await self.atlas.outage_trace(eid, phase)
        except Exception as ex:
            log.warning("event %s: Atlas traceroute burst not started: %s", eid, ex)

    def _route_signature(self, prefix):
        """Cheap local summary of how a prefix is reached right now (from the live RIS RIB, no network)."""
        eng = self.engine
        p = eng._parent(prefix) if prefix else None
        if not p or p not in eng.mons:
            return None
        chains = {}
        for e in list(eng.rib.get(p, {}).values()):
            if e.path and e.path[-1] == self.cfg.asn:
                c = paths.chain(e.path, 2)   # transit -> upstream
                chains[c] = chains.get(c, 0) + 1
        return {"prefix": p, "visible": eng.visible(p), "base": eng.mons[p].current_baseline() or 0, "chains": chains}

    def _major_change(self, a, b):
        """Reason string if routing changed enough since the last Route Views query, else None. 'Enough' =
        total visibility, or any single transit->upstream path, moving by >= the change threshold (so a
        handful of peers flickering on a minor path doesn't count)."""
        if not a or not b:
            return None
        step = max(5, b["base"] * self.cfg.rv_followup_change_pct / 100)
        if abs(b["visible"] - a["visible"]) >= step:
            return f"visibility {a['visible']} -> {b['visible']} RIS peers"
        moved = []
        for c in sorted(set(a["chains"]) | set(b["chains"])):
            n0, n1 = a["chains"].get(c, 0), b["chains"].get(c, 0)
            if abs(n1 - n0) >= step:
                moved.append(f"{c.replace(' ', ' -> ')} {n0} -> {n1} peers")
        return ("upstream paths shifted: " + ", ".join(moved)) if moved else None

    async def _event_burst(self, eid, kind, phase, prefix=None, snapshot=True):
        """Fresh Route Views data when something breaks: immediately on open/close, then again only when the
        routing changes significantly while an outage lasts. Routine polling is kept very slow. `snapshot`: False
        when the outage check just took the snapshots (only the ping and traceroute are left to run)."""
        now = time.time()
        if snapshot and now - self.last_burst < 300:
            # another event (or a change-triggered re-query at the moment of recovery) just queried
            log.info("event %s %s: Route Views burst skipped (last query %.0f s ago)", eid, phase,
                     now - self.last_burst)
        else:
            if snapshot:
                self.last_burst = now
            await self.event_probes(eid, snapshot=snapshot)
        if phase != "open" or kind not in ("outage", "upstream_missing") or self.cfg.rv_followup_change_pct <= 0:
            return
        # Watch the live RIS data (free) once a minute; re-query Route Views only on a major change.
        last_sig, rounds = self._route_signature(prefix), 0
        while rounds < 12:
            await asyncio.sleep(60)
            row = self.aconn.execute("SELECT end_ts FROM events WHERE id=?", (eid,)).fetchone()
            if not row or row["end_ts"]:
                return   # recovered: the close burst takes the final snapshot
            why = self._major_change(last_sig, self._route_signature(prefix))
            if not why or time.time() - self.last_burst < 300:
                continue   # nothing major, or Route Views was queried < 5 min ago (try again next minute)
            self.last_burst = time.time()
            rounds += 1
            log.info("event %s: %s - re-querying Route Views (%d/12)", eid, why, rounds)
            await self.event_probes(eid, probes=(rounds % 2 == 1))
            last_sig = self._route_signature(prefix)

    async def event_probes(self, eid, probes=True, snapshot=True):
        if snapshot:
            await self.snapshot_all(trigger="event", event_id=eid)
        if probes:
            for t in self.cfg.probe_targets:
                await self.run_probe("ping", t, "event", eid)
                await self.run_probe("traceroute", t, "event", eid)

    # ------------------------------------------------------------------ route views
    @property
    def probe_server(self):
        return self.cfg.rv_servers[0] if self.cfg.rv_servers else None

    RV_LIMITS = {"conns": 100, "bytes_in": 500_000, "bytes_out": 100_000}

    def _rv_state(self, server):
        b = self.rv_budget.setdefault(server, {})
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if b.get("day") != today:
            b.update({"day": today, "conns": 0, "bytes_in": 0, "bytes_out": 0})
        return b

    def _rv_save(self):
        db.kv_set(self.aconn, "rv_budget", self.rv_budget)
        self.aconn.commit()

    def _rv_bytes(self, host, bytes_in, bytes_out):
        b = self._rv_state(host)
        b["bytes_in"] += bytes_in
        b["bytes_out"] += bytes_out
        self._rv_save()

    def rv_allowed(self, server, trigger):
        """(ok, reason). Scheduled work may use 50% of the daily limits and Path finder lookups 80%; the rest is
        kept for outage-triggered and manual snapshots/probes (up to 95%)."""
        b = self._rv_state(server)
        if b.get("blocked_until", 0) > time.time():
            return False, "rate-limited by Route Views until " + time.strftime("%Y-%m-%d %H:%M %Z",
                                                                              time.localtime(b["blocked_until"]))
        frac = 0.5 if trigger == "scheduled" else 0.8 if trigger == "pathfinder" else 0.95
        for k, lim in self.RV_LIMITS.items():
            if b.get(k, 0) >= lim * frac:
                return False, f"daily Route Views budget for this collector used ({k} {b.get(k, 0)}/{lim})"
        return True, None

    def _rv_begin(self, server):
        self._rv_state(server)["conns"] += 1
        self._rv_save()

    def _rv_failed(self, server, ex):
        if isinstance(ex, routeviews.RateLimited):
            b = self._rv_state(server)
            b["blocked_until"] = time.time() + 24 * 3600   # stop for a day so retries don't keep the block alive
            b["last_error"] = str(ex)
            self._rv_save()
            log.warning("%s - pausing this collector for 24 h", ex)

    async def snapshot_server(self, server, prefixes, trigger="scheduled", event_id=None):
        ok, why = self.rv_allowed(server, trigger)
        if not ok:
            log.info("route views %s skipped: %s", server, why)
            return
        ts = time.time()
        self._rv_begin(server)
        try:
            res = await routeviews.show_bgp_many(server, prefixes)
            err = None
        except Exception as ex:
            res, err = {}, str(ex) or type(ex).__name__   # timeouts have no message; an empty error read as "no paths"
            self._rv_failed(server, ex)
            log.warning("route views %s: %s", server, err)
        for p in prefixes:
            raw, plist = res.get(p, ("", []))
            self.aconn.execute("INSERT INTO rv_snapshots(ts,server,prefix,n_paths,paths,raw,event_id,error)"
                               " VALUES(?,?,?,?,?,?,?,?)",
                               (ts, server, p, len(plist), json.dumps(plist), raw, event_id, err))
        self.aconn.commit()

    async def snapshot(self, server, prefix, event_id=None, trigger="manual"):
        await self.snapshot_server(server, [prefix], trigger, event_id)

    async def snapshot_all(self, trigger="scheduled", event_id=None):
        for srv in self.cfg.rv_servers:
            await self.snapshot_server(srv, list(self.cfg.prefixes), trigger, event_id)

    async def run_probe(self, kind, target, trigger="manual", event_id=None, server=None):
        server = server or self.probe_server
        try:
            target = config.plain_ip(target)   # e.g. an older saved setting: refuse it here, not at the collector
        except ValueError:
            log.warning("%s skipped: %r isn't a plain IP address", kind, str(target)[:80])
            return None
        ok, why = self.rv_allowed(server, trigger)
        if not ok:
            log.info("%s %s via %s skipped: %s", kind, target, server, why)
            if trigger == "scheduled":
                return None
        ts = time.time()
        succ = rtt = last_hop = last_asn = reached = None
        err = None if ok else why
        out = ""
        if ok:
            self._rv_begin(server)
        try:
            if not ok:
                raise RuntimeError(why)
            if kind == "ping":
                out, succ, rtt = await routeviews.ping(server, target)
                reached = 1 if succ else 0
            else:
                out, last_hop, last_asn, r = await routeviews.traceroute(server, target)
                reached = 1 if r else 0
        except Exception as ex:
            err = str(ex) or type(ex).__name__
            if ok:
                self._rv_failed(server, ex)
                log.warning("%s %s via %s failed: %s", kind, target, server, ex)
        cur = self.aconn.execute(
            "INSERT INTO probes(ts,server,kind,target,trigger,event_id,success_pct,rtt_avg,last_hop,last_hop_asn,"
            "reached,output,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, server, kind, target, trigger, event_id, succ, rtt, last_hop, last_asn, reached, out, err))
        self.aconn.commit()
        return cur.lastrowid

    def _resume_delay(self, sql, minutes, minimum):
        """Seconds until the next scheduled run, continuing the schedule from the last run in the database
        so service restarts don't cause extra Route Views connections."""
        last = self.aconn.execute(sql).fetchone()[0]
        if not last or minutes <= 0:
            return minimum
        return max(minimum, last + minutes * 60 - time.time())

    async def _rv_loop(self):
        await asyncio.sleep(self._resume_delay("SELECT MAX(ts) FROM rv_snapshots WHERE event_id IS NULL",
                                               self.cfg.rv_poll_minutes, 20))
        while True:
            try:
                if self.cfg.rv_poll_minutes > 0:
                    await self.snapshot_all()
            except Exception:
                log.exception("route views poll failed")
            await asyncio.sleep(max(self.cfg.rv_poll_minutes, 1) * 60)

    async def _probe_loop(self):
        await asyncio.sleep(self._resume_delay("SELECT MAX(ts) FROM probes WHERE trigger='scheduled'",
                                               self.cfg.probe_minutes, 60))
        while True:
            try:
                if self.cfg.probe_minutes > 0:
                    for t in self.cfg.probe_targets:
                        await self.run_probe("ping", t, "scheduled")
                        await self.run_probe("traceroute", t, "scheduled")
            except Exception:
                log.exception("scheduled probe failed")
            await asyncio.sleep(max(self.cfg.probe_minutes, 1) * 60)

    # ------------------------------------------------------------------ housekeeping
    async def as_names(self, asns, limit=40):
        """{asn: name} from the name cache, with up to `limit` of the missing ones looked up in RIPEstat now (and
        kept)."""
        want = list(dict.fromkeys(int(a) for a in asns if a is not None and str(a).isdigit()))
        got = {}
        if want:
            q = ",".join("?" * len(want))
            got = {r["asn"]: r["name"] for r in self.aconn.execute(f"SELECT asn,name FROM asn_names WHERE asn IN ({q})",
                                                                   want)}
        todo = [a for a in want if a not in got][:limit]

        async def fetch(a):
            try:
                nm = await ripe.as_name(a, self.sourceapp)
                self.aconn.execute("INSERT OR REPLACE INTO asn_names(asn,name,fetched) VALUES(?,?,?)", (a, nm, time.time()))
                got[a] = nm
            except Exception:
                pass
        if todo:
            await asyncio.gather(*(fetch(a) for a in todo))
            self.aconn.commit()
        return got

    async def _names_loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                known = {r["asn"] for r in self.aconn.execute("SELECT asn FROM asn_names")}
                want = {self.cfg.asn} | set(self.cfg.expected_upstreams)
                for p, table in list(self.engine.rib.items()):
                    if p in self.engine.controls:
                        continue
                    for e in list(table.values()):
                        if e.path:
                            want.update(e.path)
                for p in self.cfg.prefixes:   # each prefix's newest stored chains (through the index: no table scan)
                    for ch in json.loads(db.mix_at(self.aconn, p, time.time()) or "{}"):
                        want.update(int(x) for x in ch.split())
                # ASNs near the origin (upstreams/transit) first; they are the ones shown in charts
                near = set()
                for p, table in list(self.engine.rib.items()):
                    if p in self.engine.controls:
                        continue
                    for e in list(table.values()):
                        if e.path:
                            near.update(e.path[-4:])
                for asn in sorted(want - known, key=lambda a: (a not in near, a))[:60]:
                    try:
                        name = await ripe.as_name(asn, self.sourceapp)
                        self.aconn.execute("INSERT OR REPLACE INTO asn_names(asn,name,fetched) VALUES(?,?,?)",
                                           (asn, name, time.time()))
                        self.aconn.commit()
                    except Exception as ex:
                        log.debug("as name %s: %s", asn, ex)
                    await asyncio.sleep(0.5)
            except Exception:
                log.exception("names loop")
            await asyncio.sleep(300)

    async def _prune_loop(self):
        while True:
            await asyncio.sleep(86400)
            try:
                db.prune(self.aconn, self.cfg.raw_retention_days)
            except Exception:
                log.exception("prune")
