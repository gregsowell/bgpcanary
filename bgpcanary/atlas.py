"""Optional RIPE Atlas integration: data-plane checks (ping / traceroute) toward your space.

Off until an admin enters an Atlas API key (Settings > RIPE Atlas). When on:
  * baseline: a periodic ping + a slower periodic traceroute from the same probe set (worldwide spread +
    probes inside your upstreams' transit networks), so latency/loss can be grouped by the carrier each
    probe's traffic actually enters your upstream through
  * outages: a one-off traceroute burst from many probes when an outage opens / clears
Credits are budgeted against a daily cap. Hop addresses are mapped to networks with Team Cymru's bulk
IP-to-ASN whois. Everything is stored locally so the visualisations work from the database.
"""
import asyncio
import ipaddress
import re
import json
import logging
import socket
import statistics
import time

import httpx

from . import db

log = logging.getLogger("bgpcanary.atlas")
API = "https://atlas.ripe.net/api/v2"
KV_SETTINGS = "atlas_settings"
KV_STATE = "atlas_state"
DEFAULTS = {
    "enabled": False, "api_key": "", "credit_cap": 20000,
    "baseline_ping": True, "baseline_probes": 20, "ping_minutes": 30,
    "baseline_trace": True, "trace_minutes": 360,
    "carrier_probes": 2,                       # probes per transit network of your upstreams (0 = none)
    "outage_trace": True, "outage_probes": 50,
    "my_probes": [],                           # IDs of probes you host (status shown in Settings)
    "targets": [],                             # baseline targets inside your space (empty = Monitored network's probe targets)
    "target_labels": {},                       # target -> what it is ("Core router loopback", "Lumen peering")
    "target_groups": {},                       # target -> group for comparing ("border-1")
}
DEMO_TRACE, DEMO_PING, DEMO_TARGET = 5001, 1001, "193.0.14.129"   # public built-ins toward k.root-servers.net
COST = {"ping": 3, "traceroute": 30}          # credits per result at default packet settings
SCHEMA = """
CREATE TABLE IF NOT EXISTS atlas_msm (
    id INTEGER PRIMARY KEY, kind TEXT, purpose TEXT, target TEXT, event_id INTEGER, created REAL,
    stopped REAL, probes INTEGER, interval INTEGER, est_daily REAL, est_total REAL, last_fetch REAL);
CREATE TABLE IF NOT EXISTS atlas_result (
    msm_id INTEGER, prb_id INTEGER, ts INTEGER, kind TEXT, sent INTEGER, rcvd INTEGER,
    rtt_min REAL, rtt_avg REAL, rtt_max REAL, reached INTEGER, last_ip TEXT, last_asn INTEGER,
    hops TEXT, path_asns TEXT, PRIMARY KEY (msm_id, prb_id, ts));
CREATE INDEX IF NOT EXISTS idx_atlas_result_ts ON atlas_result(ts);
CREATE TABLE IF NOT EXISTS atlas_probe (prb_id INTEGER PRIMARY KEY, asn INTEGER, cc TEXT, lat REAL, lon REAL,
    fetched REAL);
CREATE TABLE IF NOT EXISTS ip_asn (ip TEXT PRIMARY KEY, asn INTEGER, name TEXT, fetched REAL);
"""
CONTINENT = {}   # country code -> region, filled below
for _region, _ccs in {
    "North America": "US CA MX GT BZ HN SV NI CR PA CU DO HT JM PR BS BB TT",
    "South America": "BR AR CL CO PE VE EC BO PY UY GY SR GF",
    "Europe": "GB IE FR DE NL BE LU CH AT IT ES PT DK NO SE FI IS PL CZ SK HU SI HR BA RS ME MK AL GR BG RO MD "
              "UA BY LT LV EE RU CY MT",
    "Asia": "CN JP KR TW HK MO SG MY TH VN PH ID IN PK BD LK NP KH LA MM MN KZ UZ KG TJ TM AF IR IQ SA AE QA BH "
            "KW OM YE JO LB SY IL PS TR GE AM AZ",
    "Oceania": "AU NZ FJ PG NC PF WS TO VU",
    "Africa": "ZA NG KE EG MA DZ TN LY GH CI SN CM ET TZ UG RW MZ ZM ZW AO NA BW MU MG SD"}.items():
    for _cc in _ccs.split():
        CONTINENT[_cc] = _region


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def settings(conn):
    s = dict(DEFAULTS)
    s.update(db.kv_get(conn, KV_SETTINGS, {}) or {})
    return s


def enabled(conn):
    s = settings(conn)
    return bool(s["enabled"] and s["api_key"])


