"""Path finder: the known AS paths between your network and another one.

RIS doesn't show the paths between two networks directly. It shows each RIS peer's AS path toward your prefix and
toward theirs, and every network in such a path has a route of its own to that destination: the rest of the path.
The paths between the two networks are put together from those, the way traffic would go:
  * your traffic to them: up each chain of networks that carries your prefix (your upstream, its transit, ...) to
    the first one with a route of its own to their prefix, then along that route. Your upstreams' other neighbours
    (RIPEstat's asn-neighbours, as on the connectivity tree) count too: they can carry your traffic out even when
    they don't carry your prefix.
  * their traffic to you: their own route to your prefix, when RIS sees it (they're a RIS peer, or on the way to
    one). Otherwise the same climb from their side.
Networks pass traffic on the way they're paid to: up to a provider for anywhere, but across to a peer only for that
peer's own customers. So a climb goes on only through providers (your transits, and each network's main upstreams: in
front of it on at least a tenth of the paths toward it, counted once per vantage point's network), and a route met
across a peering is used only when it stays inside that peer's customer cone. Your exchange peers and listed peers
(peering.py) are peers. A path found both ways is listed once. Route Views' collectors can be asked too, for more
vantage points.

RIS rarely sees a peering (a peer's routes only reach its customers). So where PeeringDB has you, or one of your
transits, at an exchange with them, that direct link is added as a possible path; so is a peer you listed yourself.

Which path is in use: a traceroute from this server (tracer.py) for your traffic, and a RIPE Atlas traceroute from
a probe inside their network (or else their main upstream) for theirs. What each lookup and traceroute showed is
kept as the target's history (pf_hist), and watched targets are checked on a schedule (watch.py).
"""
import asyncio
import concurrent.futures
import ipaddress
import json
import logging
import re
import socket
import time
from collections import Counter, defaultdict

import httpx

from . import __version__, atlas, db, paths, ripe, routeviews, tracer

log = logging.getLogger("bgpcanary.pathfinder")
CLIMB = 5          # networks above either end to look through before giving up on a chain
MAIN_SHARE = 0.1   # a "main upstream" is in front of the network in at least this share of the paths toward it
ROUTES = 2         # routes kept per network: RIS peers can see different ones from it
ASN = re.compile(r"^(?:AS)?(\d{1,10})$", re.I)
PEERINGDB = "https://www.peeringdb.com/api"
IX_KV = "pathfinder_ix_prefixes"   # every exchange's peering LAN prefixes from PeeringDB: {"fetched", "prefixes"}
SETTINGS_KV = "pathfinder_settings"
CHECKS = {"all": "Everyone signed in", "admin": "Admins only", "off": "Nobody"}
TRACES_PER_MINUTE = 10
RV_KEEP_S = 1800   # a Route Views answer is reused this long (each costs one of the collector's daily connections)
BACK_PROBES = 3    # RIPE Atlas probes per trace back from them (~60 credits each)
SCHEMA = """
CREATE TABLE IF NOT EXISTS pf_watch (
    id INTEGER PRIMARY KEY, query TEXT NOT NULL UNIQUE, label TEXT, minutes INTEGER NOT NULL, alert INTEGER NOT NULL,
    back INTEGER NOT NULL DEFAULT 0, created REAL, created_by TEXT, last_check REAL, state TEXT);
CREATE TABLE IF NOT EXISTS pf_hist (
    id INTEGER PRIMARY KEY, target TEXT NOT NULL, kind TEXT NOT NULL, first REAL NOT NULL, last REAL NOT NULL,
    value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_pf_hist ON pf_hist(target, kind, id);
"""
_cache = {}        # RIPEstat answers: (call, params) -> (time, data)
_ix = {"at": 0, "nets": [], "ips": {}}   # those prefixes parsed; exchange hop address -> (time, its member)
_ixof = {}         # network -> (time, {ix_id: exchange name}) from PeeringDB
_rv = {}           # (collector, prefix) -> (time, [path strings])
_ptr = {}          # address -> (time, reverse DNS name or "")
_dns = concurrent.futures.ThreadPoolExecutor(4, thread_name_prefix="pathfinder-dns")   # (slow resolvers wait here)
_traces = {"lock": None, "recent": []}
_egress = {}       # this server's public address as RIPEstat sees it: {"ip", "at"}


class PathError(Exception):
    """A lookup or check that can't be done, with the HTTP status that says why."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------------------------- settings
def settings(conn):
    """{"checks": who may run traceroutes, RIPE Atlas traces and Route Views lookups ("all" | "admin" | "off")}."""
    s = {"checks": "all"}
    s.update(db.kv_get(conn, SETTINGS_KV, {}) or {})
    return s


def may_check(conn, role):
    c = settings(conn)["checks"]
    return c == "all" or c == "admin" and role == "admin"


def check_refusal(conn):
    """Why a check isn't allowed (after may_check said no)."""
    return ("An admin has turned off traceroutes, RIPE Atlas traces and Route Views lookups."
            if settings(conn)["checks"] == "off" else "Only admins can run traceroutes, RIPE Atlas traces and Route "
                                                      "Views lookups here.")


# ---------------------------------------------------------------------------------------------- what to look up
def parse_query(q):
    """'192.0.2.1' / '192.0.2.0/24' / 'AS64500' / '64500' / 'example.com' -> ("ip" | "prefix" | "asn" | "host",
    value); ValueError if it's none of those, or not a public address."""
    q = (q or "").strip()
    m = ASN.match(q)
    if m:
        n = int(m.group(1))
        if not 0 < n < 2 ** 32:
            raise ValueError(f"AS{n} isn't a valid AS number")
        return "asn", n
    try:
        net = ipaddress.ip_network(q, strict=False)
    except ValueError:
        if atlas.HOSTNAME.match(q):
            return "host", q.rstrip(".").lower()
        raise ValueError(f"'{q[:60]}' isn't an IP address, a prefix, an AS number or a host name") from None
    if not net.network_address.is_global or net.is_multicast:
        raise ValueError(f"{q} isn't a public address, so it has no route in BGP")
    if "/" in q and net.prefixlen < net.max_prefixlen:
        return "prefix", str(net)
    return "ip", str(net.network_address)


