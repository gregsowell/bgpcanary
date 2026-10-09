"""Registry watch: IRR route objects, as-set membership, simulated upstream filters and RPKI.

Runs periodically (and on demand). Problems that can break propagation *before* it shows up in BGP -
a conflicting/stale route object, a route object claiming our ASN for someone else's space, being
dropped from an upstream's as-set, an upstream with no as-set, an invalid ROA - become events.
Changes to any of it are recorded as FYI events, so there is a history of who changed what and when.
"""
import asyncio
import hashlib
import ipaddress
import json
import logging
import socket
import time

import httpx

from . import db, ripe

log = logging.getLogger("bgpcanary.registry")
IRR_HOST = "whois.radb.net"   # IRRd mirror of RADB, ARIN, LEVEL3, NTTCOM, ALTDB, RIPE, RPKI, ...
KV_TOGGLE = "registry_watch_enabled"
KV_SNAPSHOT = "registry_snapshot"
FYI_KINDS = ("irr_change", "asset_change", "filter_sim")


def enabled(cfg, conn):
    return bool(db.kv_get(conn, KV_TOGGLE, cfg.registry_watch))


def _whois(query, irrd=False, timeout=60):
    s = socket.create_connection((IRR_HOST, 43), timeout=timeout)
    try:
        s.sendall((("!!\n" + query + "\n!q\n") if irrd else (query + "\r\n")).encode())
        out = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            out += c
    finally:
        s.close()
    return out.decode(errors="replace")


def _irrd_data(raw):
    """Payload of an IRRd '!' response ('A<len>\\n...\\nC'), or '' for D/C/F."""
    raw = raw.lstrip()
    if not raw.startswith("A"):
        return ""
    head, _, rest = raw.partition("\n")
    return rest[:int(head[1:])]


def _objects(text):
    """Parse RPSL text into [{attr: value-or-list, '_text': raw}]"""
    out = []
    for block in text.strip().split("\n\n"):
        if not block.strip() or block.lstrip().startswith("%"):
            continue
        obj, last = {"_text": block.strip()}, None
        for line in block.splitlines():
            if line.startswith((" ", "\t", "+")) and last:
                obj[last] = (obj[last] if isinstance(obj[last], str) else " ".join(obj[last])) + " " + line.strip()
                continue
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.split("#")[0].strip() if k.strip().lower() == "source" else v.strip()
            if k in obj:
                obj[k] = (obj[k] if isinstance(obj[k], list) else [obj[k]]) + [v]
            else:
                obj[k] = v
            last = k
        out.append(obj)
    return out


def _first(v):
    return v[0] if isinstance(v, list) else (v or "")


def _members(obj):
    v = obj.get("members", [])
    v = v if isinstance(v, list) else [v]
    return {m.strip().upper() for x in v for m in x.replace(",", " ").split()}


def _fp(obj):
    return hashlib.sha1(obj["_text"].encode()).hexdigest()[:12]


