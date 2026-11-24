"""Route Views collectors that stop working for everyone, replaced by the one that sees your prefixes best.

Route Views retires a collector now and then, or one dies and stays dead: its telnet never answers again, and nothing
tells the networks that ask it. Replacer.check notices a collector that has failed every try for days
(cfg.rv_replace_days) while the others answer, checks with Route Views' archive (archive.routeviews.org, where each
collector's BGP data lands every 15 minutes) that it has stopped for everyone, not just for this server, then asks the
archive's other collectors once each and puts the one that sees the most paths to your prefixes in its place.

The swap is stored on its own (kv KV) and applied on top of the configured list (config.Config.rv_servers), so the
config file, or the list saved in Settings, stays in charge: the swap holds while the dead collector is on it, and an
admin can undo it. A collector Route Views still archives data from is never replaced: something between this server
and it is blocking the connection, and a swap would hide that, so it's reported instead (an open warning, closed once
it answers again). So is every collector failing at once, which means this server can't reach Route Views at all.
"""
import asyncio
import calendar
import json
import logging
import re
import time

import httpx

from . import __version__, db, routeviews
from .sanity import RV_MIN_USUAL

log = logging.getLogger("bgpcanary.collectors")

KV = "rv_replaced"                 # {"swaps": {dead: {...}}, "kept": {host: when undone}, "told": {host or "*": {...}}}
KIND = "rv_collector"              # the events (config.ALERT_TYPES)
ARCHIVE = "https://archive.routeviews.org"
MAIN = "route-views.routeviews.org"   # Route Views' Cisco collector: its archive is a table dump every two hours
CHECK_S = 6 * 3600                 # how often the collectors' failures are looked at
MIN_TRIES = 3                      # a collector is judged after this many failed tries in a row, over the days set...
RECENT_S = 2 * 86400               # ...the latest of them this recent
ANSWERED_S = 86400                 # another collector counts as answering if it did this recently
ARCHIVE_DEAD_S = 86400             # nothing in Route Views' archive from it for this long: it's gone for everyone
ARCHIVE_FRESH_S = 6 * 3600         # one that answers here has archive data this recent, or the archive is behind
RETRY_S = 86400                    # a search no collector answered, an archive that couldn't be read: again after this
ASK_AT_ONCE = 6                    # collectors asked at the same time when looking for a replacement
ASK_TIMEOUT = 150
UA = {"User-Agent": f"bgpcanary/{__version__} (open-source BGP monitor)"}
FILE_RE = re.compile(r"(?:updates\.(\d{4})(\d\d)(\d\d)\.(\d\d)(\d\d)|oix-full-snapshot-(\d{4})-(\d\d)-(\d\d)-(\d\d)(\d\d))"
                     r"\.bz2")
INDEX_RE = re.compile(r'href="/([a-z0-9][a-z0-9.-]*)/bgpdata/?"', re.I)


# ------------------------------------------------------------------------------------------- Route Views' archive
async def get(url):
    """(HTTP status, text) of one page of Route Views' archive."""
    async with httpx.AsyncClient(timeout=60, headers=UA, follow_redirects=True) as c:
        r = await c.get(url)
        return r.status_code, r.text


def archive_dir(host):
    """Where Route Views' archive keeps `host`'s data, or None when it isn't a Route Views collector's name."""
    host = str(host or "").lower().rstrip(".")
    for dom in (".routeviews.org", ".oregon-ix.net"):
        if host.endswith(dom) and len(host) > len(dom):
            name = host[:-len(dom)]
            return "oix-route-views" if name == "route-views" else name
    return None


def _months(now):
    """This month and the one before, as the archive names them ("2026.11")."""
    t = time.gmtime(now)
    y, m = (t.tm_year, t.tm_mon - 1) if t.tm_mon > 1 else (t.tm_year - 1, 12)
    return [f"{t.tm_year}.{t.tm_mon:02d}", f"{y}.{m:02d}"]


def _top(d):
    return f"{ARCHIVE}/{d}/" if d == "oix-route-views" else f"{ARCHIVE}/{d}/bgpdata/"


def _listing(d, month):
    return f"{_top(d)}{month}/" if d == "oix-route-views" else f"{_top(d)}{month}/UPDATES/"


def _file_ts(m):
    y, mo, d, hh, mm = (int(x) for x in m.groups() if x is not None)
    return calendar.timegm((y, mo, d, hh, mm, 0))