def cymru_lookup(ips):
    """Bulk IP -> (asn, name) via whois.cymru.com. Blocking; call in a thread."""
    out = {}
    if not ips:
        return out
    s = socket.create_connection(("whois.cymru.com", 43), timeout=30)
    try:
        s.sendall(("begin\nverbose\n" + "\n".join(ips) + "\nend\n").encode())
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
    finally:
        s.close()
    for line in buf.decode(errors="replace").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 7 and parts[0].isdigit():
            out[parts[1]] = (int(parts[0]), parts[6])
        elif len(parts) >= 2 and parts[0] == "NA":
            out[parts[1]] = (None, "")
    return out


class AtlasError(RuntimeError):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?$", re.I)


def check_target(t):
    """A public IP address or a DNS name, else ValueError."""
    t = (t or "").strip()
    try:
        a = ipaddress.ip_address(t)
        if getattr(a, "scope_id", None):
            raise ValueError(f"'{t}' has a zone suffix")
    except ValueError:
        if HOSTNAME.match(t):
            return t.rstrip(".").lower()
        raise ValueError(f"'{t}' is not an IP address or host name")
    if not a.is_global:
        raise ValueError(f"{t} is not a public address; Atlas probes can't reach it")
    return str(a)


def suggest_targets(cfg):
    """[(address, label)] worth measuring inside your space: the probe targets + the first host of each prefix."""
    out = {t: "probe target" for t in cfg.probe_targets}
    for p in cfg.prefixes:
        n = ipaddress.ip_network(p, strict=False)
        h = str(n.network_address + 1 if n.num_addresses > 2 else n.network_address)
        out.setdefault(h, f"first address in {p}")
    return list(out.items())


def parse_targets(text):
    """'address  label [group]' per line -> ([address], {address: label}, {address: group}). Label and group
    are optional; a line may also hold several bare addresses separated by commas. Raises ValueError naming
    the bad line."""
    targets, labels, groups = [], {}, {}
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        try:
            many = [check_target(x) for x in parts if x] if len(parts) > 1 else None
        except ValueError:
            many = None
        if many:
            targets += many
            continue
        first, rest = (line.split(None, 1) + [""])[:2]
        try:
            t = check_target(first.rstrip(",;"))
        except ValueError as ex:
            hint = "" if "public" in str(ex) else (". Use one address per line, optionally followed by a label, "
                                                    "e.g. 192.0.2.1  Core router loopback")
            raise ValueError(f"line {n}: {ex}{hint}")
        targets.append(t)
        m = re.search(r"\[([^\]]+)\]", rest)
        if m:
            groups[t] = m.group(1).strip()[:30]
            rest = rest[:m.start()] + rest[m.end():]
        label = rest.strip().lstrip("-#,;:=").strip()
        if label:
            labels[t] = label[:60]
    targets = list(dict.fromkeys(targets))
    return targets, {t: labels[t] for t in targets if t in labels}, {t: groups[t] for t in targets if t in groups}