def key_of(q, prefix=None, origin=None):
    """The history key of a query: the query normalized, plus the prefix or origin picked for it."""
    kind, value = parse_query(q)
    k = f"AS{value}" if kind == "asn" else str(value)
    return k + (f" {prefix}" if prefix else "") + (f" AS{origin}" if origin else "")


async def _stat(call, sourceapp, ttl=600, **params):
    """ripe.stat, with answers kept for `ttl` seconds: the page asks again for each target and each reload."""
    key = (call, tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    d = await ripe.stat(call, sourceapp, **params)
    if len(_cache) >= 200:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
            del _cache[k]
    _cache[key] = (time.time(), d)
    return d


async def resolve_host(name, family=4):
    """A host name's public addresses (DNS), `family` first. ValueError if it has none."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(name, None, type=socket.SOCK_STREAM), 10)
    except (OSError, asyncio.TimeoutError):
        raise ValueError(f"{name} doesn't resolve") from None
    addrs = list(dict.fromkeys(i[4][0] for i in infos))
    good = [a for a in addrs if ipaddress.ip_address(a).is_global]
    if not good:   # (without them: the internal addresses aren't anyone's business here)
        raise ValueError(f"{name} resolves only to private addresses, which have no route in BGP")
    return sorted(good, key=lambda a: ipaddress.ip_address(a).version != family)


async def resolve(kind, value, sourceapp, family=4, prefix=None):
    """The network and prefix to find paths to: {"asn", "prefix", "ip", "origins": [every AS announcing the prefix],
    "prefixes": [for an AS: its prefixes, this address family and biggest first], "count", "host", "addrs"}. For an
    AS, `prefix` picks one of its prefixes (else its biggest of `family`). ValueError if RIS has no route to it."""
    if kind == "asn":
        d = await _stat("announced-prefixes", sourceapp, resource=f"AS{value}")
        latest = d.get("latest_time") or d.get("query_endtime")
        listed = d.get("prefixes") or []
        now = [p["prefix"] for p in listed if any(t.get("endtime") == latest for t in p.get("timelines") or [])]
        nets = sorted({ipaddress.ip_network(p) for p in now or [p["prefix"] for p in listed]},
                      key=lambda n: (n.version != family, n.prefixlen, n))
        if not nets:
            raise ValueError(f"AS{value} doesn't announce any prefix that RIS sees")
        names = [str(n) for n in nets]
        return {"asn": value, "prefix": prefix if prefix in names else names[0], "ip": None, "origins": [value],
                "prefixes": names[:300], "count": len(names)}
    if kind == "prefix":
        d = await _stat("prefix-overview", sourceapp, resource=value)
        asns = [int(a["asn"]) for a in d.get("asns") or [] if str(a.get("asn", "")).isdigit()]
        if d.get("announced") and asns and not d.get("is_less_specific"):
            return {"asn": asns[0], "prefix": d.get("resource") or value, "ip": None, "origins": asns}
        value = str(ipaddress.ip_network(value).network_address)   # not announced itself: the prefix covering it
    host = addrs = None
    if kind == "host":
        host, addrs = value, await resolve_host(value, family)
        value = addrs[0]
    d = await _stat("network-info", sourceapp, resource=value)
    asns = [int(a) for a in d.get("asns") or [] if str(a).isdigit()]
    if not asns or not d.get("prefix"):
        raise ValueError(f"{value} isn't in any prefix announced in BGP right now")
    return {"asn": asns[0], "prefix": d["prefix"], "ip": value if kind in ("ip", "host") else None, "origins": asns,
            "host": host, "addrs": addrs}


def trace_target(t):
    """Where a traceroute for this target goes: its address, else an address in its prefix (the first host: it may
    not answer, but the routers on the way to their network do)."""
    if t.get("ip"):
        return t["ip"]
    n = ipaddress.ip_network(t["prefix"])
    return str(n.network_address + 1 if n.num_addresses > 2 else n.network_address)


async def target_paths(prefix, origin, sourceapp):
    """RIS peers' AS paths toward `prefix` that end at `origin`, from RIPEstat's looking glass: (paths, data time)."""
    d = await _stat("looking-glass", sourceapp, ttl=120, resource=prefix)
    out = []
    for rrc in d.get("rrcs") or []:
        for p in rrc.get("peers") or []:
            if p.get("prefix") and p["prefix"] != prefix:
                continue
            a = paths.parse_str(p.get("as_path") or "")
            if a and a[-1] == origin:
                out.append(a)
    return out, d.get("latest_time")


async def rv_paths(svc, prefix):
    """Route Views collectors' AS paths toward `prefix` ("show ip bgp" on each, at once), within the path finder's
    share of each collector's daily budget and kept for RV_KEEP_S: ([paths], [{"server", "paths", "error"}])."""
    async def one(srv):
        hit = _rv.get((srv, prefix))
        if hit and time.time() - hit[0] < RV_KEEP_S:
            return {"server": srv, "paths": hit[1], "error": None}
        ok, why = svc.rv_allowed(srv, "pathfinder")
        if not ok:
            return {"server": srv, "paths": [], "error": why}
        svc._rv_begin(srv)
        try:
            _raw, plist = await asyncio.wait_for(routeviews.show_bgp(srv, prefix), 120)
        except Exception as ex:
            svc._rv_failed(srv, ex)
            return {"server": srv, "paths": [], "error": str(ex) or type(ex).__name__}
        _rv[(srv, prefix)] = (time.time(), plist)
        return {"server": srv, "paths": plist, "error": None}
    res = list(await asyncio.gather(*(one(s) for s in svc.cfg.rv_servers)))
    return [paths.parse_str(p) for r in res for p in r["paths"]], [
        {"server": r["server"], "paths": len(r["paths"]), "error": r["error"]} for r in res]


def my_paths(svc, family, routeviews_too=False):
    """Every RIS peer's current AS path toward your prefixes of one address family (all of your prefixes if you have
    none in it): (paths, those prefixes, how many came from Route Views' latest snapshots, when asked for)."""
    cfg = svc.cfg
    want = [p for p in cfg.prefixes if ipaddress.ip_network(p).version == family] or list(cfg.prefixes)
    out = [e.path for p in want for e in list(svc.engine.rib.get(p, {}).values()) if e.path]
    rv = []
    if routeviews_too and want:
        q = ",".join("?" * len(want))
        for r in svc.aconn.execute(
                f"SELECT paths FROM rv_snapshots WHERE id IN (SELECT MAX(id) FROM rv_snapshots WHERE prefix IN ({q}) "
                "AND (error IS NULL OR error='') GROUP BY server, prefix)", want):
            rv += [paths.parse_str(p) for p in json.loads(r["paths"] or "[]")]
    return out + rv, want, len(rv)


def my_neighbours(cfg, mine):
    """The networks next to yours: configured upstreams, and every AS seen right in front of yours."""
    return set(cfg.expected_upstreams) | {a[-2] for a in mine if len(a) >= 2 and a[-1] == cfg.asn}


# ---------------------------------------------------------------------------------------------- the paths
def _clean(ps, origin):
    """Paths that end at `origin`, without loops (prepends are already folded)."""
    return [list(a) for a in ps if a and a[-1] == origin and len(set(a)) == len(a)]


def _routes(ps):
    """Each network's own routes to the destination, as RIS peers see them: AS -> Counter of the rest of the path."""
    m = defaultdict(Counter)
    for a in ps:
        for i, x in enumerate(a):
            m[x][tuple(a[i:])] += 1
    return m


def _fronts(ps):
    """Who is right in front of each network on these paths: AS -> Counter of the network before it, counted once
    per vantage point's network (some networks feed RIS from a dozen sessions, which would make them look like
    everyone's upstream), and not counting a network's own view of its own link (that says nothing about it being
    anyone else's way there)."""
    seen = defaultdict(set)
    for a in ps:
        for i in range(1, len(a)):
            if a[0] != a[i - 1]:
                seen[(a[i], a[i - 1])].add(a[0])
    m = defaultdict(Counter)
    for (b, x), vantage in seen.items():
        m[b][x] = len(vantage)
    return m


def _main(fronts, b, a):
    """Whether `a` is one of `b`'s main upstreams: right in front of it on at least MAIN_SHARE of the paths through
    it. A provider is, on most of them; a peer only on its own customers' paths, so it isn't."""
    f = fronts.get(b)
    return bool(f) and f[a] >= MAIN_SHARE * sum(f.values())


def _downhill(route, fronts, into=None):
    """Whether a network's route stays inside its customer cone, each network on it a main upstream of the next
    (`fronts`: who is in front of whom toward that destination; `into(x)`: whether x is a provider of the
    destination itself, where that's known better than `fronts` can tell: your own transits and peers). That's all
    a network gives its peers: a peer carries your traffic only to its own customers, never on to its providers or
    its other peers."""
    last = len(route) - 2
    return all(into(a) if into and i == last else _main(fronts, b, a)
               for i, (a, b) in enumerate(zip(route, route[1:])))


def _climb(ps, routes, fronts, other, first_up=None, into=None):
    """Up each path from its destination end to the first network with a route of its own toward the other end,
    then along that route: Counter of the resulting paths (destination end first). Traffic goes up to a provider
    for anything, but across to a peer only for that peer's customer cone. So the climb goes on only through main
    upstreams (`first_up(x)` decides the first step; `fronts`: who is in front of whom on `ps`), and a route met
    across any other link has to be downhill (`other`: who is in front of whom toward the other end; `into`: see
    _downhill)."""
    out = Counter()
    for a in ps:
        up = a[::-1]
        for k, x in enumerate(up[:CLIMB + 1]):
            rise = k == 0 or (first_up(x) if k == 1 and first_up else _main(fronts, up[k - 1], x))
            if x in routes:
                for r, _ in routes[x].most_common(ROUTES):
                    p = tuple(up[:k]) + r
                    if len(set(p)) == len(p) and (rise or _downhill(r, other, into)):
                        out[p] += 1
                break
            if not rise:
                break   # (a peer's providers aren't yours)
    return out


def connect(me, mine, them, theirs, neighbours=None, peers=(), transits=()):
    """The known AS paths between `me` and `them`, each written from you to them, shortest first:
    {"paths": [{"asns": [me, ..., them], "dirs": ["in", "out"], "seen": n, "own": bool, "nbr": bool}],
     "inbound": "own" | "climb" | "none", "main": [their main upstreams]}.
    `mine` / `theirs`: RIS peers' AS paths toward your prefixes / toward theirs (vantage point first).
    `peers`: your exchange and listed peers; `transits`: your configured upstreams. Without either, a neighbour of
    yours counts as a way out when it's one of your main upstreams.
    `neighbours`: {upstream: [(its neighbour, RIPEstat's power)]}, for more ways out of your upstreams.
    "dirs": "out" = your traffic to them, "in" = their traffic to you. "seen": how many RIS paths point that way.
    "own": their own route to you, as a RIS peer sees it. "nbr": through a neighbour of your upstream that RIS
    doesn't see carrying your prefix (from RIPEstat's neighbour list).
    Paths follow how networks pass traffic on (see _climb): a peer of yours, or of your upstream's, carries it only
    to its own customers. "inbound": "own" = their own route to you; "climb" = up from their side the same way."""
    mine, theirs = _clean(mine, me), _clean(theirs, them)
    to_me, to_them = _routes(mine), _routes(theirs)
    fr_me, fr_them = _fronts(mine), _fronts(theirs)
    found = {}

    def add(p, d, n, own=False, nbr=False):
        e = found.setdefault(tuple(p), {"asns": list(p), "dirs": set(), "seen": 0, "own": False, "nbr": nbr})
        e["dirs"].add(d)
        e["seen"] += n
        e["own"] = e["own"] or own
        e["nbr"] = e["nbr"] and nbr

    def way_out(x):   # (one of your neighbours that carries your traffic anywhere: one of your transits)
        return x in transits or x not in peers and _main(fr_me, me, x)

    for p, n in _climb(mine, to_them, fr_me, fr_them, way_out).items():
        add(p, "out", n)
    for u, nbrs in (neighbours or {}).items():
        if not way_out(u):
            continue
        # an upstream's other neighbours: a way out, if they have a route of their own toward them. One of its
        # main upstreams (by RIPEstat's power) for any route; any other only within its customer cone.
        total = sum(pw for _, pw in nbrs) or 1
        for x, head, rise in [(u, (me,), True)] + [(v, (me, u), pw >= MAIN_SHARE * total) for v, pw in nbrs]:
            if x == me or x not in to_them or any(k[:len(head) + 1] == head + (x,) for k in found):
                continue
            for r, _ in to_them[x].most_common(1):
                p = head + r
                if len(set(p)) == len(p) and (rise or _downhill(r, fr_them)):
                    add(p, "out", 0, nbr=True)
    inbound = "none"
    main = sorted(x for x, _ in fr_them.get(them, Counter()).most_common(6) if _main(fr_them, them, x))
    if them in to_me:   # their own route to you
        inbound = "own"
        for r, n in to_me[them].most_common(ROUTES + 1):
            add(r[::-1], "in", n, own=True)
    elif theirs:   # (a peer of yours carries their traffic to you only when they're in its customer cone)
        got = _climb(theirs, to_me, fr_them, fr_me, into=way_out)
        for p, n in got.items():
            add(p[::-1], "in", n)
        inbound = "climb" if got else "none"
    out = sorted(found.values(), key=lambda e: (len(e["asns"]), -e["seen"], "out" not in e["dirs"], e["asns"]))
    for e in out:
        e["dirs"] = sorted(e["dirs"])
    return {"paths": out, "inbound": inbound, "main": main}


def possible_paths(me, them, transits, ixes, known, my_exchanges=()):
    """Direct links RIS can't see, but PeeringDB says could be there: you and them at the same exchange (from your
    exchange list: `my_exchanges` [{"name", "members"}]), or one of your `transits` and them (`ixes`: {asn: {ix_id:
    name}}). Peering routes are only passed on to customers, so RIS rarely sees them. A traceroute shows whether
    they're in use. [{"asns", "dirs", "seen": 0, "possible": [exchange names]}], not among the `known` paths."""
    have = {tuple(e["asns"]) for e in known}
    out = []
    shared = sorted(x["name"] for x in my_exchanges if them in (x.get("members") or ()))
    if shared and (me, them) not in have:
        out.append({"asns": [me, them], "possible": shared})
    for u in transits:
        shared = sorted(set(ixes.get(u, {}).values()) & set(ixes.get(them, {}).values()))
        if shared and u not in (me, them) and (me, u, them) not in have:
            out.append({"asns": [me, u, them], "possible": shared})
    for e in out:
        e.update(dirs=["in", "out"], seen=0, own=False, nbr=False)
    return out


async def exchanges_of(asns):
    """The exchanges each network is at, from PeeringDB: {asn: {ix_id: name}}, kept for a day."""
    now, asns = time.time(), list(dict.fromkeys(asns))
    todo = [a for a in asns if a not in _ixof or now - _ixof[a][0] > 86400]
    if todo:
        try:
            async with httpx.AsyncClient(timeout=30, headers={"User-Agent": f"bgpcanary/{__version__}"}) as c:
                r = await c.get(f"{PEERINGDB}/netixlan", params={"asn__in": ",".join(map(str, todo)),
                                                                 "fields": "asn,ix_id,name"})
                r.raise_for_status()
                got = defaultdict(dict)
                for x in r.json().get("data", []):   # ("Metro-IX North: Metro-IX North Peering LAN")
                    got[x["asn"]][x["ix_id"]] = (x.get("name") or f"IX {x['ix_id']}").split(":")[0].strip()
            if len(_ixof) > 1000:
                _ixof.clear()
            for a in todo:
                _ixof[a] = (now, dict(got.get(a, {})))
        except Exception as ex:
            log.info("PeeringDB exchanges of %s: %s", todo, ex)
    return {a: _ixof[a][1] for a in asns if a in _ixof}


async def lookup(svc, q, prefix=None, origin=0, routeviews_too=False):
    """Everything the page shows about the paths between you and `q` (an IP address, prefix, AS number or host name);
    PathError when it can't be looked up."""
    cfg = svc.cfg
    try:
        kind, value = parse_query(q)
        fam = 4 if any(ipaddress.ip_network(p).version == 4 for p in cfg.prefixes) else 6
        t = await resolve(kind, value, svc.sourceapp, family=fam, prefix=prefix or None)
    except ValueError as ex:
        raise PathError(str(ex)) from None
    except Exception as ex:
        log.warning("path finder: looking up %r: %s", str(q)[:100], ex)
        raise PathError("RIPEstat didn't answer. Try again in a minute.", 502) from None
    if origin in t["origins"]:
        t["asn"] = origin
    them = t["asn"]
    if them == cfg.asn:
        raise PathError(f"That's your own network (AS{cfg.asn}).")
    lg_task = asyncio.ensure_future(target_paths(t["prefix"], them, svc.sourceapp))
    rv, rv_servers = ([], [])
    if routeviews_too and cfg.rv_servers:
        try:
            rv, rv_servers = await rv_paths(svc, t["prefix"])
        except Exception:   # (each collector's own failures are already in its answer)
            log.exception("path finder: Route Views for %s", t["prefix"])
    try:
        theirs, when = await lg_task
    except Exception as ex:
        log.warning("path finder: looking glass for %s: %s", t["prefix"], ex)
        raise PathError("RIPEstat's looking glass didn't answer. Try again in a minute.", 502) from None
    n_ris = len(theirs)
    theirs += [a for a in rv if a and a[-1] == them]
    fam = ipaddress.ip_network(t["prefix"]).version
    mine, mine_pfx, mine_rv = my_paths(svc, fam, routeviews_too)
    nbrs = my_neighbours(cfg, mine)
    peers = {a for a in nbrs if svc.engine.peer_group(a)}   # (your exchanges' members and listed peers)
    ups = sorted(nbrs - peers)   # (exchange peers aren't looked up, as on the tree)
    from . import tree
    upn = await tree.upstream_neighbors(svc.sourceapp, ups)
    res = connect(cfg.asn, mine, them, theirs, {u: list(upn.get(u, [])) for u in ups}, peers,
                  set(cfg.expected_upstreams))
    if them in cfg.peers and not any(e["asns"] == [cfg.asn, them] for e in res["paths"]):
        # a peer listed under Settings › Monitored network: a direct link RIS may well not see
        res["paths"].insert(0, {"asns": [cfg.asn, them], "dirs": ["in", "out"], "seen": 0, "own": False, "nbr": False,
                                "listed": True})
    # direct links RIS can't see but PeeringDB makes possible (you or one of your transits at their exchange)
    fr = _fronts(_clean(mine, cfg.asn))
    outs = [u for u in ups if u in cfg.expected_upstreams or _main(fr, cfg.asn, u)]
    ixes = await exchanges_of(outs + [them]) if outs else {}
    res["paths"] += possible_paths(cfg.asn, them, outs, ixes, res["paths"], svc.peering.exchanges())
    notes = []
    st = svc.engine.status.get("phase") or ""
    if st in ("starting", "connecting to RIS Live", "finishing the history load") or svc.engine.status.get("history"):
        notes.append("bgpcanary is still starting up, so your side of these paths may be incomplete.")
    if not mine:
        notes.append("RIS has no route to your prefixes right now, so there's nothing to build your side from.")
    elif ipaddress.ip_network(mine_pfx[0]).version != fam:
        notes.append(f"You have no IPv{fam} prefix, so your side comes from your IPv{6 if fam == 4 else 4} paths.")
    if not theirs:
        notes.append(f"No RIS peer has a route to {t['prefix']} from AS{them} right now.")
    nm = await svc.as_names({cfg.asn, them, *t["origins"], *(a for e in res["paths"] for a in e["asns"])}, limit=60)
    # (part of the history key: a prefix or origin picked instead of the one used by default)
    picked = prefix if prefix and prefix == t["prefix"] and prefix != (t.get("prefixes") or [None])[0] else None
    return {"me": cfg.asn, "target": t, "kind": kind, "key": key_of(q, picked, them if them != t["origins"][0] else None),
            "my_prefixes": mine_pfx, "mine": len(mine), "mine_rv": mine_rv, "theirs": len(theirs),
            "theirs_rv": len(theirs) - n_ris, "rv_servers": rv_servers, "lg_time": when, **res, "notes": notes,
            "names": {str(k): v for k, v in nm.items()}, "trace_to": trace_target(t), "can_trace": tracer.supported()}


# ---------------------------------------------------------------------------------------------- traceroutes
def trace_asns(hops, asn_of):
    """The networks a traceroute went through, in order, and where it went dark: (networks, gaps), gaps[i] = how many
    hops between networks i and i+1 showed no network of their own (silent, private or unannounced addresses), so
    up to that many networks the traceroute didn't see may be there. A network is listed once: a router answering
    from an address of its neighbour's (a link numbered from the neighbour's space) would otherwise make A B look like
    A B A; such a hop counts as dark."""
    seq, gaps, dark = [], [], 0
    for h in hops:
        a = asn_of.get(h.get("ip"))
        if a and a not in seq:
            if seq:
                gaps.append(dark)
            seq.append(a)
            dark = 0
        elif a and a == seq[-1]:
            dark = 0   # (dark hops inside one network hide nothing between networks)
        else:
            dark += 1
    return seq, gaps


def follows(path, seq, gaps):
    """How far `path` goes along a traceroute's networks: they're in it in order, from its start, with others in
    between only where the traceroute's hops went dark, at most one per dark hop (gaps). 0 if it doesn't."""
    j = 0
    for i, a in enumerate(seq):
        try:
            k = path.index(a, j)
        except ValueError:
            return 0
        if (k != 0) if i == 0 else k - j > (gaps[i - 1] if i - 1 < len(gaps) else 0):
            return 0
        j = k + 1
    return j


def match_trace(known, seq, gaps, them):
    """Which known path (dicts with "asns" and "seen", as connect() gives them) a traceroute's networks (from yours)
    follow: {"asns", "how": "exact" | "gaps" (some of its networks were hidden behind silent hops) | "part" (it
    stopped before them, following that path so far) | "new" (not a known path), "upto": how many of the path's
    networks it covered, "hidden": networks on it the traceroute didn't see}. None for less than two networks."""
    if len(seq) < 2:
        return None
    for e in known:
        if e["asns"] == seq:
            return {"asns": seq, "how": "exact", "upto": len(seq), "hidden": []}
    reached, best, upto = seq[-1] == them, None, 0
    for e in known:
        n = follows(e["asns"], seq, gaps)
        if n and (n == len(e["asns"]) if reached else n < len(e["asns"])) and (best is None or e["seen"] > best["seen"]):
            best, upto = e, n
    if best:
        return {"asns": best["asns"], "how": "gaps" if reached else "part", "upto": upto,
                "hidden": [a for a in best["asns"][:upto] if a not in seq]}
    return {"asns": seq, "how": "new", "upto": len(seq), "hidden": []}


async def _ix_nets(conn):
    """Exchange peering LAN prefixes (PeeringDB), fetched once a day and kept in the database."""
    now = time.time()
    if now - _ix["at"] < 3600:
        return _ix["nets"]
    d = db.kv_get(conn, IX_KV, {}) or {}
    if now - d.get("fetched", 0) > 86400:
        try:
            async with httpx.AsyncClient(timeout=60, headers={"User-Agent": f"bgpcanary/{__version__}"}) as c:
                r = await c.get(f"{PEERINGDB}/ixpfx", params={"fields": "prefix"})
                r.raise_for_status()
                d = {"fetched": now, "prefixes": [x["prefix"] for x in r.json().get("data", []) if x.get("prefix")]}
            db.kv_set(conn, IX_KV, d)
            conn.commit()
        except Exception as ex:
            log.warning("PeeringDB exchange prefixes: %s (hops at exchanges keep their IP-to-ASN answer)", ex)
    nets = []
    for p in d.get("prefixes", []):
        try:
            nets.append(ipaddress.ip_network(p, strict=False))
        except ValueError:
            pass
    _ix.update(at=now, nets=nets)
    return nets


async def exchange_hops(conn, ips):
    """Traceroute hops on an internet exchange's peering LAN: address -> {"ix": the exchange's name, "asn": the
    member whose router answered from it}, both from PeeringDB (either may be missing). Some network or other often
    announces an exchange's LAN, so an IP-to-ASN lookup would put the wrong network on the path."""
    nets, out = await _ix_nets(conn), {}
    hits = []
    for ip in dict.fromkeys(i for i in ips if i):
        a = ipaddress.ip_address(ip)
        if any(a in n for n in nets):
            hits.append(ip)
    if not hits:
        return out
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": f"bgpcanary/{__version__}"}) as c:
        for ip in hits:
            hit = _ix["ips"].get(ip)
            if not hit or time.time() - hit[0] > 86400:
                info = {"ix": "", "asn": None}
                try:
                    r = await c.get(f"{PEERINGDB}/netixlan", params={"ipaddr6" if ":" in ip else "ipaddr4": ip,
                                                                     "fields": "asn,name"})
                    r.raise_for_status()
                    rows = r.json().get("data") or []
                    if rows:   # "Metro-IX North: Metro-IX North Peering LAN" -> "Metro-IX North"
                        info = {"ix": (rows[0].get("name") or "").split(":")[0].strip(), "asn": int(rows[0]["asn"])}
                except Exception as ex:
                    log.info("PeeringDB member at %s: %s", ip, ex)
                hit = (time.time(), info)
                if len(_ix["ips"]) > 500:
                    _ix["ips"].clear()
                _ix["ips"][ip] = hit
            out[ip] = hit[1]
    return out


async def ptr_names(ips, timeout=4.0):
    """Reverse DNS names of addresses (routers' names often carry their city), kept for a day; whatever hasn't
    answered within `timeout` is left out (and filled in for next time)."""
    loop, now = asyncio.get_running_loop(), time.time()

    async def one(ip):
        try:
            name = (await loop.run_in_executor(_dns, socket.gethostbyaddr, ip))[0]
        except Exception:   # no PTR record, or the resolver gave up
            name = ""
        if len(_ptr) > 5000:
            _ptr.clear()
        _ptr[ip] = (time.time(), name)
    todo = [ip for ip in dict.fromkeys(i for i in ips if i) if ip not in _ptr or now - _ptr[ip][0] > 86400]
    if todo:
        await asyncio.wait([asyncio.ensure_future(one(ip)) for ip in todo], timeout=timeout)
    return {ip: _ptr[ip][1] for ip in ips if ip in _ptr and _ptr[ip][1]}


async def egress_ip(svc):
    """This server's address on the internet (behind NAT, the router's), from RIPEstat; None if it didn't answer."""
    if time.time() - _egress.get("at", 0) > 600:
        try:
            _egress.update(ip=(await ripe.stat("whats-my-ip", svc.sourceapp)).get("ip"), at=time.time())
        except Exception as ex:
            log.info("whats-my-ip: %s", ex)
            return None
    return _egress.get("ip")


async def hop_asns(svc, ips):
    """Hop address -> ASN: Team Cymru's whois, cached for a week (shared with the RIPE Atlas pages), or one RIPEstat
    lookup per address where that's blocked."""
    try:
        return {ip: asn for ip, (asn, _) in (await svc.atlas.ip_asns(ips)).items() if asn}
    except Exception as ex:
        log.info("whois.cymru.com didn't answer (%s); asking RIPEstat", ex)
    out = {}

    async def one(ip):
        try:
            d = await ripe.stat("network-info", svc.sourceapp, resource=ip)
            if d.get("asns"):
                out[ip] = int(d["asns"][0])
        except Exception:
            pass
    await asyncio.gather(*(one(ip) for ip in dict.fromkeys(ips) if atlas._public(ip)))
    return out


async def label_hops(svc, hops, extra=()):
    """Each hop's network (exchange LANs: the member whose port it is, from PeeringDB), exchange and reverse DNS
    name, in place; returns {address: ASN} for the hops and `extra` addresses."""
    ips = [h["ip"] for h in hops if h.get("ip")]
    asn_of, ix, names = await asyncio.gather(hop_asns(svc, ips + [x for x in extra if x]),
                                             exchange_hops(svc.aconn, ips), ptr_names(ips))
    for ip, x in ix.items():
        if x["asn"]:
            asn_of[ip] = x["asn"]
        else:
            asn_of.pop(ip, None)
    for h in hops:
        h["asn"] = asn_of.get(h.get("ip"))
        if h.get("ip") in ix:
            h["ix"] = ix[h["ip"]]["ix"] or "an exchange"
        if h.get("ip") in names:
            h["name"] = names[h["ip"]]
        if h.get("ip") and not atlas._public(h["ip"]):
            h["private"] = True
    return asn_of


async def trace(svc, ip, known=(), them=None, limit=True):
    """Traceroute from this server to `ip`: each hop with its network, exchange and name, the networks it went
    through in order ("asns", "gaps"), whether it shows your traffic's path ("from_you": this server's address is in
    your prefixes, or it leaves through your own network or one of your upstreams; your AS is put first when your
    routers answered from other addresses), and which of the `known` paths it follows ("match"). `limit`: count it
    against TRACES_PER_MINUTE (a person asking; scheduled checks just wait their turn). PathError if it can't run."""
    cfg = svc.cfg
    if not tracer.supported():
        raise PathError("Traceroute needs bgpcanary to run on Linux.", 501)
    try:
        a = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        raise PathError("That isn't an IP address.") from None
    if not a.is_global or a.is_multicast:
        raise PathError(f"{a} isn't a public address.")
    now = time.time()
    if limit:
        _traces["recent"] = [x for x in _traces["recent"] if now - x < 60]
        if len(_traces["recent"]) >= TRACES_PER_MINUTE:
            raise PathError("Too many traceroutes in the last minute. Try again shortly.", 429)
        _traces["recent"].append(now)
    if _traces["lock"] is None:
        _traces["lock"] = asyncio.Lock()
    async with _traces["lock"]:   # one at a time
        try:
            res = await asyncio.to_thread(tracer.run, str(a))
        except OSError as ex:
            raise PathError(f"The traceroute couldn't start: {ex.strerror or ex}.", 502) from None
    egress = await egress_ip(svc)
    asn_of = await label_hops(svc, res["hops"], [egress])
    seq, gaps = trace_asns(res["hops"], asn_of)
    mine, _, _ = my_paths(svc, a.version)
    nbrs = my_neighbours(cfg, mine)
    try:
        own = bool(egress) and any(ipaddress.ip_address(egress) in ipaddress.ip_network(p) for p in cfg.prefixes)
    except ValueError:
        own = False
    own = own or (bool(seq) and seq[0] == cfg.asn)
    # this server's address is one of your upstream's (your router's link to it, behind NAT): the trace still
    # shows how traffic leaves through that upstream, though your own prefixes' traffic may be routed otherwise
    via_up = not own and bool(seq) and seq[0] in nbrs
    added = bool(seq) and (own or via_up) and seq[0] != cfg.asn
    if added:   # (between you and a neighbour, nothing's hidden; else the dark hops before it may hide some)
        lead = next((i for i, h in enumerate(res["hops"]) if h["asn"]), 0)
        seq, gaps = [cfg.asn] + seq, [0 if seq[0] in nbrs else max(lead, 1)] + gaps
    elif len(seq) > 1 and seq[0] == cfg.asn and seq[1] not in nbrs:
        # the network next to yours has to be one of your neighbours: when it isn't, one is hidden (its routers
        # answered from the next network's addresses, as with a link numbered from your upstream's provider)
        gaps[0] = max(gaps[0], 1)
    from_you = bool(seq) and (own or via_up)
    out_asn = asn_of.get(egress) if egress else None
    nm = await svc.as_names(seq + ([out_asn] if out_asn else []))
    res.update(target=str(a), asns=seq, gaps=gaps, from_you=from_you, own_space=own, added_you=added, egress=egress,
               egress_asn=out_asn, names={str(k): v for k, v in nm.items()},
               match=match_trace(list(known), seq, gaps, them) if from_you and not res["direct"] else None)
    return res


def active_value(tr):
    """What a traceroute says about the path in use, for the history: the known path it follows (or its own),
    complete or only so far."""
    m = tr.get("match")
    if not m:
        return None
    return {"path": m["asns"][:m["upto"]], "complete": m["how"] != "part"}


# ---------------------------------------------------------------------------------------------- their way back
async def back_start(svc, them, main=(), user=""):
    """Start a one-off RIPE Atlas traceroute to your address from probes inside their network, or else inside their
    main upstreams (the path back from there is most of theirs): {"msm", "probes": [ids], "from_asn", "inside",
    "target", "credits"}. PathError if Atlas isn't set up, has no probe in any of them, or the credit cap is near."""
    if not atlas.enabled(svc.aconn):
        raise PathError("Their way back to you comes from RIPE Atlas, which isn't set up (Settings › RIPE Atlas).",
                          409)
    target = svc.atlas.target()
    if not target:
        raise PathError("Set a probe target inside your space first (Settings › Monitored network).", 409)
    v6 = ":" in target
    probes, from_asn = [], None
    for asn in [them] + [m for m in main if m != them]:
        try:
            ps = await svc.atlas._connected_probes(**{"asn_v6" if v6 else "asn_v4": asn})
        except Exception as ex:
            raise PathError(f"RIPE Atlas didn't answer: {ex}", 502) from None
        if ps:
            probes, from_asn = ps[:BACK_PROBES], asn
            break
    if not probes:
        raise PathError(f"RIPE Atlas has no connected probe in AS{them}" + (
            f" or its main upstream{'s' if len(main) > 1 else ''} ({', '.join('AS%s' % m for m in main)})" if main else "")
            + ", so their way back to you can't be traced.", 404)
    svc.atlas._store_probes(probes)
    ids = [p["id"] for p in probes]
    try:
        msm = await svc.atlas.create("traceroute", target, ids, "pathfinder",
                                     description=f"bgpcanary path finder: from AS{from_asn} back to you ({user})")
    except Exception as ex:
        raise PathError(f"RIPE Atlas refused the measurement: {ex}", 502) from None
    return {"msm": msm, "probes": ids, "from_asn": from_asn, "inside": from_asn == them, "target": target,
            "credits": atlas.COST["traceroute"] * 2 * len(ids)}


async def back_result(svc, msm, them, known=()):
    """What a back_start measurement has shown so far: {"done", "results": [{"probe", "asn", "cc", "ts", "reached",
    "hops", "asns" (written from you, as the paths are), "gaps", "match", "from_you"}], "names"}."""
    m = svc.aconn.execute("SELECT * FROM atlas_msm WHERE id=? AND purpose='pathfinder'", (msm,)).fetchone()
    if not m:
        raise PathError("No such measurement.", 404)
    if time.time() - (m["last_fetch"] or 0) > 15:   # (Atlas has them before the 5-minute poll does)
        try:
            await svc.atlas.fetch(msm, "traceroute", m["created"] - 60)
        except Exception as ex:
            log.info("Atlas results for %s: %s", msm, ex)
        svc.aconn.execute("UPDATE atlas_msm SET last_fetch=? WHERE id=?", (time.time(), msm))
        svc.aconn.commit()
    rows = svc.aconn.execute("SELECT r.*, p.asn AS probe_asn, p.cc FROM atlas_result r LEFT JOIN atlas_probe p ON "
                             "p.prb_id=r.prb_id WHERE r.msm_id=? ORDER BY r.ts", (msm,)).fetchall()
    cfg, out, asns = svc.cfg, [], set()
    mine, _, _ = my_paths(svc, 6 if ":" in (m["target"] or "") else 4)
    nbrs = my_neighbours(cfg, mine)
    for r in rows:
        hops = [{"ttl": h.get("h"), "ip": h.get("ip"), "rtt": h.get("rtt")} for h in json.loads(r["hops"] or "[]")]
        asn_of = await label_hops(svc, hops)
        seq, gaps = trace_asns(hops, asn_of)   # from their side to you
        pa = r["probe_asn"]
        if pa and seq and pa not in seq:   # (it starts in the probe's own network, whose routers didn't show)
            seq, gaps = [pa] + seq, [next((i for i, h in enumerate(hops) if h.get("asn")), 0)] + gaps
        if seq and seq[-1] != cfg.asn and seq[-1] in nbrs:
            seq, gaps = seq + [cfg.asn], gaps + [0]   # (got as far as your upstream: your routers just didn't answer)
        elif len(seq) > 1 and seq[-1] == cfg.asn and seq[-2] not in nbrs:
            gaps[-1] = max(gaps[-1], 1)   # (a neighbour of yours hidden there, as in trace())
        rev, rgaps = seq[::-1], gaps[::-1]
        ok = bool(rev) and rev[0] == cfg.asn
        asns.update(rev)
        out.append({"probe": r["prb_id"], "asn": r["probe_asn"], "cc": r["cc"], "ts": r["ts"],
                    "reached": bool(r["reached"]), "hops": hops, "asns": rev, "gaps": rgaps, "from_you": ok,
                    "match": match_trace(list(known), rev, rgaps, them) if ok else None})
    nm = await svc.as_names(asns)
    return {"done": len(out) >= (m["probes"] or 0) or time.time() - m["created"] > 900, "results": out,
            "names": {str(k): v for k, v in nm.items()}}


def back_value(results):
    """What the traces back say about their path to you, for the history: the path (from you) of the first probe
    that got through, complete or as far as it went."""
    for r in results:
        if r.get("match"):
            m = r["match"]
            return {"path": m["asns"][:m["upto"]], "complete": m["how"] != "part", "probe_asn": r["asn"]}
    return None


# ---------------------------------------------------------------------------------------------- history
def record(conn, target, kind, value, ts=None):
    """Keep what was seen for `target`: the "known" paths, the "active" path from a traceroute, the path "back". A new
    row when it differs from the last one; otherwise the last row's time moves on. Returns True if it changed."""
    if value is None:
        return False
    ts = ts or time.time()
    v = json.dumps(value, sort_keys=True, separators=(",", ":"))
    row = conn.execute("SELECT id, value FROM pf_hist WHERE target=? AND kind=? ORDER BY id DESC LIMIT 1",
                       (target, kind)).fetchone()
    if row and row["value"] == v:
        conn.execute("UPDATE pf_hist SET last=? WHERE id=?", (ts, row["id"]))
    else:
        conn.execute("INSERT INTO pf_hist(target,kind,first,last,value) VALUES(?,?,?,?,?)", (target, kind, ts, ts, v))
    conn.commit()
    return not row or row["value"] != v


def known_value(res):
    """The known paths, for the history (not the possible peerings: those come from PeeringDB, not from routes)."""
    known = [e for e in res["paths"] if not e.get("possible")]
    return {"paths": sorted(" ".join(map(str, e["asns"])) for e in known),
            "shortest": " ".join(map(str, known[0]["asns"])) if known else None}


def history(conn, target, limit=60):
    """What was seen for `target`, newest first: [{"kind", "first", "last", "value"}]."""
    return [{"kind": r["kind"], "first": r["first"], "last": r["last"], "value": json.loads(r["value"])}
            for r in conn.execute("SELECT * FROM pf_hist WHERE target=? ORDER BY last DESC, id DESC LIMIT ?",
                                  (target, limit))]


def prune(conn, days=400):
    conn.execute("DELETE FROM pf_hist WHERE last < ?", (time.time() - days * 86400,))
    conn.commit()