class RegistryWatch:
    def __init__(self, svc):
        self.svc = svc
        self.cfg = svc.cfg
        self.last_run = None
        self.running = False
        self.report = None     # dict for the Registry page
        self.peeringdb = {}    # asn -> (ts, [as-set names])

    async def _q(self, query, irrd=False):
        return await asyncio.to_thread(_whois, query, irrd)

    async def _pdb_assets(self, asn):
        c = self.peeringdb.get(asn)
        if c and time.time() - c[0] < 86400:
            return c[1]
        sets = []
        try:
            async with httpx.AsyncClient(timeout=30) as cl:
                r = await cl.get("https://www.peeringdb.com/api/net", params={"asn": asn})
                for net in r.json().get("data", []):
                    for tok in (net.get("irr_as_set") or "").replace(",", " ").split():
                        sets.append(tok.split("::")[-1])
        except Exception as ex:
            log.warning("peeringdb lookup AS%s failed: %s", asn, ex)
            return c[1] if c else []
        self.peeringdb[asn] = (time.time(), sets)
        return sets

    # ------------------------------------------------------------------ the checks
    async def collect(self):
        asn = self.cfg.asn
        nets = {p: ipaddress.ip_network(p) for p in self.cfg.prefixes}
        rep = {"ts": time.time(), "checks": [], "objects": [], "foreign": [], "assets": [], "filters": [], "rpki": []}

        def check(name, status, msg, key=None, severity=None, detail=None, prefix=None):
            rep["checks"].append({"name": name, "status": status, "msg": msg, "key": key, "severity": severity,
                                  "detail": detail or {}, "prefix": prefix})

        # 1. route objects covering our prefixes (exact, less- and more-specific), every registry
        for p, net in nets.items():
            objs = _objects(_irrd_data(await self._q(f"!r{p},L", irrd=True)))
            objs += _objects(_irrd_data(await self._q(f"!r{p},M", irrd=True)))
            ours_ok = []
            for o in objs:
                route, origin, src = _first(o.get("route") or o.get("route6")), _first(o.get("origin")).upper(), _first(o.get("source")).upper()
                try:
                    rnet = ipaddress.ip_network(route, strict=False)
                except ValueError:
                    continue
                rel = "exact" if rnet == net else ("more-specific" if rnet.subnet_of(net) else "covering")
                rpki = _first(o.get("rpki-ov-state")) or ("n/a" if src == "RPKI" else "")
                row = {"prefix": p, "route": route, "origin": origin, "source": src, "rel": rel, "rpki": rpki,
                       "mnt": _first(o.get("mnt-by")), "modified": _first(o.get("last-modified")),
                       "descr": _first(o.get("descr")), "fp": _fp(o), "text": o["_text"]}
                rep["objects"].append(row)
                if origin == f"AS{asn}" and rel == "exact" and src != "RPKI":
                    ours_ok.append(row)
                if origin != f"AS{asn}" and rel in ("exact", "more-specific"):
                    sev = "warning"
                    check("Conflicting route object", "fail",
                          f"{src} has route {route} with origin {origin} (maintainer {row['mnt']}). Filters built from "
                          f"{origin}'s records will treat this as {origin}'s space; it can block or wrongly permit your route. "
                          f"Ask the maintainer to delete it.",
                          key=f"reg|conflict|{src}|{route}|{origin}", severity=sev, prefix=p,
                          detail={"object": o["_text"]})
            if ours_ok:
                bad = [r for r in ours_ok if r["rpki"] and r["rpki"] != "valid"]
                check("Route object for your prefix", "warn" if bad else "pass",
                      f"{p}: origin AS{asn} registered in " + ", ".join(sorted({r['source'] for r in ours_ok}))
                      + (f"; RPKI state not valid in {', '.join(r['source'] for r in bad)}" if bad else ""), prefix=p)
            else:
                check("Route object for your prefix", "fail",
                      f"No IRR route object for {p} with origin AS{asn}. Most transit filters are built from IRR, "
                      f"so providers may reject the route.", key=f"reg|noroute|{p}", severity="warning", prefix=p)

        # 2. route objects that claim OUR ASN for space that isn't ours (monitored prefixes + owned_space)
        mine = list(nets.values()) + [ipaddress.ip_network(x, strict=False) for x in self.cfg.owned_space]
        for o in _objects(await self._q(f"-i origin AS{asn}")):
            route, src = _first(o.get("route") or o.get("route6")), _first(o.get("source")).upper()
            if src == "RPKI":
                continue  # derived from ROAs the address holder created - not an IRR claim someone typed in
            try:
                rnet = ipaddress.ip_network(route, strict=False)
            except ValueError:
                continue
            if any(rnet.version == n.version and (rnet == n or rnet.subnet_of(n)) for n in mine):
                continue
            rep["foreign"].append({"route": route, "source": src, "mnt": _first(o.get("mnt-by")),
                                   "modified": _first(o.get("last-modified")), "text": o["_text"]})
        if len(rep["foreign"]) > 20:
            # a network with lots of space it hasn't listed: one summary instead of one warning per object
            ex = ", ".join(f"{f['route']} ({f['source']})" for f in rep["foreign"][:10])
            check("Route object claiming your ASN", "warn",
                  f"{len(rep['foreign'])} IRR route objects claim AS{asn} for space outside your monitored prefixes, "
                  f"e.g. {ex}. If this is space you hold, add it under 'Other address space you hold' in "
                  f"Settings > Monitoring so only unexpected objects are reported.",
                  key="reg|foreign|summary", severity="info", detail={"count": len(rep["foreign"])})
        else:
            for f in rep["foreign"]:
                check("Route object claiming your ASN", "fail",
                      f"{f['source']} has route {f['route']} with origin AS{asn} (maintainer {f['mnt']}), but it "
                      f"isn't in your monitored or owned space. Someone can use it to get a hijack of {f['route']} "
                      f"past IRR filters using your ASN.",
                      key=f"reg|foreign|{f['source']}|{f['route']}", severity="warning", detail={"object": f["text"]})
        if not rep["foreign"]:
            check("Route object claiming your ASN", "pass", f"No IRR route objects claim AS{asn} for space outside "
                  f"{', '.join(self.cfg.prefixes)}")

        # 3. as-sets that list our ASN directly
        for o in _objects(await self._q(f"-i members AS{asn}")):
            name, src = _first(o.get("as-set")).upper(), _first(o.get("source")).upper()
            if not name:
                continue
            rep["assets"].append({"name": name, "source": src, "mnt": _first(o.get("mnt-by")),
                                  "modified": _first(o.get("last-modified")), "fp": _fp(o)})

        # 4. simulate the prefix filter each upstream's transits would build
        async def irr_prefixes(a):
            """IPv4 (!g) + IPv6 (!6) prefixes registered with origin AS<a> - what bgpq4-style tools build from."""
            v4 = _irrd_data(await self._q(f"!gAS{a}", irrd=True)).split()
            v6 = _irrd_data(await self._q(f"!6AS{a}", irrd=True)).split()
            return {str(ipaddress.ip_network(x, strict=False)) for x in v4 + v6 if "/" in x}

        our_prefixes = await irr_prefixes(asn)
        for u in self.cfg.expected_upstreams:
            sets = await self._pdb_assets(u)
            if not sets:
                bare = await irr_prefixes(u)
                inc = [p for p in self.cfg.prefixes if p in bare]
                # as-sets named after this upstream that already list us (exist, but nobody is told to use them)
                unlisted = [a for a in rep["assets"] if a["name"].startswith(f"AS{u}:")]
                row = {"upstream": u, "set": None, "includes_asn": False, "includes_prefix": bool(inc),
                       "via": "bare ASN", "unregistered_sets": [f"{a['name']} ({a['source']})" for a in unlisted]}
                rep["filters"].append(row)
                if unlisted:
                    names = ", ".join(row["unregistered_sets"])
                    msg = (f"AS{u} has no as-set in PeeringDB, but {names} lists AS{asn}. Transits only use an "
                           f"as-set when AS{u} tells them to, so filters built from AS{u}'s records "
                           + ("include" if inc else "do NOT include") + f" your prefix. Ask AS{u} to put {names} "
                           f"(or a parent set that includes it) in its PeeringDB 'IRR as-set' field and give it to each "
                           f"of its transit providers.")
                else:
                    msg = (f"AS{u} publishes no IRR as-set in PeeringDB. Its transits can only build filters from "
                           f"AS{u}'s own route objects, which " + ("include" if inc else "do NOT include")
                           + f" your prefix. Ask AS{u} to create an as-set containing AS{asn} and register it with "
                           f"every transit.")
                check("Upstream filter simulation", "warn", msg, key=f"reg|filter|{u}|noset", severity="info",
                      detail=row)
                continue
            for sname in sets:
                members = set(_irrd_data(await self._q(f"!i{sname},1", irrd=True)).split())
                has_asn = f"AS{asn}" in members
                inc = [p for p in self.cfg.prefixes if has_asn and p in our_prefixes]
                row = {"upstream": u, "set": sname, "includes_asn": has_asn, "includes_prefix": bool(inc),
                       "via": "as-set", "set_size": len(members)}
                rep["filters"].append(row)
                if has_asn and inc:
                    check("Upstream filter simulation", "pass",
                          f"{sname} (AS{u}'s as-set in PeeringDB) includes AS{asn}, and IRR has your prefix -> a filter "
                          f"built from it would accept {', '.join(inc)}")
                else:
                    why = (f"{sname} doesn't include AS{asn}" if not has_asn
                           else f"IRR has no route object for your prefix with origin AS{asn}")
                    check("Upstream filter simulation", "fail",
                          f"A transit filter built from {sname} (AS{u}'s as-set) would reject your prefix: {why}.",
                          key=f"reg|filter|{u}|{sname}", severity="info", detail=row)

        # 5. RPKI
        for p in self.cfg.prefixes:
            try:
                d = await ripe.stat("rpki-validation", self.svc.sourceapp, resource=f"AS{asn}", prefix=p)
                st = d.get("status", "unknown")
                roas = d.get("validating_roas", [])
            except Exception as ex:
                log.warning("rpki-validation %s failed: %s", p, ex)
                continue
            rep["rpki"].append({"prefix": p, "status": st, "roas": roas})
            if st == "valid":
                check("RPKI", "pass", f"{p} origin AS{asn} is RPKI-valid ({len(roas)} ROA{'s' if len(roas) != 1 else ''})",
                      prefix=p)
            else:
                check("RPKI", "fail", f"{p} origin AS{asn} is RPKI-{st}. " +
                      ("Networks that drop invalids will reject it!" if st == "invalid" else
                       "Create a ROA so networks can validate it."),
                      key=f"reg|rpki|{p}", severity="critical" if st == "invalid" else "warning", prefix=p)
        return rep

    # ------------------------------------------------------------------ events + change tracking
    async def run(self):
        if self.running:
            return
        conn, eng = self.svc.aconn, self.svc.engine
        if not enabled(self.cfg, conn):
            self.close_all()
            return
        self.running = True
        try:
            rep = await self.collect()
        except Exception:
            log.exception("registry check failed")
            self.running = False
            return
        self.running = False
        self.report, self.last_run = rep, rep["ts"]
        now = time.time()
        # open/close problem events
        current = {}
        for c in rep["checks"]:
            if c["key"]:
                current[c["key"]] = c
        open_now = {k: a for k, a in eng.anomalies.items() if k.startswith("reg|")}
        for key, c in current.items():
            if key in open_now:
                continue
            eid = eng._new_event(c["key"].split("|")[1] == "filter" and "filter_sim" or "registry", c["severity"],
                                 c["prefix"] or self.cfg.prefixes[0], now, c["msg"].split(". ")[0][:180],
                                 {"ext_key": key, "note": c["msg"], **{k: v for k, v in c["detail"].items()
                                                                        if k != "object"},
                                  "object": c["detail"].get("object")})
            eng.anomalies[key] = {"id": eid, "zero_since": None, "sev": c["severity"]}
        for key, a in open_now.items():
            if key not in current:
                eng._close_event(a["id"], now)
                del eng.anomalies[key]
        # change history (point FYI events); first run only records a baseline
        prev = db.kv_get(conn, KV_SNAPSHOT)
        snap = {"objects": {f"{o['source']}|{o['route']}|{o['origin']}": {"fp": o["fp"], "mnt": o["mnt"],
                                                                         "text": o["text"]} for o in rep["objects"]},
                # large unlisted holdings are summarised; don't track (or diff) thousands of objects
                "foreign": ({f"{o['source']}|{o['route']}": o["text"] for o in rep["foreign"]}
                            if len(rep["foreign"]) <= 200 else {}),
                "assets": {f"{a['source']}|{a['name']}": a["fp"] for a in rep["assets"]}}
        if prev:
            self._diff(prev, snap, now)
        db.kv_set(conn, KV_SNAPSHOT, snap)
        conn.commit()

    def _diff(self, prev, snap, now):
        eng, p0 = self.svc.engine, self.cfg.prefixes[0]
        for kind, label in (("objects", "route object"), ("foreign", "route object claiming your ASN")):
            a, b = prev.get(kind, {}), snap.get(kind, {})
            for k in b.keys() - a.keys():
                src, route, *origin = k.split("|")
                eng._new_event("irr_change", "info", p0, now,
                               f"{src}: {label} {route}{' origin ' + origin[0] if origin else ''} added",
                               {"change": "added", "object": b[k]["text"] if isinstance(b[k], dict) else b[k]},
                               end_ts=now)
            for k in a.keys() - b.keys():
                src, route, *origin = k.split("|")
                eng._new_event("irr_change", "info", p0, now,
                               f"{src}: {label} {route}{' origin ' + origin[0] if origin else ''} removed",
                               {"change": "removed", "object": a[k]["text"] if isinstance(a[k], dict) else a[k]},
                               end_ts=now)
            for k in a.keys() & b.keys():
                fa = a[k]["fp"] if isinstance(a[k], dict) else hashlib.sha1(a[k].encode()).hexdigest()
                fb = b[k]["fp"] if isinstance(b[k], dict) else hashlib.sha1(b[k].encode()).hexdigest()
                if fa != fb:
                    src, route, *origin = k.split("|")
                    eng._new_event("irr_change", "info", p0, now,
                                   f"{src}: {label} {route}{' origin ' + origin[0] if origin else ''} modified",
                                   {"change": "modified",
                                    "before": a[k]["text"] if isinstance(a[k], dict) else a[k],
                                    "object": b[k]["text"] if isinstance(b[k], dict) else b[k]}, end_ts=now)
        a, b = prev.get("assets", {}), snap.get("assets", {})
        for k in b.keys() - a.keys():
            src, name = k.split("|", 1)
            eng._new_event("asset_change", "info", p0, now, f"AS{self.cfg.asn} added to as-set {name} ({src})",
                           {"change": "added", "as_set": name, "source": src}, end_ts=now)
        for k in a.keys() - b.keys():
            src, name = k.split("|", 1)
            eng._new_event("asset_change", "info", p0, now, f"AS{self.cfg.asn} removed from as-set {name} ({src})",
                           {"change": "removed", "as_set": name, "source": src}, end_ts=now)

    def close_all(self):
        eng = self.svc.engine
        for key in [k for k in eng.anomalies if k.startswith("reg|")]:
            a = eng.anomalies.pop(key)
            eng.conn.execute("UPDATE events SET end_ts=? WHERE id=? AND end_ts IS NULL", (time.time(), a["id"]))
        eng.conn.commit()