def _public(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_global
    except ValueError:
        return False


async def local_probe():
    """State of a RIPE Atlas software probe on this machine (read-only; no privileges needed)."""
    out = {"installed": False, "active": None, "since": None, "pubkey": None, "version": None}
    try:
        p = await asyncio.create_subprocess_exec(
            "systemctl", "show", "ripe-atlas.service", "-p", "LoadState,ActiveState,SubState,ActiveEnterTimestamp",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        props = dict(line.split("=", 1) for line in (await p.communicate())[0].decode().splitlines() if "=" in line)
        out["installed"] = props.get("LoadState") == "loaded"
        out["active"] = f"{props.get('ActiveState')} ({props.get('SubState')})" if out["installed"] else None
        out["running"] = props.get("ActiveState") == "active"
        out["since"] = props.get("ActiveEnterTimestamp") or None
    except Exception:
        pass
    for path in ("/etc/ripe-atlas/probe_key.pub", "/var/lib/bgpcanary/atlas-probe-key.pub"):   # copy made by the installer
        try:
            with open(path) as f:
                out["pubkey"] = f.read().strip()
            break
        except OSError:
            pass
    return out


class Atlas:
    def __init__(self, svc):
        self.svc = svc
        self.conn = svc.aconn
        init_db(self.conn)
        self._credits = (0, None)
        self.last_error = None

    # ------------------------------------------------------------------ api
    @property
    def s(self):
        return settings(self.conn)

    async def _api(self, method, path, key=True, **kw):
        """key: True = the saved key, False = anonymous, or a key string (self-test of an unsaved key)."""
        headers = {"Accept": "application/json"}
        key = self.s["api_key"] if key is True else key
        if key:
            headers["Authorization"] = f"Key {key}"
        async with httpx.AsyncClient(timeout=60, headers=headers) as c:
            r = await c.request(method, API + path, **kw)
        if r.status_code >= 400:
            try:
                e = r.json().get("error", {})
                # the useful part of a 400 is usually in the per-field "errors" list
                detail = "; ".join([e.get("detail") or ""] + [x.get("detail", "") for x in e.get("errors") or []
                                                              if isinstance(x, dict)]).strip("; ") or r.text[:300]
            except Exception:
                detail = r.text[:300]
            raise AtlasError(r.status_code, f"Atlas API {r.status_code}: {detail}")
        return r.json() if r.content else {}

    async def credits(self, force=False):
        ts, val = self._credits
        if val is not None and not force and time.time() - ts < 1800:
            return val
        try:
            val = await self._api("GET", "/credits/")
        except Exception as ex:
            val = {"error": str(ex)}
        self._credits = (time.time(), val)
        return val

    async def self_test(self, key=None):
        """Check everything the module needs: the key, each permission, credits, the probe directory, a real
        one-probe ping to the target, and the IP-to-ASN service. Returns [(check, ok, detail)]; ok None = a note."""
        key = key or self.s["api_key"]
        if not key:
            return [("API key", False, "No key saved or entered above.")]
        out = []
        try:
            c = await self._api("GET", "/credits/", key=key)
            bal = c.get("current_balance") or 0
            out.append(("Key works · permission: Get information about your credits", True,
                        f"Balance {bal:,.0f} credits; estimated income {c.get('estimated_daily_income') or 0:,.0f}/day."))
            if bal <= 0:
                out.append(("Credits", False, "Your balance is 0, so Atlas will refuse new measurements. Host a probe "
                                              "or ask another Atlas user to transfer credits."))
            elif bal < self.s["credit_cap"]:
                out.append(("Credits", None, f"Balance is below the daily cap ({self.s['credit_cap']:,}); Atlas stops "
                                              "accepting measurements once it runs out."))
        except Exception as ex:
            out.append(("Key works · permission: Get information about your credits", False, str(ex)))
        target = self.target()
        out.append(("Probe target", True if target else None, target or
                    f"No probe target set (Settings > Monitored network). The test below uses {DEMO_TARGET}."))
        try:
            probes = await self._connected_probes(page_size=5)
            out.append(("Atlas probe directory", bool(probes), f"{len(probes)} connected probes found in a sample."))
        except Exception as ex:
            probes = []
            out.append(("Atlas probe directory", False, str(ex)))
        # One real one-off ping from one probe (~6 credits): proves the create permission, that credits are
        # accepted, and that the target answers from outside. Atlas won't stop a measurement it is still
        # setting up, but a key without the stop permission gets 403 rather than 400, which tests that too.
        msm = None
        if probes:
            body = {"definitions": [{"type": "ping", "af": 6 if ":" in (target or "") else 4,
                                     "target": target or DEMO_TARGET, "is_oneoff": True,
                                     "description": "bgpcanary connection test"}],
                    "probes": [{"type": "probes", "value": str(probes[0]["id"]), "requested": 1}], "is_oneoff": True}
            try:
                msm = (await self._api("POST", "/measurements/", key=key, json=body))["measurements"][0]
                out.append(("Permission: Schedule a new measurement", True,
                            f"Started a one-probe test ping, measurement #{msm} (about 6 credits)."))
            except Exception as ex:
                out.append(("Permission: Schedule a new measurement", False, str(ex)))
        if msm:
            try:
                await self._api("DELETE", f"/measurements/{msm}/", key=key)
                out.append(("Permission: Stop a running measurement", True, "Granted."))
            except AtlasError as ex:
                ok = ex.status == 400
                out.append(("Permission: Stop a running measurement", ok, "Granted." if ok else str(ex)))
            except Exception as ex:
                out.append(("Permission: Stop a running measurement", False, str(ex)))
            res = []
            for _ in range(9):   # one-off results usually arrive within a minute
                await asyncio.sleep(5)
                try:
                    res = await self._api("GET", f"/measurements/{msm}/results/", key=False)
                except Exception:
                    res = []
                if res:
                    break
            if res:
                r = res[0]
                p = next((x for x in probes if x["id"] == r.get("prb_id")), {})
                where = f"probe {r.get('prb_id')} ({p.get('country_code') or '?'}, AS{p.get('asn_v4') or '?'})"
                if r.get("rcvd"):
                    out.append(("Test ping", True, f"{where} reached {r.get('dst_addr')}: {r.get('rcvd')}/{r.get('sent')} "
                                                   f"replies, average {r.get('avg', 0):.1f} ms."))
                else:
                    out.append(("Test ping", None, f"{where} got no replies from {r.get('dst_addr')}. Atlas works, but "
                                                   "check the target answers ICMP from the internet."))
            else:
                out.append(("Test ping", None, f"No result within 45 s; see https://atlas.ripe.net/measurements/{msm}/ "
                                               "in a minute or two."))
        try:
            r = await asyncio.to_thread(cymru_lookup, [DEMO_TARGET])
            ok = bool(r.get(DEMO_TARGET, (None,))[0])
            out.append(("IP-to-network lookups (whois.cymru.com, TCP 43)", ok,
                        "Names the networks on traceroute paths." if ok else "No answer; outbound TCP 43 may be blocked."))
        except Exception as ex:
            out.append(("IP-to-network lookups (whois.cymru.com, TCP 43)", False, f"{ex}. Outbound TCP 43 may be blocked."))
        return out

    async def my_probes(self):
        """Public Atlas status of the probes you host (IDs from Settings)."""
        ids = self.s.get("my_probes") or []
        if not ids:
            return []
        try:
            d = await self._api("GET", "/probes/", key=False, params={
                "id__in": ",".join(map(str, ids)), "page_size": 50,
                "fields": "id,status,status_since,asn_v4,country_code,description,type"})
            return d.get("results", [])
        except Exception as ex:
            return [{"id": i, "error": str(ex)} for i in ids]

    # ------------------------------------------------------------------ budget
    def _spent_today(self):
        oneoff = sum(r["est_total"] or 0 for r in self.conn.execute(
            "SELECT est_total FROM atlas_msm WHERE purpose!='baseline' AND created >= ?",
            (time.time() - 86400,)))
        periodic = sum(r["est_daily"] or 0 for r in self.conn.execute(
            "SELECT est_daily FROM atlas_msm WHERE purpose='baseline' AND stopped IS NULL"))
        return periodic + oneoff

    def spent_oneoff(self):
        return sum(r["est_total"] or 0 for r in self.conn.execute(
            "SELECT est_total FROM atlas_msm WHERE purpose NOT IN ('baseline', 'demo') AND created >= ?",
            (time.time() - 86400,)))

    def budget_ok(self, extra):
        return self._spent_today() + extra <= self.s["credit_cap"]

    # ------------------------------------------------------------------ probes
    async def probes_in(self, n, asn=None, country=None):
        """Up to n connected probes inside a network or country, spread across networks/countries."""
        ps = await self._connected_probes(**({"asn_v4": asn} if asn else {"country_code": country.upper()}))
        by = {}
        for p in ps:
            by.setdefault(p.get("asn_v4") if country else p.get("country_code"), []).append(p)
        chosen, keys = [], list(by)
        while len(chosen) < n and any(by.values()):
            for k in keys:
                if by[k] and len(chosen) < n:
                    chosen.append(by[k].pop(0))
        self._store_probes(chosen)
        return [p["id"] for p in chosen]

    async def _connected_probes(self, **filters):
        params = {"status": 1, "is_public": "true", "fields": "id,asn_v4,country_code,geometry",
                  "page_size": 500, **filters}
        d = await self._api("GET", "/probes/", key=False, params=params)
        return d.get("results", [])

    async def pick_probes(self, n_world, per_carrier, carriers):
        """Explicit probe IDs: a worldwide spread (round-robin across countries) + probes inside each carrier."""
        chosen, meta = [], {}
        for asn in carriers if per_carrier else []:
            try:
                ps = await self._connected_probes(asn_v4=asn)
            except Exception:
                ps = []
            for p in ps[:per_carrier]:
                chosen.append(p["id"])
                meta[p["id"]] = p
        world = await self._connected_probes()
        by_cc = {}
        for p in world:
            if p["id"] not in meta:
                by_cc.setdefault(p.get("country_code") or "??", []).append(p)
        ccs = sorted(by_cc, key=lambda c: -len(by_cc[c]))
        i = 0
        while len(chosen) < n_world + per_carrier * len(carriers) and any(by_cc.values()):
            cc = ccs[i % len(ccs)]
            if by_cc[cc]:
                p = by_cc[cc].pop(0)
                chosen.append(p["id"])
                meta[p["id"]] = p
            i += 1
            if i > 10000:
                break
        self._store_probes(meta.values())
        return chosen

    def _store_probes(self, plist):
        for p in plist:
            g = (p.get("geometry") or {}).get("coordinates") or [None, None]
            self.conn.execute("INSERT OR REPLACE INTO atlas_probe(prb_id,asn,cc,lat,lon,fetched) VALUES(?,?,?,?,?,?)",
                              (p["id"], p.get("asn_v4"), p.get("country_code"), g[1], g[0], time.time()))
        self.conn.commit()

    async def ensure_probe_meta(self, ids):
        known = {r["prb_id"] for r in self.conn.execute("SELECT prb_id FROM atlas_probe")}
        missing = [i for i in ids if i not in known]
        for k in range(0, len(missing), 100):
            chunk = missing[k:k + 100]
            d = await self._api("GET", "/probes/", key=False,
                                params={"id__in": ",".join(map(str, chunk)), "fields": "id,asn_v4,country_code,geometry",
                                        "page_size": 100})
            self._store_probes(d.get("results", []))

    async def ip_asns(self, ips):
        ips = sorted({i for i in ips if i and _public(i)})
        if not ips:
            return {}
        q = ",".join("?" * len(ips))
        cached = {r["ip"]: (r["asn"], r["name"]) for r in self.conn.execute(
            f"SELECT ip, asn, name FROM ip_asn WHERE ip IN ({q}) AND fetched > ?", ips + [time.time() - 7 * 86400])}
        todo = [i for i in ips if i not in cached]
        if todo:
            res = await asyncio.to_thread(cymru_lookup, todo)
            for ip in todo:
                asn, name = res.get(ip, (None, ""))
                self.conn.execute("INSERT OR REPLACE INTO ip_asn(ip,asn,name,fetched) VALUES(?,?,?,?)",
                                  (ip, asn, name, time.time()))
                cached[ip] = (asn, name)
            self.conn.commit()
        return cached

    # ------------------------------------------------------------------ measurements
    async def create(self, kind, target, probe_ids, purpose, interval=None, event_id=None, description=""):
        oneoff = interval is None
        unit = COST[kind] * (2 if oneoff else 1)
        est_total = unit * len(probe_ids) if oneoff else None
        est_daily = None if oneoff else unit * len(probe_ids) * 86400 / interval
        if not self.budget_ok(est_total or est_daily):
            raise RuntimeError(f"daily Atlas credit cap ({self.s['credit_cap']}) would be exceeded")
        af = 6 if ":" in target else 4   # host names are measured over IPv4
        definition = {"type": kind, "af": af, "target": target, "description": description or f"bgpcanary {purpose}",
                      "is_oneoff": oneoff}
        if kind == "traceroute":
            definition.update({"protocol": "ICMP", "packets": 3, "paris": 16})
        if not oneoff:
            definition["interval"] = interval
        body = {"definitions": [definition], "is_oneoff": oneoff,
                "probes": [{"type": "probes", "value": ",".join(map(str, probe_ids)), "requested": len(probe_ids)}]}
        d = await self._api("POST", "/measurements/", json=body)
        msm = d["measurements"][0]
        self.conn.execute("INSERT OR REPLACE INTO atlas_msm(id,kind,purpose,target,event_id,created,probes,interval,"
                          "est_daily,est_total,last_fetch) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (msm, kind, purpose, target, event_id, time.time(), len(probe_ids), interval, est_daily,
                           est_total, time.time() - 600))
        self.conn.commit()
        log.info("Atlas %s %s measurement %s created (%d probes)", purpose, kind, msm, len(probe_ids))
        if oneoff:
            asyncio.get_running_loop().create_task(self._follow(msm, kind))
        return msm

    async def _follow(self, msm, kind):
        """Pull a one-off's results soon after it starts instead of waiting for the 5-minute poll."""
        for delay in (90, 90, 120, 180):
            await asyncio.sleep(delay)
            try:
                await self.fetch(msm, kind, time.time() - 3600)
                self.conn.execute("UPDATE atlas_msm SET last_fetch=? WHERE id=?", (time.time(), msm))
                self.conn.commit()
                if kind == "ping":
                    self.check_loss()
            except Exception as ex:
                log.warning("Atlas fetch %s: %s", msm, ex)

    async def stop(self, msm_id):
        try:
            await self._api("DELETE", f"/measurements/{msm_id}/")
        except Exception as ex:
            log.warning("stopping Atlas measurement %s: %s", msm_id, ex)
        self.conn.execute("UPDATE atlas_msm SET stopped=? WHERE id=?", (time.time(), msm_id))
        self.conn.commit()

    def label(self, t):
        return (self.s.get("target_labels") or {}).get(t, "")

    def group(self, t):
        return (self.s.get("target_groups") or {}).get(t, "")

    def targets(self):
        """Baseline targets: the Atlas list from Settings, else the Monitored network's probe targets."""
        return list(self.s.get("targets") or self.svc.cfg.probe_targets)

    def target(self):
        t = self.targets()
        return t[0] if t else None

    async def carriers(self):
        """Transit networks of your upstreams (from the global table) - where per-carrier probes come from."""
        from . import tree
        ups = sorted(set(self.svc.cfg.expected_upstreams))
        nbrs = await tree.upstream_neighbors(self.svc.sourceapp, ups)
        return sorted({n for u in ups for n, _p in nbrs.get(u, []) if n != self.svc.cfg.asn})

    async def ensure_baseline(self):
        """Keep one periodic ping + traceroute per baseline target, from the same probe set; targets whose
        settings changed are re-created, removed targets are stopped."""
        self.last_error = None   # set again below if a target still can't start
        s = self.s
        st = db.kv_get(self.conn, KV_STATE, {}) or {}
        if "baseline_by" not in st:   # older single-target state
            old = dict(st.get("baseline_sig") or {})
            st["baseline_by"] = {old.pop("target"): {"sig": old, "msms": st.get("baseline_msms", [])}} if old else {}
        sig = {"probes": s["baseline_probes"], "carrier_probes": s["carrier_probes"],
               "ping": s["ping_minutes"] if s["baseline_ping"] else 0,
               "trace": s["trace_minutes"] if s["baseline_trace"] else 0}
        want = self.targets() if (sig["ping"] or sig["trace"]) else []
        by = st["baseline_by"]

        def save():
            st["baseline_msms"] = [m for v in by.values() for m in v["msms"]]
            db.kv_set(self.conn, KV_STATE, st)
            self.conn.commit()
        # re-create targets whose settings changed, and any left half-made (e.g. the credit cap ran out midway)
        for t in [t for t in by if t not in want or by[t]["sig"] != sig or by[t].get("partial")]:
            for m in by.pop(t)["msms"]:
                await self.stop(m)
            save()
        todo = [t for t in want if t not in by]
        if not todo:
            return
        carriers = await self.carriers()
        probes = st.get("baseline_probes") if by and st.get("baseline_probes_sig") == sig else None
        if not probes:   # same probes for every target, so they can be compared
            probes = await self.pick_probes(s["baseline_probes"], s["carrier_probes"], carriers)
            st.update({"baseline_probes": probes, "baseline_probes_sig": sig})
        per_target = len(probes) * 86400 * ((COST["ping"] / (sig["ping"] * 60) if sig["ping"] else 0) +
                                            (COST["traceroute"] / (sig["trace"] * 60) if sig["trace"] else 0))
        for t in todo:
            if not self.budget_ok(per_target):   # all of a target's measurements, or none of them
                self.last_error = (f"baseline for {t} not started: it needs ~{per_target:,.0f} credits/day and the "
                                   f"daily cap ({s['credit_cap']:,}) would be exceeded")
                log.warning("Atlas: %s", self.last_error)
                continue
            made = []
            try:
                if sig["ping"]:
                    made.append(await self.create("ping", t, probes, "baseline", interval=sig["ping"] * 60))
                if sig["trace"]:
                    made.append(await self.create("traceroute", t, probes, "baseline", interval=sig["trace"] * 60))
            finally:
                if made:
                    by[t] = {"sig": sig, "msms": made, "partial": len(made) < bool(sig["ping"]) + bool(sig["trace"])}
                    save()

    async def outage_trace(self, event_id, phase):
        if not self.s["outage_trace"]:
            return None
        return await self.burst(f"outage-{phase}", event_id, f"bgpcanary outage {phase} (event {event_id})")

    async def burst(self, purpose, event_id=None, description="bgpcanary traceroute"):
        """One-off traceroute from ~outage_probes probes: one inside each upstream transit, the rest worldwide."""
        s, target = self.s, self.target()
        if not target:
            raise RuntimeError("no probe target set (Settings > Monitored network)")
        carriers = await self.carriers()
        probes = await self.pick_probes(max(0, s["outage_probes"] - len(carriers)), 1, carriers)
        return await self.create("traceroute", target, probes[:s["outage_probes"]], purpose,
                                 event_id=event_id, description=description)

    async def adhoc(self, target, kinds, probes, user=""):
        """One-off ping and/or traceroute toward any public target from the given probes."""
        made = []
        for kind in kinds:
            made.append(await self.create(kind, target, probes, "adhoc", description=f"bgpcanary ad-hoc {kind} ({user})"))
        return made

    async def shift_check(self, event_id):
        """Checks when an upstream shift opens: a one-off ping to each baseline target (up to 3) from worldwide
        probes plus one inside each upstream transit, and the traceroute burst. Results feed check_loss()."""
        s, made = self.s, []
        targets = self.targets()[:3]
        if not targets:
            return made
        carriers = await self.carriers()
        probes = await self.pick_probes(max(0, 30 - len(carriers)), 1, carriers)
        for t in targets:
            made.append(await self.create("ping", t, probes[:40], "check", event_id=event_id,
                                          description=f"bgpcanary upstream-shift check (event {event_id})"))
        if s["outage_trace"]:
            made.append(await self.burst("check", event_id, f"bgpcanary upstream-shift traceroute (event {event_id})"))
        return made

    def check_loss(self):
        """Reachability loss: a round of Atlas pings to one of your own targets (baseline or check) in which at
        least loss_alert_pct of the probes got no reply at all opens a critical event, whatever BGP shows."""
        pct = self.svc.cfg.loss_alert_pct
        mine = set(self.targets())
        if pct <= 0 or not mine:
            return
        now = time.time()
        seen = db.kv_get(self.conn, "atlas_loss_seen", []) or []
        done = set(seen)
        rows = self.conn.execute(
            "SELECT r.msm_id, r.prb_id, r.ts, r.sent, r.rcvd, m.target FROM atlas_result r JOIN atlas_msm m "
            "ON m.id=r.msm_id WHERE r.kind='ping' AND m.purpose IN ('baseline','check') AND r.ts>? "
            "ORDER BY r.msm_id, r.ts", (now - 6 * 3600,)).fetchall()
        rounds, cur = [], None
        for r in rows:   # a round = one measurement's results with no gap longer than 2 minutes
            if r["target"] not in mine:
                continue
            if not cur or cur[-1]["msm_id"] != r["msm_id"] or r["ts"] - cur[-1]["ts"] > 120:
                cur = []
                rounds.append(cur)
            cur.append(r)
        for rs in rounds:
            key = f"{rs[0]['msm_id']}|{int(rs[0]['ts'])}"
            if key in done or len(rs) < 10 or rs[-1]["ts"] > now - 300:   # too small, or still arriving
                continue
            done.add(key)
            seen.append(key)
            failed = [r for r in rs if r["sent"] and not r["rcvd"]]
            if len(failed) * 100 >= pct * len(rs):
                self._loss_event(rs, failed)
        db.kv_set(self.conn, "atlas_loss_seen", seen[-3000:])
        self.conn.commit()

    def _loss_event(self, rs, failed):
        t = rs[0]["target"]
        label = self.label(t)
        start, end = min(r["ts"] for r in failed), max(r["ts"] for r in failed) + 1
        prefix = None
        try:
            ip = ipaddress.ip_address(t)
            prefix = next((p for p in self.svc.cfg.prefixes if ip in ipaddress.ip_network(p, strict=False)), None)
        except ValueError:
            pass
        shift = self.conn.execute(
            "SELECT id FROM events WHERE kind='upstream_shift' AND (prefix=? OR ? IS NULL) AND start_ts<=? "
            "AND COALESCE(end_ts, ?)>=?", (prefix, prefix, end + 300, time.time(), start - 300)).fetchone()
        title = (f"{t}{f' ({label})' if label else ''} unreachable from {len(failed)} of {len(rs)} RIPE Atlas probes"
                 + (" during an upstream shift" if shift else ""))
        self.svc.engine._new_event(
            "reachability_loss", "critical", prefix, start, title,
            {"target": t, "label": label, "msm": rs[0]["msm_id"], "probes": len(rs),
             "failed_probes": sorted(r["prb_id"] for r in failed), "shift_event": shift["id"] if shift else None,
             "note": "Packets to your address didn't come back to most probes in this round of RIPE Atlas pings: "
                     "for that moment, your space was unreachable from much of the internet"
                     + (" while routes were moving between your upstreams (see the linked upstream-shift event)."
                        if shift else ", even though BGP may have looked normal.")},
            end_ts=end)

    async def disable(self):
        """Stop the periodic baseline measurements (one-offs finish on their own)."""
        st = db.kv_get(self.conn, KV_STATE, {}) or {}
        for m in st.get("baseline_msms", []):
            await self.stop(m)
        st.update({"baseline_sig": None, "baseline_msms": [], "baseline_by": {}})
        db.kv_set(self.conn, KV_STATE, st)
        self.conn.commit()

    async def load_demo(self):
        """Preview data without an API key or credits: the last day of RIPE's own public built-in
        measurements toward k.root-servers.net, from a worldwide sample of probes."""
        probes = await self.pick_probes(30, 0, [])
        since = time.time() - 86400
        for msm, kind in ((DEMO_TRACE, "traceroute"), (DEMO_PING, "ping")):
            self.conn.execute("INSERT OR REPLACE INTO atlas_msm(id,kind,purpose,target,created,probes,last_fetch) "
                              "VALUES(?,?,?,?,?,?,?)", (msm, kind, "demo", DEMO_TARGET, time.time(), len(probes),
                                                        time.time()))
            self.conn.execute("DELETE FROM atlas_result WHERE msm_id=?", (msm,))
            self.conn.commit()
            n = await self.fetch(msm, kind, since, probe_ids=probes)
            log.info("Atlas demo: %d %s results from measurement %s", n, kind, msm)

    def clear_demo(self):
        self.conn.execute("DELETE FROM atlas_result WHERE msm_id IN (SELECT id FROM atlas_msm WHERE purpose='demo')")
        self.conn.execute("DELETE FROM atlas_msm WHERE purpose='demo'")
        self.conn.commit()

    # ------------------------------------------------------------------ results
    async def fetch(self, msm_id, kind, since, until=None, probe_ids=None):
        params = {"start": int(since), "format": "json"}
        if until:
            params["stop"] = int(until)
        if probe_ids:
            params["probe_ids"] = ",".join(map(str, probe_ids))
        rows = await self._api("GET", f"/measurements/{msm_id}/results/", key=False, params=params)
        await self.store(msm_id, kind, rows)
        return len(rows)

    async def store(self, msm_id, kind, rows):
        if not rows:
            return
        await self.ensure_probe_meta(sorted({r["prb_id"] for r in rows}))
        hop_ips = [h.get("from") for r in rows if r.get("type") == "traceroute" for x in r.get("result", [])
                   for h in x.get("result", []) if isinstance(h, dict)]
        asn_of = await self.ip_asns(hop_ips + [r.get("dst_addr") for r in rows])
        for r in rows:
            if r.get("type") == "ping":
                rtts = [x["rtt"] for x in r.get("result", []) if isinstance(x, dict) and "rtt" in x]
                self.conn.execute(
                    "INSERT OR REPLACE INTO atlas_result(msm_id,prb_id,ts,kind,sent,rcvd,rtt_min,rtt_avg,rtt_max,"
                    "reached) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (msm_id, r["prb_id"], r["timestamp"], "ping", r.get("sent"), r.get("rcvd"),
                     min(rtts) if rtts else None, statistics.median(rtts) if rtts else None,
                     max(rtts) if rtts else None, 1 if rtts else 0))
            elif r.get("type") == "traceroute":
                hops, last_ip, dst = [], None, r.get("dst_addr")
                for x in r.get("result", []):
                    rep = [h for h in x.get("result", []) if isinstance(h, dict) and h.get("from")]
                    if rep:
                        ip = rep[0]["from"]
                        rtt = min((h["rtt"] for h in rep if "rtt" in h), default=None)
                        asn = asn_of.get(ip, (None, ""))[0]
                        hops.append({"h": x.get("hop"), "ip": ip, "rtt": rtt, "asn": asn})
                        last_ip = ip
                    else:
                        hops.append({"h": x.get("hop"), "ip": None, "rtt": None, "asn": None})
                reached = 1 if (r.get("destination_ip_responded") or any(h["ip"] == dst for h in hops)) else 0
                path = []
                for h in hops:
                    if h["asn"] and (not path or path[-1] != h["asn"]):
                        path.append(h["asn"])
                rtt_last = next((h["rtt"] for h in reversed(hops) if h["rtt"] is not None), None)
                self.conn.execute(
                    "INSERT OR REPLACE INTO atlas_result(msm_id,prb_id,ts,kind,reached,rtt_avg,last_ip,last_asn,hops,"
                    "path_asns) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (msm_id, r["prb_id"], r["timestamp"], "traceroute", reached, rtt_last if reached else None,
                     last_ip, asn_of.get(last_ip, (None, ""))[0] if last_ip else None, json.dumps(hops),
                     json.dumps(path)))
        self.conn.commit()
        # names for ASNs seen on paths (shown in visualisations)
        for ip, (asn, name) in asn_of.items():
            if asn and name:
                self.conn.execute("INSERT OR IGNORE INTO asn_names(asn,name,fetched) VALUES(?,?,?)",
                                  (asn, name.rsplit(",", 1)[0], time.time()))
        self.conn.commit()

    async def poll(self):
        """Pull new results for active measurements (baseline + one-offs from the last 6 hours)."""
        for m in self.conn.execute("SELECT * FROM atlas_msm WHERE purpose != 'demo' AND (stopped IS NULL OR stopped > ?) "
                                   "AND (purpose='baseline' OR created > ?)",
                                   (time.time() - 3600, time.time() - 6 * 3600)).fetchall():
            since = (m["last_fetch"] or m["created"]) - 900   # probes can upload late
            try:
                n = await self.fetch(m["id"], m["kind"], since)
                self.conn.execute("UPDATE atlas_msm SET last_fetch=? WHERE id=?", (time.time(), m["id"]))
                self.conn.commit()
                if n:
                    log.info("Atlas %s: %d new results", m["id"], n)
            except Exception as ex:
                self.last_error = str(ex)
                log.warning("Atlas fetch %s: %s", m["id"], ex)
        try:
            self.check_loss()
        except Exception:
            log.exception("Atlas reachability check failed")

    async def loop(self):
        await asyncio.sleep(90)
        while True:
            try:
                if enabled(self.conn):
                    await self.ensure_baseline()
                    await self.poll()
            except Exception as ex:
                self.last_error = str(ex)
                log.warning("Atlas: %s", ex)
            await asyncio.sleep(300)