def _check(status):
    if status != 200:
        raise OSError(f"Route Views' archive answered HTTP {status}")


async def archive_newest(host, now):
    """When Route Views' archive last got data from `host`: its newest file this month or last (None: neither month
    has any). LookupError: the archive has no such collector. OSError, httpx errors: it couldn't be read."""
    d = archive_dir(host)
    if d is None:
        raise LookupError(host)
    for month in _months(now):
        status, text = await get(_listing(d, month))
        if status == 404:        # (a month with nothing yet, or no such collector: see below)
            continue
        _check(status)
        found = [_file_ts(m) for m in FILE_RE.finditer(text)]
        if found:
            return max(found)
    status, _text = await get(_top(d))
    if status == 404:
        raise LookupError(host)
    _check(status)
    return None


async def archive_collectors():
    """Route Views' collectors as its archive's front page lists them, as host names, the Cisco one first."""
    status, text = await get(ARCHIVE + "/")
    _check(status)
    found = [f"{d.lower()}.routeviews.org" for d in INDEX_RE.findall(text)]
    if not found:
        raise OSError("Route Views' archive lists no collectors")
    return list(dict.fromkeys([MAIN] + found))


# ------------------------------------------------------------------------------------------- what this server saw
def failing(conn, server):
    """`server`'s run of failed tries since it last answered: {"first", "last", "tries", "last_ok"}, or None when its
    latest try answered (or it was never tried). A try is a snapshot, one row per prefix at the same time. One that
    Route Views' rate limiter turned away isn't a failure (the collector is there), and an answer with no paths is an
    answer."""
    last_ok = conn.execute("SELECT MAX(ts) FROM rv_snapshots WHERE server=? AND (error IS NULL OR error='')",
                           (server,)).fetchone()[0]
    first, last, tries = conn.execute(
        "SELECT MIN(ts), MAX(ts), COUNT(DISTINCT ts) FROM rv_snapshots WHERE server=? AND ts>? AND error!='' AND "
        "error NOT LIKE '%rate-limited%'", (server, last_ok or 0)).fetchone()
    if not tries:
        return None
    return {"first": first, "last": last, "tries": tries, "last_ok": last_ok}


def answered_since(conn, server, since):
    return conn.execute("SELECT 1 FROM rv_snapshots WHERE server=? AND ts>=? AND (error IS NULL OR error='') LIMIT 1",
                        (server, since)).fetchone() is not None


def origin_paths(plist, asn):
    """How many of `plist` (path strings) end in your ASN."""
    return sum(1 for p in plist if p.split() and p.split()[-1] == str(asn))


def when(ts):
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(ts))


def _days(s):
    d = s / 86400
    return f"{d:.0f} day{'s' if round(d) != 1 else ''}" if d >= 1.5 else f"{s / 3600:.0f} hours"


def _ago(s):
    return f"{s / 60:.0f} minutes" if s < 5400 else f"{s / 3600:.0f} hours" if s < 2 * 86400 else _days(s)