# ---------------------------------------------------------------------- as-set details (Registry page links)
_ASSET_CACHE = {}   # (name, source) -> (ts, detail)


def _irrd_multi(commands):
    """Run several IRRd '!' commands in one session; returns the payload of each ('' for empty)."""
    raw = _whois("\n".join(commands), irrd=True)
    out, i = [], 0
    while i < len(raw):
        if raw[i] == "A":
            nl = raw.index("\n", i)
            n = int(raw[i + 1:nl])
            out.append(raw[nl + 1:nl + 1 + n])
            i = nl + 1 + n
        elif raw[i] in "CDF":
            if raw[i] in "DF":
                out.append("")
            nl = raw.find("\n", i)
            i = len(raw) if nl < 0 else nl + 1
        else:
            i += 1
    return out


def asset_detail(name, source, my_asn, upstreams=(), max_lookups=80):
    """Everything the Registry page shows for one as-set: the RPSL object (from `source`), its direct
    members, the full recursive expansion, and the chain of nested sets through which `my_asn` is included."""
    name, source = name.upper(), (source or "").upper()
    key = (name, source)
    hit = _ASSET_CACHE.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    src_cmd = [f"!s{source}"] if source else []
    text = _whois(f"-s {source} {name}" if source else name)
    objs = [o for o in _objects(text) if _first(o.get("as-set")).upper() == name]
    obj = objs[0] if objs else {}
    payloads = _irrd_multi(src_cmd + [f"!i{name}"])
    direct = payloads[-1].split() if payloads else []
    expanded = sorted({m for m in _irrd_data(_whois(f"!i{name},1", irrd=True)).split() if m.startswith("AS")
                       and m[2:].isdigit()}, key=lambda a: int(a[2:]))
    me = f"AS{my_asn}"
    # breadth-first search through nested sets for the chain that pulls in our ASN
    chain, seen, queue, lookups = None, {name}, [[name]], 0
    if me in direct:
        chain = [name, me]
    while queue and chain is None and lookups < max_lookups:
        path = queue.pop(0)
        members = direct if len(path) == 1 else _irrd_data(_whois(f"!i{path[-1]}", irrd=True)).split()
        lookups += 0 if len(path) == 1 else 1
        if me in members:
            chain = path + [me]
            break
        for m in members:
            if ":" in m or m.upper().startswith("AS-"):
                if m.upper() not in seen:
                    seen.add(m.upper())
                    queue.append(path + [m.upper()])
    detail = {
        "name": name, "source": source or _first(obj.get("source")),
        "descr": _first(obj.get("descr")), "mnt": _first(obj.get("mnt-by")),
        "modified": _first(obj.get("last-modified")), "created": _first(obj.get("created")),
        "text": obj.get("_text", text.strip()[:4000]),
        "direct": direct, "direct_sets": [m for m in direct if ":" in m or m.upper().startswith("AS-")],
        "expanded": expanded, "includes_me": me in expanded, "chain": chain,
        "upstreams_in": [f"AS{u}" for u in upstreams if f"AS{u}" in expanded],
        "fetched": time.time(),
    }
    _ASSET_CACHE[key] = (time.time(), detail)
    return detail