# ------------------------------------------------------------------------------------------- the check
class Replacer:
    """Collectors that stopped working: found by check() (every CHECK_S), swapped in cfg.rv_swaps, kept in kv KV."""

    def __init__(self, svc):
        self.svc = svc
        self.running = False
        self.wait = {}   # host -> not looked at again before then (its archive couldn't be read, no collector answered)
        st = db.kv_get(svc.aconn, KV, {}) or {}
        self.st = {k: dict(st.get(k) or {}) for k in ("swaps", "kept", "told")}
        self._apply()

    # ---- state
    def _apply(self):
        self.svc.cfg.rv_swaps = {h: s.get("by") for h, s in self.st["swaps"].items()}

    def _save(self):
        db.kv_set(self.svc.aconn, KV, self.st)
        self.svc.aconn.commit()
        self._apply()

    def swaps(self):
        """The swaps in effect, oldest first: [{"dead", "by", "at", "paths", ...}]."""
        return sorted(({"dead": h, **s} for h, s in self.st["swaps"].items()), key=lambda s: s.get("at") or 0)

    def told(self):
        """The open warnings: [{"host" ("*": every collector), "what", "since", "event", ...}]."""
        return [{"host": h, **t} for h, t in self.st["told"].items()]

    def undo(self, host):
        """Put `host` back in the list. It isn't replaced again until it has answered once more."""
        sw = self.st["swaps"].pop(host, None)
        if sw is None:
            return None
        self.st["kept"][host] = time.time()
        self._save()
        log.info("Route Views collector %s is asked again (its replacement by %s undone)", host, sw.get("by") or "nothing")
        return sw

    def answered(self, server, ts):
        """A snapshot from `server` came back (Service.snapshot_server): what was said about it, or about every
        collector, is over, and a collector put back by hand may be judged again."""
        if server in self.st["kept"]:
            del self.st["kept"][server]
            self._save()
        for h in (server, "*"):
            if h in self.st["told"]:
                self._close(h, ts)

    def _close(self, h, ts):
        t = self.st["told"].pop(h)
        self._save()
        if t.get("event"):
            self.svc.engine._close_event(t["event"], ts)

    def _event(self, severity, title, detail, at, notice=False):
        """An event about the collectors: an open warning, or a notice (closed as it opens)."""
        return self.svc.engine._new_event(KIND, severity, None, at, title, detail, end_ts=at if notice else None)

    def _listed(self):
        """The collectors a swap can apply to: the configured ones, and what replaced them (in case that goes too)."""
        out, todo = set(), list(self.svc.cfg.rv_configured)
        while todo:
            h = todo.pop()
            if h and h not in out:
                out.add(h)
                todo.append((self.st["swaps"].get(h) or {}).get("by"))
        return out

    # ---- the check
    async def loop(self):
        await asyncio.sleep(600)
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("checking the Route Views collectors failed")
            await asyncio.sleep(CHECK_S)

    async def check(self, now=None):
        if self.running:
            return
        self.running = True
        try:
            await self._check(now or time.time())
        finally:
            self.running = False

    async def _check(self, now):
        cfg, conn = self.svc.cfg, self.svc.aconn
        listed = self._listed()
        for h in [h for h in self.st["swaps"] if h not in listed]:
            log.info("Route Views collector %s is no longer listed: its replacement by %s is forgotten", h,
                     self.st["swaps"][h].get("by") or "nothing")
            del self.st["swaps"][h]
            self._save()
        servers = cfg.rv_servers
        for h in [h for h in self.st["told"] if not cfg.rv_replace_dead or (h not in servers and h != "*")]:
            self._close(h, now)   # (switched off, or no longer asked)
        for h, at in list(self.st["kept"].items()):
            last_ok = conn.execute("SELECT MAX(ts) FROM rv_snapshots WHERE server=? AND (error IS NULL OR error='')",
                                   (h,)).fetchone()[0]
            if h not in servers or (last_ok or 0) > at:   # (no longer asked, or it has answered since it was put back)
                del self.st["kept"][h]
                self._save()
        if not cfg.rv_replace_dead or not servers:
            return
        runs = {s: failing(conn, s) for s in servers}
        days = cfg.rv_replace_days * 86400
        gone = [s for s, r in runs.items() if r and r["tries"] >= MIN_TRIES and now - r["first"] >= days
                and now - r["last"] <= RECENT_S and s not in self.st["kept"]]
        if len(gone) == len(servers):
            self._tell_all(servers, runs, now)
            return
        if "*" in self.st["told"]:
            self._close("*", now)
        if not gone:
            return
        others = [s for s in servers if s not in gone]
        answering = [s for s in others if answered_since(conn, s, now - ANSWERED_S)]
        if len(answering) * 2 < len(others):
            log.info("Route Views collectors %s have failed every try for days, but only %d of the other %d answered "
                     "in the last day: not judged until they do", ", ".join(gone), len(answering), len(others))
            return
        lag = None
        for s in gone:
            r, t = runs[s], self.st["told"].get(s)
            if self.wait.get(s, 0) > now or (t and t["since"] == r["first"] and now - t["checked"] < RETRY_S):
                continue   # (told, and looked at again once a day: it may stop for everyone later)
            if lag is None:
                lag = await self._archive_lags(answering, now)
            if lag:
                log.info("not judging Route Views collector %s: %s", s, lag)
                return
            await self._judge(s, r, now, servers)

    async def _archive_lags(self, answering, now):
        """Why Route Views' archive can't be relied on right now ("" if it can): it has nothing recent from a collector
        that answers this server, or it can't be read. (Can't tell without such a collector it keeps: relied on.)"""
        for s in answering:
            try:
                newest = await archive_newest(s, now)
            except LookupError:
                continue
            except Exception as ex:
                return f"Route Views' archive couldn't be read ({str(ex) or type(ex).__name__})"
            if newest is None or now - newest > ARCHIVE_FRESH_S:
                return f"Route Views' archive has nothing recent from {s} either, which answers here: it's behind"
            return ""
        return ""

    async def _judge(self, s, run, now, servers):
        try:
            newest = await archive_newest(s, now)
        except LookupError:
            self._tell(s, run, now, "unknown")
            return
        except Exception as ex:
            log.warning("Route Views' archive couldn't be read for %s: %s", s, str(ex) or type(ex).__name__)
            self.wait[s] = now + RETRY_S
            return
        if newest is not None and now - newest <= ARCHIVE_DEAD_S:
            self._tell(s, run, now, "unreachable", newest)
            return
        await self._replace(s, run, now, servers, newest)

    def _silence(self, s, run, now):
        tried = f"{run['tries']} tries over {_days(now - run['first'])}"
        return (f"{s} hasn't answered this server since {when(run['last_ok'])} ({tried})" if run["last_ok"] else
                f"{s} has never answered this server ({tried})")

    def _tell(self, s, run, now, what, newest=None):
        """A collector that fails here but can't be replaced: say so once, in an open warning."""
        t = self.st["told"].get(s)
        if t and t["since"] == run["first"] and t["what"] == what:
            t["checked"] = now
            self._save()
            return
        if t:
            self._close(s, now)
        how_long = _days(now - (run["last_ok"] or run["first"]))
        if what == "unreachable":
            title = f"Route Views collector {s} hasn't answered this server for {how_long}, but it's working"
            note = (f"{self._silence(s, run, now)}, but Route Views' archive has data from it from {_ago(now - newest)} "
                    f"ago, so the collector is running: something between this server and it blocks the connection "
                    f"(telnet, TCP port 23), or Route Views stopped offering telnet on it. It wasn't replaced. Try "
                    f"`telnet {s}` from this server; if it doesn't answer anyone any more, remove it under Settings › "
                    f"Detection & data sources. This closes when it answers again.")
        else:
            title = f"Route Views collector {s} hasn't answered this server for {how_long}"
            note = (f"{self._silence(s, run, now)}. It isn't one of the collectors Route Views' archive lists, so "
                    f"bgpcanary can't tell whether it's down for everyone, and didn't replace it. Check the name, or "
                    f"remove it under Settings › Detection & data sources. This closes when it answers again.")
        eid = self._event("warning", title, {"collector": s, "failing_since": run["first"], "silent_since":
                                             run["last_ok"], "tries": run["tries"], "archive_last": newest,
                                             "verdict": what, "note": note}, now)
        self.st["told"][s] = {"what": what, "since": run["first"], "event": eid, "at": now, "checked": now}
        self._save()
        log.warning("Route Views collector %s: %s", s, title)

    def _tell_all(self, servers, runs, now):
        if "*" in self.st["told"]:
            return
        since = max(r["last_ok"] or r["first"] for r in runs.values())
        n = len(servers)
        title = (f"None of the {n} Route Views collectors has answered this server for {_days(now - since)}" if n > 1
                 else f"Route Views collector {servers[0]} hasn't answered this server for {_days(now - since)}")
        note = ("When every collector fails at once, the trouble is on this side: a firewall or proxy blocking outgoing "
                "telnet (TCP port 23), or routing. Nothing was replaced. Meanwhile outages can't be checked against "
                "Route Views (RIPE Atlas still counts, if it's set up). This closes when one answers again.")
        eid = self._event("warning", title, {"collectors": servers, "silent_since": since, "verdict": "all",
                                             "note": note}, now)
        self.st["told"]["*"] = {"what": "all", "since": since, "event": eid, "at": now, "checked": now}
        self._save()
        log.warning("%s", title)

    async def _replace(self, s, run, now, servers, newest):
        cfg = self.svc.cfg
        try:
            hosts = await archive_collectors()
        except Exception as ex:
            log.warning("Route Views' list of collectors couldn't be read: %s", str(ex) or type(ex).__name__)
            self.wait[s] = now + RETRY_S
            return
        skip = set(servers) | set(self.st["swaps"]) | {s}
        cands = [h for h in hosts if h not in skip]
        res = await self._ask_all(cands)
        answered = [x for x in res if x[1] is not None]
        if not answered:
            log.warning("Route Views collector %s stopped working for everyone, but none of the %d others asked "
                        "answered: trying again tomorrow", s, len(cands))
            self.wait[s] = now + RETRY_S
            return
        good = sorted((x for x in answered if x[1] >= RV_MIN_USUAL), key=lambda x: -x[1])
        pick, pings = (good[0] if good else None), None
        if good and servers[0] == s and cfg.probe_targets:   # (the first one also runs ping and traceroute)
            pings = False
            for x in good[:3]:
                if await self._pings(x[0], cfg.probe_targets[0]):
                    pick, pings = x, True
                    break
        gone = (f"{self._silence(s, run, now)}, and Route Views' archive has had nothing from it "
                + (f"since {when(newest)}" if newest else "this month or last") + ", so it's down for everyone.")
        ranked = sorted(answered, key=lambda x: -x[1])
        if pick:
            also = [f"{h} {n}" for h, n, _ in ranked if h != pick[0]][:3]
            title = f"Route Views collector {s} stopped working; {pick[0]} replaces it"
            note = (f"{gone} Of the {len(cands)} other Route Views collectors asked, {len(answered)} answered, and "
                    f"{pick[0]} sees the most paths to your prefixes: {pick[1]}"
                    + (f" (then {', '.join(also)})" if also else "") + ". It's asked in its place from now on.")
            if pings is False:
                note += (" It doesn't answer ping, so the ping and traceroute probes (Reachability) won't work until "
                         "a collector that allows them is first in the list.")
        else:
            best = ranked[0]
            title = f"Route Views collector {s} stopped working; dropped, as no other collector sees enough of your paths"
            note = (f"{gone} Of the {len(cands)} other Route Views collectors asked, {len(answered)} answered, and none "
                    f"sees at least {RV_MIN_USUAL} paths to your prefixes (the most: {best[0]}, {best[1]}), too few to "
                    f"check an outage against, so it was dropped instead of replaced.")
        note += (f" Your own list isn't changed: the swap holds while {s} is on it. Undo it under Settings › "
                 f"Detection & data sources.")
        eid = self._event("info", title, {
            "collector": s, "replacement": pick[0] if pick else None, "paths": pick[1] if pick else None,
            "failing_since": run["first"], "silent_since": run["last_ok"], "tries": run["tries"],
            "archive_last": newest, "pings": pings, "asked": [[h, n] for h, n, _ in sorted(
                res, key=lambda x: (x[1] is None, -(x[1] or 0), x[0]))], "note": note}, now, notice=True)
        if pick:   # (its answer is its first snapshot: the pages show its view at once)
            conn = self.svc.aconn
            for p, (raw, plist) in pick[2].items():
                conn.execute("INSERT INTO rv_snapshots(ts,server,prefix,n_paths,paths,raw,event_id,error) "
                             "VALUES(?,?,?,?,?,?,?,NULL)", (now, pick[0], p, len(plist), json.dumps(plist), raw, eid))
            conn.commit()
        self.st["swaps"][s] = {"by": pick[0] if pick else None, "at": now, "event": eid,
                               "paths": pick[1] if pick else None, "silent_since": run["last_ok"],
                               "archive_last": newest}
        self._save()
        if s in self.st["told"]:   # (a warning about it from before it stopped for everyone)
            self._close(s, now)
        log.warning("%s", title)

    async def _ask_all(self, hosts):
        """[(host, paths to your prefixes or None, its answer {prefix: (raw, paths)} or why it gave none)]."""
        sem = asyncio.Semaphore(ASK_AT_ONCE)
        prefixes = list(self.svc.cfg.prefixes)

        async def one(h):
            async with sem:
                return await self._ask(h, prefixes)
        return list(await asyncio.gather(*(one(h) for h in hosts)))

    async def _ask(self, host, prefixes):
        svc = self.svc
        ok, why = svc.rv_allowed(host, "event")
        if not ok:
            return host, None, why
        svc._rv_begin(host)
        try:
            res = await asyncio.wait_for(routeviews.show_bgp_many(host, prefixes), ASK_TIMEOUT)
        except Exception as ex:
            svc._rv_failed(host, ex)
            return host, None, str(ex) or type(ex).__name__
        return host, sum(origin_paths(plist, svc.cfg.asn) for _raw, plist in res.values()), res

    async def _pings(self, host, target):
        """Whether `host` runs ping (and so traceroute) for its guests, as the first collector must."""
        svc = self.svc
        if not svc.rv_allowed(host, "event")[0]:
            return False
        svc._rv_begin(host)
        try:
            _raw, success, _rtt = await asyncio.wait_for(routeviews.ping(host, target), 90)
            return success is not None
        except Exception as ex:
            svc._rv_failed(host, ex)
            return False
