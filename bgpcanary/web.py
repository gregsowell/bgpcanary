"""FastAPI web UI."""
import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import REPO, __version__, alerts, atlas, auth, config, db, evidence, paths, registry, ripe, transit, tree, updates
from .service import Service

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bgpcanary.web")
HERE = os.path.dirname(__file__)

try:
    cfg = config.load()
except config.ConfigError as ex:   # shown by `journalctl -u bgpcanary` instead of a traceback
    log.critical("%s", ex)
    raise SystemExit(2)
svc = Service(cfg)
throttle = auth.Throttle()
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def fmt_dur(s):
    if s is None:
        return "ongoing"
    s = int(s)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


templates.env.filters["dur"] = fmt_dur
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else {}


@asynccontextmanager
async def lifespan(app):
    await svc.start()
    yield
    await svc.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, session_cookie="bgpcanary",
                   max_age=7 * 86400, same_site="strict",
                   https_only=os.environ.get("BGPCANARY_INSECURE_COOKIES") != "1")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


class NeedLogin(Exception):
    pass


class MustChangePassword(Exception):
    pass


class AdminOnly(Exception):
    pass


class NeedSetup(Exception):
    pass


# Pages usable before the ASN/prefixes are set up. Include the targets of their forms: the first-login
# password change posts to /settings/password, and leaving it out made that change impossible on fresh installs.
SETUP_PATHS = ("/settings/monitor", "/restarting", "/account/password", "/settings/password", "/logout", "/users",
               "/settings/alerts")


@app.exception_handler(NeedLogin)
async def _need_login(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login required"}, status_code=401)
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


@app.exception_handler(MustChangePassword)
async def _must_change(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "password change required"}, status_code=403)
    return RedirectResponse("/account/password", status_code=303)


@app.exception_handler(NeedSetup)
async def _need_setup(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "not configured yet"}, status_code=503)
    if getattr(request.state, "role", None) == "admin":
        return RedirectResponse("/settings/monitor", status_code=303)
    return render(request, "setup_pending.html")


@app.exception_handler(AdminOnly)
async def _admin_only(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "admin role required"}, status_code=403)
    resp = render(request, "denied.html")
    resp.status_code = 403
    return resp


def user(request: Request, allow_must_change=False):
    """The logged-in username. Looks the account up on every request, so role changes and deletions take
    effect immediately."""
    u = request.session.get("user")
    if not u:
        raise NeedLogin()
    row = auth.get_user(svc.aconn, u)
    if not row:
        request.session.clear()
        raise NeedLogin()
    request.state.role = row["role"]
    if row["must_change"] and not allow_must_change:
        raise MustChangePassword()
    if not cfg.configured and not request.url.path.startswith(SETUP_PATHS):
        raise NeedSetup()
    return u


def admin(request: Request):
    """Like user(), but requires the admin role (viewers are read-only)."""
    u = user(request)
    if request.state.role != "admin":
        raise AdminOnly()
    return u


def rconn():
    return db.connect(cfg.db_path)


def names(conn, asns):
    asns = [int(a) for a in asns if a is not None and str(a).isdigit()]
    if not asns:
        return {}
    q = ",".join("?" * len(asns))
    return {r["asn"]: r["name"] for r in conn.execute(f"SELECT asn,name FROM asn_names WHERE asn IN ({q})", asns)}


def render(request, name, **ctx):
    role = getattr(request.state, "role", None)
    ctx.update(request=request, cfg=cfg, user=request.session.get("user"), status=svc.engine.status,
               now=time.time(), role=role, is_admin=role == "admin", version=__version__,
               update=updates.state(svc.aconn) if role == "admin" else None,
               outage=bool(request.session.get("user")) and any(m.state == "down" for m in svc.engine.mons.values()))
    return templates.TemplateResponse(request, name, ctx)


# ---------------------------------------------------------------------- auth
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    return render(request, "login.html", next=next, error=None)


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    ip = request.headers.get("x-real-ip") or request.client.host
    if throttle.blocked(ip):
        return render(request, "login.html", next=next, error="Too many failed attempts. Try again later.")
    conn = rconn()
    ok = await asyncio.to_thread(auth.check_user, conn, username, password)
    conn.close()
    if not ok:
        throttle.fail(ip)
        log.warning("failed login for %r from %s", username, ip)
        await asyncio.sleep(1)
        return render(request, "login.html", next=next, error="Invalid username or password.")
    throttle.reset(ip)
    request.session.clear()
    request.session["user"] = username
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    row = auth.get_user(svc.aconn, username)
    if row and row["must_change"]:
        return RedirectResponse("/account/password", status_code=303)
    return RedirectResponse(next, status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------- pages
def prefix_summary(conn):
    out = []
    eng = svc.engine
    for p, m in eng.mons.items():
        v = eng.visible(p)
        base = m.baseline()
        chains = {}
        ups = {}
        for e in list(eng.rib.get(p, {}).values()):
            if e.path and e.path[-1] == cfg.asn:
                c = paths.chain(e.path)
                chains[c] = chains.get(c, 0) + 1
                u = paths.upstream(e.path)
                ups[u] = ups.get(u, 0) + 1
        state = "down" if m.state == "down" else ("degraded" if m.dip_start else "ok")
        if not eng.status.get("phase", "").startswith("live"):
            state = "starting"
        out.append({"prefix": p, "visible": v, "baseline": base, "state": state,
                    "pct": (100 * v / base) if base else None,
                    "chains": sorted(chains.items(), key=lambda x: -x[1]),
                    "upstreams": sorted(ups.items(), key=lambda x: -x[1]),
                    "missing": [u for u in cfg.expected_upstreams if u not in ups]})
    return out


def selected_prefixes(request):
    """Prefixes picked on the dashboard (?prefix=a&prefix=b or ?prefix=a,b); none or unknown = all of them."""
    want = [x.strip() for v in request.query_params.getlist("prefix") for x in v.split(",") if x.strip()]
    sel = [p for p in cfg.prefixes if p in want]
    return sel or list(cfg.prefixes)


STATE_ORDER = {"down": 0, "degraded": 1, "starting": 2, "ok": 3}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user(request)
    conn = rconn()
    sel = selected_prefixes(request)
    every = len(sel) == len(cfg.prefixes)
    summ_all = prefix_summary(conn)
    summ = [s for s in summ_all if s["prefix"] in sel]
    asns = {cfg.asn, *cfg.expected_upstreams}
    for s in summ:
        for c, _ in s["chains"]:
            asns.update(int(x) for x in c.split())
    # events for the selected prefixes plus network-wide ones (no prefix); all of them when everything is selected
    where = "" if every else f"(prefix IN ({','.join('?' * len(sel))}) OR prefix IS NULL) AND "
    args = [] if every else sel
    events = conn.execute(f"SELECT * FROM events WHERE {where}(severity!='info' OR kind IN ('transit_filter',"
                          "'upstream_missing','filter_sim','irr_change','asset_change')) ORDER BY start_ts DESC LIMIT 10",
                          args).fetchall()
    blips = conn.execute(f"SELECT * FROM events WHERE {where}kind='blip' ORDER BY start_ts DESC LIMIT 5", args).fetchall()
    probe = db.annotate_probes(conn, conn.execute("SELECT * FROM probes ORDER BY ts DESC LIMIT 4").fetchall())
    rv_rows = [r for r in conn.execute("SELECT id,ts,server,prefix,n_paths,error FROM rv_snapshots WHERE id IN "
                                       "(SELECT MAX(id) FROM rv_snapshots GROUP BY server, prefix)").fetchall()
               if r["prefix"] in sel]
    rv = None
    if rv_rows:
        rv = {"n_paths": sum(r["n_paths"] or 0 for r in rv_rows), "servers": len(rv_rows),
              "ok": sum(1 for r in rv_rows if not r["error"]), "ts": max(r["ts"] for r in rv_rows)}
    # newest successful "show ip bgp" per collector, newest first, until there are 10+ paths (2-4 collectors)
    rv_latest = []
    for p in sel:
        got = []
        for r in conn.execute("SELECT s.* FROM rv_snapshots s JOIN (SELECT server, MAX(ts) ts FROM rv_snapshots "
                              "WHERE prefix=? AND (error IS NULL OR error='') AND raw!='' GROUP BY server) l "
                              "ON l.server=s.server AND l.ts=s.ts WHERE s.prefix=? ORDER BY s.ts DESC, s.n_paths DESC",
                              (p, p)):
            if len(got) >= 4 or (len(got) >= 2 and sum(x["n_paths"] or 0 for x in got) >= 10):
                break
            got.append(r)
        rv_latest += got
    has_atlas = conn.execute("SELECT 1 FROM atlas_msm WHERE purpose='baseline' LIMIT 1").fetchone() is not None
    nm = names(conn, asns)
    conn.close()
    # combined view (several prefixes): problems first, then lowest visibility
    summ.sort(key=lambda s: (STATE_ORDER.get(s["state"], 9), s["pct"] if s["pct"] is not None else 999))
    chains, ups = {}, {}
    for s in summ:
        for c, n in s["chains"]:
            t = chains.setdefault(c, [0, 0])
            t[0] += n
            t[1] += 1
        for u, n in s["upstreams"]:
            ups[u] = ups.get(u, 0) + n
    combined = {
        "chains": sorted(((c, n, k) for c, (n, k) in chains.items()), key=lambda x: -x[1]),
        "upstreams": sorted(ups.items(), key=lambda x: -x[1]),
        "missing": [u for u in cfg.expected_upstreams if u not in ups],
        "paths": sum(s["visible"] for s in summ),
        "ok": sum(1 for s in summ if s["state"] == "ok"),
        "worst": min((s for s in summ if s["pct"] is not None), key=lambda s: s["pct"], default=None),
    }
    return render(request, "dashboard.html", summ=summ, all_prefixes=[s["prefix"] for s in summ_all], sel=sel,
                  every=every, combined=combined, events=events, blips=blips, probes=probe, rv=rv, names=nm,
                  rv_latest=rv_latest, atlas_targets=svc.atlas.targets() if has_atlas else [])


@app.get("/api/series")
async def api_series(request: Request, prefix: str = None, start: float = None, end: float = None):
    user(request)
    prefix = prefix or cfg.prefixes[0]
    end = end or time.time()
    start = start or end - 86400
    conn = rconn()
    rows = conn.execute("SELECT ts,visible,min_visible,baseline,chains,updates,withdrawals FROM buckets "
                        "WHERE prefix=? AND ts>=? AND ts<=? ORDER BY ts", (prefix, start, end)).fetchall()
    step = max(1, len(rows) // 1500)
    pts = []
    for i in range(0, len(rows), step):
        grp = rows[i:i + step]
        low = min(grp, key=lambda r: r["min_visible"] if r["min_visible"] is not None else r["visible"])
        pts.append({"ts": grp[0]["ts"], "visible": min(r["visible"] for r in grp),
                    "min": min((r["min_visible"] if r["min_visible"] is not None else r["visible"]) for r in grp),
                    "max": max(r["visible"] for r in grp),
                    "baseline": grp[-1]["baseline"],
                    "updates": sum(r["updates"] or 0 for r in grp),
                    "withdrawals": sum(r["withdrawals"] or 0 for r in grp),
                    "chains": json.loads(low["chains"] or "{}")})
    # insert null points across data gaps so charts show a break instead of a flat line
    step_s = step * cfg.bucket_seconds
    gapped = []
    for p in pts:
        if gapped and p["ts"] - gapped[-1]["ts"] > 3 * step_s:
            gapped.append({"ts": gapped[-1]["ts"] + step_s, "visible": None, "min": None, "max": None,
                           "baseline": None, "updates": None, "withdrawals": None, "chains": {}, "gap": True})
        gapped.append(p)
    pts = gapped
    totals = {}
    for p in pts:
        for c, n in p["chains"].items():
            totals[c] = totals.get(c, 0) + n
    top = [c for c, _ in sorted(totals.items(), key=lambda x: -x[1])[:6]]
    events = [dict(r) for r in conn.execute(
        "SELECT id,kind,severity,start_ts,end_ts,title FROM events WHERE (prefix=? OR prefix IS NULL) "
        "AND start_ts<=? AND COALESCE(end_ts, ?)>=? ORDER BY start_ts", (prefix, end, time.time(), start))]
    asns = set()
    for c in top:
        asns.update(int(x) for x in c.split())
    nm = names(conn, asns)
    conn.close()
    chain_series = {c: [None if p.get("gap") else p["chains"].get(c, 0) for p in pts] for c in top}
    chain_series["other"] = [None if p.get("gap") else sum(v for k, v in p["chains"].items() if k not in top)
                             for p in pts]
    return {"prefix": prefix, "start": start, "end": end, "step": step * cfg.bucket_seconds,
            "ts": [p["ts"] for p in pts], "visible": [p["visible"] for p in pts], "min": [p["min"] for p in pts],
            "baseline": [p["baseline"] for p in pts], "updates": [p["updates"] for p in pts],
            "withdrawals": [p["withdrawals"] for p in pts], "chains": chain_series,
            "chain_labels": {c: " → ".join(f"AS{x} {nm.get(int(x), '')}".strip() for x in c.split()) for c in top},
            "events": events, "threshold_pct": cfg.outage_threshold_pct}


@app.get("/api/series/multi")
async def api_series_multi(request: Request, prefixes: str, start: float = None, end: float = None):
    """Several prefixes on one time grid (dashboard combined view): visibility as % of each prefix's baseline
    (one line each, up to MAX_STRANDS; the rest folded into the lowest of them), chains and churn summed."""
    user(request)
    want = [p for p in dict.fromkeys(prefixes.split(",")) if p in cfg.prefixes]
    if not want:
        raise HTTPException(400, "no monitored prefixes given")
    end = end or time.time()
    start = start or end - 86400
    conn = rconn()
    q = ",".join("?" * len(want))
    rows = conn.execute(f"SELECT prefix,ts,visible,min_visible,baseline,chains,updates,withdrawals FROM buckets "
                        f"WHERE prefix IN ({q}) AND ts>=? AND ts<=? ORDER BY ts", (*want, start, end)).fetchall()
    # the same time slots for every prefix: about 1500 across the window, at least one bucket each
    slot_s = max(cfg.bucket_seconds, (end - start) / 1500)
    nslots = int((end - start) // slot_s) + 1
    low = {p: [None] * nslots for p in want}   # lowest % of baseline in each slot
    lowest_row = {}                            # (prefix, slot) -> bucket with the lowest visibility
    upd, wd = [None] * nslots, [None] * nslots
    vis = lambda r: r["min_visible"] if r["min_visible"] is not None else r["visible"]
    for r in rows:
        i = int((r["ts"] - start) // slot_s)
        if not 0 <= i < nslots:
            continue
        if r["baseline"]:
            pct = 100 * vis(r) / r["baseline"]
            if low[r["prefix"]][i] is None or pct < low[r["prefix"]][i]:
                low[r["prefix"]][i] = pct
        k = (r["prefix"], i)
        if k not in lowest_row or vis(r) < vis(lowest_row[k]):
            lowest_row[k] = r
        upd[i] = (upd[i] or 0) + (r["updates"] or 0)
        wd[i] = (wd[i] or 0) + (r["withdrawals"] or 0)
    chains = [dict() for _ in range(nslots)]
    for (p, i), r in lowest_row.items():
        for c, n in json.loads(r["chains"] or "{}").items():
            chains[i][c] = chains[i].get(c, 0) + n
    has = [upd[i] is not None for i in range(nslots)]   # slots with any data; the rest are gaps
    # the prefixes that dipped lowest get their own line; any beyond MAX_STRANDS share a "lowest of the others" line
    worst = {p: min((x for x in low[p] if x is not None), default=999) for p in want}
    order = sorted(want, key=lambda p: worst[p])
    shown, rest = order[:MAX_STRANDS], order[MAX_STRANDS:]
    lines = [{"prefix": p, "pct": [round(x, 1) if x is not None else None for x in low[p]]} for p in shown]
    others = None
    if rest:
        others = {"count": len(rest),
                  "pct": [min((round(low[p][i], 1) for p in rest if low[p][i] is not None), default=None)
                          for i in range(nslots)]}
    totals = {}
    for d in chains:
        for c, n in d.items():
            totals[c] = totals.get(c, 0) + n
    top = [c for c, _ in sorted(totals.items(), key=lambda x: -x[1])[:6]]
    chain_series = {c: [d.get(c, 0) if has[i] else None for i, d in enumerate(chains)] for c in top}
    chain_series["other"] = [sum(n for c, n in d.items() if c not in top) if has[i] else None
                             for i, d in enumerate(chains)]
    every = len(want) == len(cfg.prefixes)
    events = [dict(r) for r in conn.execute(
        "SELECT id,kind,severity,start_ts,end_ts,title FROM events WHERE "
        + ("" if every else f"(prefix IN ({q}) OR prefix IS NULL) AND ")
        + "start_ts<=? AND COALESCE(end_ts, ?)>=? ORDER BY start_ts",
        (*([] if every else want), end, time.time(), start))]
    nm = names(conn, {int(x) for c in top for x in c.split()})
    conn.close()
    return {"prefixes": want, "start": start, "end": end, "step": slot_s,
            "ts": [start + i * slot_s for i in range(nslots)], "lines": lines, "others": others,
            "updates": upd, "withdrawals": wd, "chains": chain_series,
            "chain_labels": {c: " → ".join(f"AS{x} {nm.get(int(x), '')}".strip() for x in c.split()) for c in top},
            "events": events, "threshold_pct": cfg.outage_threshold_pct}


@app.get("/api/status")
async def api_status(request: Request):
    user(request)
    conn = rconn()
    s = prefix_summary(conn)
    conn.close()
    st = dict(svc.engine.status)
    return {"status": st, "prefixes": s}


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, kind: str = "", severity: str = "", info: int = 0, page: int = 1):
    user(request)
    conn = rconn()
    where, args = [], []
    if kind:
        where.append("kind=?")
        args.append(kind)
    if severity:
        where.append("severity=?")
        args.append(severity)
    elif not info:
        where.append("severity!='info'")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) c FROM events {w}", args).fetchone()["c"]
    rows = conn.execute(f"SELECT * FROM events {w} ORDER BY start_ts DESC LIMIT 100 OFFSET ?",
                        args + [(page - 1) * 100]).fetchall()
    kinds = [r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM events ORDER BY kind")]
    conn.close()
    return render(request, "events.html", rows=rows, total=total, page=page, kind=kind, severity=severity,
                  info=info, kinds=kinds)


@app.get("/events/{eid}", response_class=HTMLResponse)
async def event_page(request: Request, eid: int):
    user(request)
    conn = rconn()
    ev = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not ev:
        raise HTTPException(404)
    end = ev["end_ts"] or time.time()
    pad = max(900, min(3 * 3600, (end - ev["start_ts"]) * 0.5))
    ups = conn.execute("SELECT * FROM updates WHERE prefix=? AND ts>=? AND ts<=? ORDER BY ts LIMIT 3000",
                       (ev["prefix"], ev["start_ts"] - 300, min(end, ev["start_ts"] + 1800))).fetchall()
    probes = conn.execute("SELECT * FROM probes WHERE event_id=? OR (ts>=? AND ts<=?) ORDER BY ts",
                          (eid, ev["start_ts"] - 3600, end + 600)).fetchall()
    snaps = conn.execute("SELECT * FROM rv_snapshots WHERE event_id=? OR (ts>=? AND ts<=?) ORDER BY ts",
                         (eid, ev["start_ts"] - 1800, end + 600)).fetchall()
    alerts_sent = conn.execute("SELECT * FROM alert_log WHERE event_id=? ORDER BY ts", (eid,)).fetchall()
    atlas_msms = conn.execute("SELECT m.*, (SELECT COUNT(*) FROM atlas_result r WHERE r.msm_id=m.id) results, "
                              "(SELECT SUM(reached) FROM atlas_result r WHERE r.msm_id=m.id) reached "
                              "FROM atlas_msm m WHERE m.event_id=? ORDER BY m.created", (eid,)).fetchall()
    base = conn.execute("SELECT target FROM atlas_msm WHERE purpose='baseline' AND created<=? "
                        "AND (stopped IS NULL OR stopped>=?) ORDER BY created DESC LIMIT 1", (end, ev["start_ts"])).fetchone()
    ev_data = await _evidence(conn, ev)
    conn.close()
    return render(request, "event.html", ev=ev, ups=ups, probes=probes, snaps=snaps, alerts=alerts_sent,
                  win_start=ev["start_ts"] - pad, win_end=end + pad, atlas_msms=atlas_msms,
                  atlas_target=base["target"] if base else None, evd=ev_data, compare_all=",".join(svc.atlas.targets()))


async def _evidence(conn, ev):
    nbrs = await tree.upstream_neighbors(svc.sourceapp, sorted(set(cfg.expected_upstreams)))
    return evidence.collect(conn, cfg, ev, atlas_payload, nbrs, svc.atlas.label, lambda a: names(conn, a))


@app.get("/events/{eid}/report.txt")
async def event_report(request: Request, eid: int):
    """The event's evidence as plain text, to attach to a ticket with an upstream provider."""
    user(request)
    conn = rconn()
    try:
        ev = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not ev:
            raise HTTPException(404)
        text = evidence.as_text(cfg, ev, await _evidence(conn, ev),
                                link=f"{cfg.base_url}/events/{eid}" if cfg.base_url else "")
    finally:
        conn.close()
    from fastapi.responses import PlainTextResponse
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(ev["start_ts"]))
    return PlainTextResponse(text, headers={"Content-Disposition": f'inline; filename="bgpcanary-event-{eid}-{stamp}.txt"'})


def replay_payload(conn, prefix, s, e):
    """Peer-level path history for [s, e], for the path-tree replay (runs in a worker thread).

    Returns the RIB at `s` plus every change inside the window, compactly:
    peers[i] = [collector, peer_ip, peer_asn]; paths[j] = "a b c ... origin";
    init[i] = path index or -1; updates = [[ts, peer_index, path_index_or_-1], ...]
    """
    base = db.state_at(conn, prefix, s)
    ups = conn.execute("SELECT ts, collector, peer, peer_asn, kind, path FROM updates "
                       "WHERE prefix=? AND ts>? AND ts<=? ORDER BY ts, id", (prefix, s, e)).fetchall()
    peer_idx, peers, path_idx, plist = {}, [], {}, []

    def pi(r):
        k = (r["collector"], r["peer"])
        if k not in peer_idx:
            peer_idx[k] = len(peers)
            peers.append([r["collector"], r["peer"], r["peer_asn"]])
        return peer_idx[k]

    def pa(r):
        if r["kind"] != "A" or not r["path"]:
            return -1
        if r["path"] not in path_idx:
            path_idx[r["path"]] = len(plist)
            plist.append(r["path"])
        return path_idx[r["path"]]

    init = {}
    for r in base:
        init[pi(r)] = pa(r)
    last = dict(init)
    updates = []
    for r in ups:
        i, j = pi(r), pa(r)
        if last.get(i, -1) == j:
            continue  # re-announcement of the same path (communities etc.) - no change for the tree
        last[i] = j
        updates.append([round(r["ts"], 1), i, j])
    asns = set()
    for p in plist:
        asns.update(int(x) for x in p.split())
    events = [{"id": r["id"], "start": r["start_ts"], "end": r["end_ts"], "severity": r["severity"],
               "kind": r["kind"], "title": r["title"]} for r in conn.execute(
        "SELECT * FROM events WHERE prefix=? AND kind='outage' AND start_ts<=? AND COALESCE(end_ts, ?)>=? "
        "ORDER BY start_ts", (prefix, e, time.time(), s))]
    return {"asn": cfg.asn, "prefix": prefix, "start": s, "end": e, "peers": peers, "paths": plist,
            "init": [init.get(i, -1) for i in range(len(peers))], "updates": updates, "events": events,
            "names": {str(k): v for k, v in names(conn, asns).items()}}


_NBR_CACHE = {}   # upstream asn -> (fetched_ts, [{"asn", "power"}])


async def upstream_neighbors(data):
    """Attach each direct upstream's own neighbours (its transits/peers, i.e. ASes seen to its left in the
    global table, from RIPEstat asn-neighbours) so the tree can show who is NOT carrying our prefix.
    Upstreams = configured expected upstreams + any AS seen adjacent to our origin in the window."""
    ups = set(cfg.expected_upstreams)
    for p in data["paths"]:
        a = [int(x) for x in p.split()]
        if len(a) >= 2 and a[-1] == cfg.asn:
            ups.add(a[-2])
    out = {}
    for u in sorted(ups):
        hit = _NBR_CACHE.get(u)
        if not hit or time.time() - hit[0] > 6 * 3600:
            try:
                d = await ripe.stat("asn-neighbours", svc.sourceapp, resource=f"AS{u}")
                lst = [{"asn": n["asn"], "power": n.get("power", 0)} for n in d.get("neighbours", [])
                       if n.get("type") in ("left", "uncertain") and n["asn"] != cfg.asn]
                hit = (time.time(), tree.top_neighbors(lst, key=lambda n: n["asn"]))
                _NBR_CACHE[u] = hit
            except Exception as ex:
                log.warning("asn-neighbours AS%s failed: %s", u, ex)
                hit = hit or (0, [])
        out[str(u)] = hit[1]
    data["upstreams"] = sorted(ups)
    data["upstream_neighbors"] = out
    # names for neighbours that never appear in our own paths
    want = {n["asn"] for lst in out.values() for n in lst} | ups
    missing = [a for a in want if str(a) not in data["names"]]
    if missing:
        known = names(svc.aconn, missing)
        for a in missing:
            if a in known:
                data["names"][str(a)] = known[a]
        todo = [a for a in missing if str(a) not in data["names"]][:40]

        async def fetch(a):
            try:
                nm = await ripe.as_name(a, svc.sourceapp)
                svc.aconn.execute("INSERT OR REPLACE INTO asn_names(asn,name,fetched) VALUES(?,?,?)", (a, nm, time.time()))
                data["names"][str(a)] = nm
            except Exception:
                pass
        await asyncio.gather(*(fetch(a) for a in todo))
        svc.aconn.commit()
    return data


@app.get("/api/events/{eid}/replay")
async def api_event_replay(request: Request, eid: int):
    user(request)
    conn = rconn()
    try:
        ev = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not ev or not ev["prefix"]:
            raise HTTPException(404)
        end = ev["end_ts"] or time.time()
        pad = min(1800, max(600, (end - ev["start_ts"]) * 0.08))
        s, e = ev["start_ts"] - pad, min(end + pad, time.time())
        out = await asyncio.to_thread(replay_payload, conn, ev["prefix"], s, e)
    finally:
        conn.close()
    out["event"] = {"id": ev["id"], "title": ev["title"], "severity": ev["severity"], "kind": ev["kind"],
                    "start": ev["start_ts"], "end": ev["end_ts"], "prefix": ev["prefix"]}
    out["events"] = [out["event"]]
    return await upstream_neighbors(out)


@app.get("/api/replay")
async def api_window_replay(request: Request, prefix: str = None, start: float = None, end: float = None):
    """Path-tree data for an arbitrary window (dashboard), default the last 24 h."""
    user(request)
    prefix = prefix or cfg.prefixes[0]
    end = min(end or time.time(), time.time())
    start = max(start or end - 86400, end - 92 * 86400)
    conn = rconn()
    try:
        out = await asyncio.to_thread(replay_payload, conn, prefix, start, end)
    finally:
        conn.close()
    return await upstream_neighbors(out)


MAX_STRANDS = 12   # strand colors in replay.js


def merge_replays(parts):
    """Combine per-prefix replay payloads into one: peers are tagged with their prefix (peer_prefix), paths
    are de-duplicated, updates are merged in time order. The tree then draws one strand per prefix."""
    out = {k: parts[0][k] for k in ("asn", "start", "end")}
    out.update(prefix=", ".join(p["prefix"] for p in parts), prefixes=[p["prefix"] for p in parts], peers=[],
               peer_prefix=[], paths=[], init=[], updates=[], events=[], names={})
    path_idx = {}
    for n, part in enumerate(parts):
        off = len(out["peers"])
        remap = []
        for path in part["paths"]:
            if path not in path_idx:
                path_idx[path] = len(out["paths"])
                out["paths"].append(path)
            remap.append(path_idx[path])
        out["peers"] += part["peers"]
        out["peer_prefix"] += [n] * len(part["peers"])
        out["init"] += [remap[j] if j >= 0 else -1 for j in part["init"]]
        out["updates"] += [[ts, i + off, remap[j] if j >= 0 else -1] for ts, i, j in part["updates"]]
        out["events"] += [dict(e, prefix=part["prefix"]) for e in part["events"]]
        out["names"].update(part["names"])
    out["updates"].sort(key=lambda u: u[0])
    return out


@app.get("/api/replay/compare")
async def api_compare_replay(request: Request, prefixes: str, start: float = None, end: float = None,
                             combine: int = 0):
    """Path-tree data for several prefixes at once (dashboard tree). Up to MAX_STRANDS get a colored strand each;
    combine=1 merges any number into a single strand (the dashboard does that past its strand setting)."""
    user(request)
    want = [p for p in dict.fromkeys(prefixes.split(",")) if p in cfg.prefixes][:50 if combine else MAX_STRANDS]
    if not want:
        raise HTTPException(400, "no monitored prefixes given")
    end = min(end or time.time(), time.time())
    start = max(start or end - 86400, end - 92 * 86400)
    conn = rconn()
    try:
        parts = [await asyncio.to_thread(replay_payload, conn, p, start, end) for p in want]
    finally:
        conn.close()
    out = merge_replays(parts)
    if combine:   # one strand: every (peer, prefix) pair counts as a path, no per-prefix colors
        del out["peer_prefix"], out["prefixes"]
        out["prefix"] = f"{len(want)} prefixes combined"
        out["combined"] = want
    return await upstream_neighbors(out)


@app.get("/paths", response_class=HTMLResponse)
async def paths_page(request: Request, prefix: str = None):
    user(request)
    prefix = prefix or cfg.prefixes[0]
    conn = rconn()
    cur = svc.engine.current_paths(prefix)
    others = sorted(p for p in svc.engine.rib if p not in svc.engine.mons and
                    any(e.path for e in svc.engine.rib[p].values()))
    snaps = conn.execute("SELECT * FROM rv_snapshots WHERE id IN (SELECT MAX(id) FROM rv_snapshots "
                         "WHERE prefix=? GROUP BY server)", (prefix,)).fetchall()
    asns = set()
    for c in cur:
        asns.update(c["path"])
    for s in snaps:
        for p in json.loads(s["paths"] or "[]"):
            asns.update(int(x) for x in p.split())
    nm = names(conn, asns)
    conn.close()
    rv = []
    for srv in cfg.rv_servers:
        b = svc._rv_state(srv)
        ok, why = svc.rv_allowed(srv, "scheduled")
        rv.append({"server": srv, "ok": ok, "why": why, "conns": b.get("conns", 0), "kb": b.get("bytes_in", 0) / 1000})
    return render(request, "paths.html", prefix=prefix, cur=cur, snaps=snaps, names=nm, others=others, rv=rv)


@app.get("/updates", response_class=HTMLResponse)
async def updates_page(request: Request, start: float = None, end: float = None, q: str = "", source: str = ""):
    user(request)
    end = end or time.time()
    start = start or end - 86400
    conn = rconn()
    where, args = ["ts>=?", "ts<=?"], [start, end]
    if q:
        where.append("(path LIKE ? OR peer LIKE ? OR collector LIKE ? OR prefix LIKE ?)")
        args += [f"%{q}%"] * 4
    if source:
        where.append("source=?")
        args.append(source)
    rows = conn.execute(f"SELECT * FROM updates WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT 2000",
                        args).fetchall()
    conn.close()
    return render(request, "updates.html", rows=rows, start=start, end=end, q=q, source=source)


@app.get("/probes", response_class=HTMLResponse)
async def probes_page(request: Request, page: int = 1):
    user(request)
    conn = rconn()
    rows = conn.execute("SELECT id,ts,server,kind,target,trigger,event_id,success_pct,rtt_avg,last_hop,last_hop_asn,"
                        "reached,error FROM probes ORDER BY ts DESC LIMIT 100 OFFSET ?", ((page - 1) * 100,)).fetchall()
    snaps = conn.execute("SELECT id,ts,server,prefix,n_paths,event_id,error FROM rv_snapshots ORDER BY ts DESC "
                         "LIMIT 50").fetchall()
    # latest routine (not event-triggered) "show ip bgp" per collector and prefix, in the configured order
    latest = conn.execute("SELECT s.* FROM rv_snapshots s JOIN (SELECT server, prefix, MAX(ts) ts FROM rv_snapshots "
                          "WHERE event_id IS NULL GROUP BY server, prefix) l ON l.server=s.server AND l.prefix=s.prefix "
                          "AND l.ts=s.ts").fetchall()
    order = {s: i for i, s in enumerate(cfg.rv_servers)}
    pfx = {p: i for i, p in enumerate(cfg.prefixes)}
    latest = [dict(r) for r in sorted((r for r in latest if r["prefix"] in pfx),
                                      key=lambda r: (order.get(r["server"], 99), r["server"], pfx[r["prefix"]]))]
    for r in latest:   # a failed run: also show that collector's last good output
        if r["error"]:
            r["prev"] = conn.execute("SELECT id, ts, n_paths, raw FROM rv_snapshots WHERE server=? AND prefix=? "
                                     "AND (error IS NULL OR error='') AND raw!='' ORDER BY ts DESC LIMIT 1",
                                     (r["server"], r["prefix"])).fetchone()
    rows = db.annotate_probes(conn, rows)
    nm = names(conn, [r["last_hop_asn"] for r in rows])
    conn.close()
    return render(request, "probes.html", rows=rows, snaps=snaps, page=page, names=nm, latest=latest)


@app.get("/probes/{pid}", response_class=HTMLResponse)
async def probe_page(request: Request, pid: int):
    user(request)
    conn = rconn()
    r = conn.execute("SELECT * FROM probes WHERE id=?", (pid,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404)
    r = db.annotate_probes(conn, [r])[0]
    nm = names(conn, [r["last_hop_asn"]])
    conn.close()
    return render(request, "probe.html", p=r, names=nm)


@app.get("/snapshots/{sid}", response_class=HTMLResponse)
async def snapshot_page(request: Request, sid: int):
    user(request)
    conn = rconn()
    r = conn.execute("SELECT * FROM rv_snapshots WHERE id=?", (sid,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(404)
    return render(request, "snapshot.html", s=r)


@app.post("/actions/probe")
async def action_probe(request: Request, kind: str = Form(...), target: str = Form(...)):
    admin(request)
    import ipaddress
    try:
        ipaddress.ip_address(target)
    except ValueError:
        raise HTTPException(400, "target must be an IP address")
    if kind not in ("ping", "traceroute"):
        raise HTTPException(400)
    asyncio.create_task(svc.run_probe(kind, target, "manual"))
    return RedirectResponse("/probes?started=1", status_code=303)


@app.post("/actions/snapshot")
async def action_snapshot(request: Request):
    admin(request)
    asyncio.create_task(svc.snapshot_all(trigger="manual"))
    return RedirectResponse("/probes?started=1", status_code=303)


@app.post("/actions/resync")
async def action_resync(request: Request):
    admin(request)
    asyncio.create_task(svc._resync_all())
    return RedirectResponse("/settings?msg=Resync+started", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str = ""):
    user(request)
    conn = rconn()
    log_rows = conn.execute("SELECT a.*, e.title FROM alert_log a LEFT JOIN events e ON e.id=a.event_id "
                            "ORDER BY a.ts DESC LIMIT 30").fetchall()
    counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in ("updates", "buckets", "events", "probes", "rv_snapshots")}
    oldest = conn.execute("SELECT MIN(ts) t FROM buckets").fetchone()["t"]
    conn.close()
    size = os.path.getsize(cfg.db_path) if os.path.exists(cfg.db_path) else 0
    tf_rows = svc.transit.last_result
    conn = rconn()
    nm = names(conn, {r["upstream"] for r in tf_rows} | {r["transit"] for r in tf_rows})
    conn.close()
    return render(request, "settings.html", msg=msg, log_rows=log_rows, counts=counts, size=size, oldest=oldest,
                  tf_enabled=transit.enabled(cfg, svc.aconn), tf_rows=tf_rows, tf_last=svc.transit.last_run, names=nm,
                  reg_enabled=registry.enabled(cfg, svc.aconn),
                  atlas_on=atlas.enabled(svc.aconn),
                  alert_types_on=sum(1 for t in config.ALERT_TYPES if config.alert_mode(cfg, t["kind"]) != "off"),
                  alert_types_total=len(config.ALERT_TYPES),
                  n_admins=sum(1 for u in auth.list_users(svc.aconn) if u["role"] == "admin"),
                  n_viewers=sum(1 for u in auth.list_users(svc.aconn) if u["role"] == "viewer"))


@app.get("/registry", response_class=HTMLResponse)
async def registry_page(request: Request, msg: str = ""):
    user(request)
    conn = rconn()
    hist = conn.execute("SELECT * FROM events WHERE kind IN ('registry','filter_sim','irr_change','asset_change') "
                        "ORDER BY start_ts DESC LIMIT 50").fetchall()
    conn.close()
    rep = svc.registry.report
    # Reality next to the simulation: which of each upstream's transits carry us right now (live RIS), and
    # whether that transit has its own as-set listing us (which is how it can carry us even when the
    # upstream's registration wouldn't allow it).
    carry = {}
    for p in cfg.prefixes:
        for e in list(svc.engine.rib.get(p, {}).values()):
            if e.path and e.path[-1] == cfg.asn and len(e.path) >= 3:
                key = (e.path[-2], e.path[-3])
                carry[key] = carry.get(key, 0) + 1
    tf = {(r["upstream"], r["transit"]): r for r in svc.transit.last_result}
    ups = [f["upstream"] for f in (rep or {}).get("filters", [])]
    try:   # each upstream's full transit/peer list from the global table (cached 6 h), carrying us or not
        nbrs = await tree.upstream_neighbors(svc.sourceapp, ups)
    except Exception:
        nbrs = {}
    reality, asns = {}, set()
    for f in (rep or {}).get("filters", []):
        u = f["upstream"]
        transits = {t for (uu, t) in carry if uu == u} | {t for (uu, t) in tf if uu == u} | \
                   {t for t, _power in nbrs.get(u, []) if t != cfg.asn}
        rows = []
        for t in transits:
            own = [a["name"] for a in rep.get("assets", [])
                   if a["name"].upper().startswith(f"AS{t}:") or re.search(rf"\bAS{t}\b", a.get("mnt", "").upper())]
            ref = tf.get((u, t), {}).get("ref")
            rows.append({"transit": t, "ours": carry.get((u, t), 0), "ref": ref, "own_sets": own})
            asns.add(t)
        rows.sort(key=lambda r: (-r["ours"], -(r["ref"] or 0)))
        reality[u] = rows
        asns.add(u)
    nm = names(svc.aconn, asns)
    return render(request, "registry.html", msg=msg, rep=rep, last=svc.registry.last_run,
                  running=svc.registry.running, reg_enabled=registry.enabled(cfg, svc.aconn), hist=hist,
                  reality=reality, names=nm)


ASSET_RE = re.compile(r"^[A-Za-z0-9:_.\-]{2,100}$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9\-]{0,20}$")


async def _asset(name, source):
    if not ASSET_RE.match(name) or not SOURCE_RE.match(source or ""):
        raise HTTPException(400, "invalid as-set or registry name")
    return await asyncio.to_thread(registry.asset_detail, name, source, cfg.asn, cfg.expected_upstreams)


@app.get("/api/asset")
async def api_asset(request: Request, name: str, source: str = ""):
    """Summary for the hover card on the Registry page."""
    user(request)
    d = await _asset(name, source)
    nm = names(svc.aconn, [m[2:] for m in d["direct"][:15] if m.startswith("AS") and m[2:].isdigit()])
    return {"name": d["name"], "source": d["source"], "descr": d["descr"], "mnt": d["mnt"],
            "modified": d["modified"], "direct_count": len(d["direct"]), "expanded_count": len(d["expanded"]),
            "direct_sets": d["direct_sets"], "includes_me": d["includes_me"], "chain": d["chain"],
            "upstreams_in": d["upstreams_in"], "asn": cfg.asn,
            "direct_preview": [{"m": m, "name": nm.get(int(m[2:]), "") if m[2:].isdigit() else ""}
                               for m in d["direct"][:15]]}


@app.get("/registry/asset/{name}", response_class=HTMLResponse)
async def asset_page(request: Request, name: str, source: str = ""):
    user(request)
    d = await _asset(name, source)
    nm = names(svc.aconn, [m[2:] for m in d["expanded"]] + [m[2:] for m in d["direct"] if m[2:].isdigit()])
    return render(request, "asset.html", d=d, names=nm, mine=f"AS{cfg.asn}",
                  ups={f"AS{u}" for u in cfg.expected_upstreams})


@app.post("/registry/run")
async def registry_run(request: Request):
    admin(request)
    asyncio.create_task(svc.registry.run())
    return RedirectResponse("/registry?msg=" + quote("Checks started. They take about 15-30 seconds; refresh to see "
                                                    "the results."), status_code=303)


@app.post("/registry/toggle")
async def registry_toggle(request: Request, enabled: str = Form("")):
    admin(request)
    on = enabled == "1"
    db.kv_set(svc.aconn, registry.KV_TOGGLE, on)
    svc.aconn.commit()
    if on:
        asyncio.create_task(svc.registry.run())
        msg = "Registry watch turned on. The first check is running now."
    else:
        svc.registry.close_all()
        msg = "Registry watch turned off. Its open notices were closed quietly."
    return RedirectResponse(f"/registry?msg={quote(msg)}", status_code=303)


@app.post("/settings/transit-filter")
async def toggle_transit_filter(request: Request, enabled: str = Form("")):
    admin(request)
    on = enabled == "1"
    db.kv_set(svc.aconn, transit.KV_TOGGLE, on)
    svc.aconn.commit()
    if on:
        asyncio.create_task(svc.transit.run())
        msg = "Transit filter check turned on. The first results appear within a minute; anything flagged must show up in two checks in a row before it is reported."
    else:
        svc.transit.close_all("turned off")
        msg = "Transit filter check turned off. Its open notices were closed quietly."
    return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)


# ---------------------------------------------------------------------- monitoring settings (admins)
STARTED = time.time()


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(HERE, "static", "favicon.ico"), media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness check (used by the restart page and by external monitoring)."""
    return {"ok": True, "version": __version__, "started": STARTED, "phase": svc.engine.status.get("phase"),
            "configured": cfg.configured}


# ---------------------------------------------------------------------- about & updates
@app.get("/settings/about", response_class=HTMLResponse)
async def about_page(request: Request, msg: str = ""):
    user(request)
    return render(request, "about.html", msg=msg, st=updates.state(svc.aconn), check_on=updates.enabled(svc.aconn),
                  repo=REPO)


@app.post("/settings/about")
async def about_save(request: Request, action: str = Form(""), enabled: str = Form("")):
    who = admin(request)
    if action == "check":
        st = await updates.check(svc.aconn)
        msg = (f"Version {st['latest']} is available." if st["available"] else
               "Couldn't check: " + st["error"] if st.get("error") else "You're running the latest version.")
    else:
        db.kv_set(svc.aconn, updates.KV_ENABLED, enabled == "1")
        svc.aconn.commit()
        log.info("user %s turned the update check %s", who, "on" if enabled == "1" else "off")
        msg = "Daily update check turned " + ("on." if enabled == "1" else "off.")
    return RedirectResponse(f"/settings/about?msg={quote(msg)}", status_code=303)


def _restart_soon(delay=1.0):
    """Exit gracefully; systemd (Restart=always) starts the service again with the new settings."""
    import signal
    asyncio.get_running_loop().call_later(delay, os.kill, os.getpid(), signal.SIGTERM)


def _parse_list(text):
    return [x for x in re.split(r"[\s,;]+", text or "") if x]


@app.get("/settings/monitor", response_class=HTMLResponse)
async def monitor_settings_page(request: Request, msg: str = "", err: int = 0):
    admin(request)
    return render(request, "monitor_settings.html", msg=msg, err=err, m=config.monitor_settings_dict(cfg),
                  from_db=db.kv_get(svc.aconn, config.MONITOR_KV) is not None, first_run=not cfg.configured)


@app.post("/settings/monitor")
async def monitor_settings_save(request: Request, asn: str = Form(""), prefixes: str = Form(""),
                                expected_upstreams: str = Form(""), probe_targets: str = Form(""),
                                owned_space: str = Form(""), site_name: str = Form("")):
    import ipaddress
    who = admin(request)

    def back(m):
        return RedirectResponse(f"/settings/monitor?err=1&msg={quote(m)}", status_code=303)

    a = asn.strip().upper().removeprefix("AS")
    if not a.isdigit() or not 0 < int(a) < 2 ** 32:
        return back("Enter your ASN as a number, e.g. 64500")
    pfx, problems = [], []
    for x in _parse_list(prefixes):
        try:
            net = ipaddress.ip_network(x, strict=True)
        except ValueError:
            try:
                problems.append(f"{x} has host bits set - did you mean {ipaddress.ip_network(x, strict=False)}?")
            except ValueError:
                problems.append(f"{x} isn't a valid prefix")
            continue
        if (net.version == 4 and net.prefixlen < 8) or (net.version == 6 and net.prefixlen < 16):
            problems.append(f"{net} is too broad to monitor")
            continue
        if str(net) not in pfx:
            pfx.append(str(net))
    if problems:
        return back("; ".join(problems))
    if not pfx:
        return back("Add at least one prefix")
    ups = []
    for x in _parse_list(expected_upstreams):
        y = x.upper().removeprefix("AS")
        if not y.isdigit():
            return back(f"Upstream {x} isn't an ASN")
        ups.append(int(y))
    owned = []
    for x in _parse_list(owned_space):
        try:
            owned.append(str(ipaddress.ip_network(x, strict=False)))
        except ValueError:
            return back(f"Owned space {x} isn't a valid prefix")
    targets = []
    for x in _parse_list(probe_targets):
        try:
            targets.append(str(ipaddress.ip_address(x)))
        except ValueError:
            return back(f"Probe target {x} isn't an IP address")
    data = {"asn": int(a), "prefixes": pfx, "expected_upstreams": ups, "owned_space": owned,
            "probe_targets": targets, "site_name": site_name.strip() or f"BGP Canary · AS{a}"}
    old = config.monitor_settings_dict(cfg)
    db.kv_set(svc.aconn, config.MONITOR_KV, data)
    svc.aconn.commit()
    added = [p for p in pfx if p not in old["prefixes"]]
    log.info("user %s changed monitoring settings: %s (added %s)", who, data, added)
    _restart_soon()
    return RedirectResponse("/restarting?" + urlencode({"since": STARTED, "added": ",".join(added)}),
                            status_code=303)


@app.get("/restarting", response_class=HTMLResponse)
async def restarting_page(request: Request, since: float = 0, added: str = ""):
    return render(request, "restarting.html", since=since, added=[x for x in added.split(",") if x])


# ---------------------------------------------------------------------- detection & data sources (admins)
@app.get("/settings/advanced", response_class=HTMLResponse)
async def advanced_settings_page(request: Request, msg: str = "", err: int = 0):
    admin(request)
    file_cfg = config.load()   # what the config file alone says, to show defaults and what's overridden
    sections = {}
    for attr, sec, label, kind, opts in config.TUNING:
        val, default = getattr(cfg, attr), getattr(file_cfg, attr)
        sections.setdefault(sec, []).append({"attr": attr, "label": label, "kind": kind, "opts": opts,
                                             "value": val, "default": default, "changed": val != default})
    # current "normal" (baseline) per prefix, so % thresholds can be shown as peer counts
    baselines = {p: m.baseline() for p, m in svc.engine.mons.items() if m.baseline()}
    return render(request, "advanced_settings.html", msg=msg, err=err, sections=sections,
                  from_db=db.kv_get(svc.aconn, config.TUNING_KV) is not None, baselines=baselines)


@app.post("/settings/advanced")
async def advanced_settings_save(request: Request):
    who = admin(request)
    form = await request.form()
    data, problems = {}, []
    for attr, _sec, label, kind, opts in config.TUNING:
        raw = form.get(attr, "" if kind != "bool" else None)
        if kind == "bool":
            raw = "1" if raw else ""
        try:
            data[attr] = config.coerce_tuning(attr, kind, opts, raw)
        except (ValueError, TypeError) as ex:
            problems.append(f"{label}: {ex}")
    if data.get("recover_threshold_pct", 100) < data.get("outage_threshold_pct", 0):
        problems.append("The recovery level must be at or above the outage threshold")
    if not data.get("rv_servers"):
        problems.append("List at least one Route Views collector")
    # Route Views: 100 connections/collector/day per IP; keep scheduled snapshots + probes well under that
    snaps = 1440 / data["rv_poll_minutes"] if data.get("rv_poll_minutes") else 0
    probes = 2 * 1440 / data["probe_minutes"] if data.get("probe_minutes") else 0   # ping + traceroute, first collector
    if snaps > 40 or snaps + probes > 45:
        problems.append(f"That schedule would use about {snaps:.0f} Route Views connections per collector per day "
                        f"({snaps + probes:.0f} on the first collector, which also runs the probes). Route Views "
                        f"allows 100 and bgpcanary keeps scheduled use under 50, so use longer intervals (e.g. "
                        f"snapshots every 240 min and probes every 120 min)")
    if problems:
        return RedirectResponse("/settings/advanced?err=1&msg=" + quote("; ".join(problems)), status_code=303)
    db.kv_set(svc.aconn, config.TUNING_KV, data)
    svc.aconn.commit()
    log.info("user %s changed detection/data-source settings: %s", who, data)
    _restart_soon()
    return RedirectResponse("/restarting?" + urlencode({"since": STARTED}), status_code=303)


@app.post("/settings/advanced/reset")
async def advanced_settings_reset(request: Request):
    who = admin(request)
    svc.aconn.execute("DELETE FROM kv WHERE k=?", (config.TUNING_KV,))
    svc.aconn.commit()
    log.info("user %s reset detection/data-source settings to the config-file defaults", who)
    _restart_soon()
    return RedirectResponse("/restarting?" + urlencode({"since": STARTED}), status_code=303)


# ---------------------------------------------------------------------- alert channel settings (admins)
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def _mask_url(u):
    """Show enough of a webhook URL to recognise it without exposing the secret token."""
    try:
        p = urlsplit(u)
        return f"{p.scheme}://{p.netloc}/…{u[-4:]}" if len(u) > 12 else "…"
    except Exception:
        return "…"


def _save_alert_settings(d, who, what):
    db.kv_set(svc.aconn, config.ALERT_KV, d)
    svc.aconn.commit()
    config.apply_alert_settings(cfg, d)
    log.info("user %s changed alert settings: %s", who, what)


def _alerts_redirect(msg, err=False, anchor=""):
    return RedirectResponse(f"/settings/alerts?msg={quote(msg)}" + ("&err=1" if err else "") + anchor,
                            status_code=303)


_TUNING = {t[0]: t for t in config.TUNING}
_MODE_LABEL = {"on": "All", "warning": "Warning and up", "critical": "Critical only", "off": "Off"}


def _alert_types(form=None):
    """Rows for "What to alert on": every alert type, its notify choice, its own settings and its check toggle.
    `form`: submitted values to show again after an input error."""
    since = time.time() - 30 * 86400
    fired = {r["kind"]: r["n"] for r in svc.aconn.execute(
        "SELECT kind, COUNT(*) n FROM events WHERE start_ts>=? AND historical=0 GROUP BY kind", (since,))}
    checks = {"transit": transit.enabled(cfg, svc.aconn), "registry": registry.enabled(cfg, svc.aconn)}
    rows, groups = [], []
    for t in config.ALERT_TYPES:
        sevs = t["sev"]
        opts = ["on"] + [m for m in ("warning", "critical") if len(sevs) > 1 and m in sevs and m != min(
            sevs, key=lambda s: config._SEV[s])] + ["off"]
        mode = form.get("mode_" + t["kind"]) if form else config.alert_mode(cfg, t["kind"])
        tun = []
        for attr in t["tuning"]:
            a, _sec, label, kind, o = _TUNING[attr]
            val = form.get("t_" + attr, "") if form else getattr(cfg, attr)
            tun.append({"attr": a, "label": label, "kind": kind, "opts": o, "value": val})
        if t["group"] not in groups:
            groups.append(t["group"])
        rows.append({**t, "mode": mode, "modes": [(m, _MODE_LABEL[m]) for m in opts], "tuning": tun,
                     "fired": fired.get(t["kind"], 0),
                     "check_on": (form.get("check_" + t["check"]) == "1" if form else checks[t["check"]])
                     if t.get("check") else None})
    return groups, rows


@app.get("/settings/alerts", response_class=HTMLResponse)
async def alert_settings_page(request: Request, msg: str = "", err: int = 0, form=None):
    admin(request)
    d = config.alert_settings_dict(cfg)
    hooks = [{"i": i, "name": w["name"], "format": w["format"], "enabled": w["enabled"], "masked": _mask_url(w["url"])}
             for i, w in enumerate(d["webhooks"])]
    em = dict(d["email"])
    em["has_password"] = bool(em.pop("password"))
    em["security"] = "ssl" if em["ssl"] else ("starttls" if em["starttls"] else "none")
    groups, types = _alert_types(form)
    return render(request, "alert_settings.html", msg=msg, err=err, general=d, email=em, hooks=hooks,
                  from_db=db.kv_get(svc.aconn, config.ALERT_KV) is not None, groups=groups, types=types,
                  atlas_on=atlas.enabled(svc.aconn), kinds_saved=bool(cfg.alert_kinds))


@app.post("/settings/alerts/types", response_class=HTMLResponse)
async def alert_settings_types(request: Request):
    """Save "What to alert on": per-type notify choice, the settings that shape each type, and the on/off state
    of the background checks. Applies at once (no restart)."""
    who = admin(request)
    f = await request.form()
    kinds, tuning, problems = {}, {}, []
    for t in config.ALERT_TYPES:
        m = f.get("mode_" + t["kind"], "on")
        kinds[t["kind"]] = m if m in config.ALERT_MODES else "on"
        for attr in t["tuning"]:
            a, _sec, label, kind, o = _TUNING[attr]
            try:
                tuning[a] = config.coerce_tuning(a, kind, o, f.get("t_" + a, ""))
            except (ValueError, TypeError) as ex:
                problems.append(f"{label}: {ex}")
    if tuning.get("recover_threshold_pct", 100) < tuning.get("outage_threshold_pct", 0):
        problems.append("The recovery level must be at or above the outage threshold")
    if problems:   # show the form again with everything that was typed
        return await alert_settings_page(request, "Not saved: " + "; ".join(problems), 1, form=f)
    d = config.alert_settings_dict(cfg)
    d["kinds"] = kinds
    _save_alert_settings(d, who, "alert types: " + ", ".join(f"{k}={v}" for k, v in kinds.items()))
    saved = dict(db.kv_get(svc.aconn, config.TUNING_KV, {}) or {})
    saved.update(tuning)
    db.kv_set(svc.aconn, config.TUNING_KV, saved)
    svc.aconn.commit()
    config.apply_tuning(cfg, saved)   # these thresholds are read live; no restart needed
    for name, mod, run in (("transit", transit, svc.transit), ("registry", registry, svc.registry)):
        want = f.get("check_" + name) == "1"
        if want != mod.enabled(cfg, svc.aconn):
            db.kv_set(svc.aconn, mod.KV_TOGGLE, want)
            svc.aconn.commit()
            if want:
                asyncio.create_task(run.run())
            elif name == "transit":
                run.close_all("turned off")
            else:
                run.close_all()
    n_on = sum(1 for v in kinds.values() if v != "off")
    return _alerts_redirect(f"Saved: {n_on} of {len(kinds)} alert types send notifications.", anchor="#types")


@app.post("/settings/alerts/general")
async def alert_settings_general(request: Request, min_severity: str = Form(""), base_url: str = Form("")):
    who = admin(request)
    min_severity = min_severity or cfg.alert_min_severity   # now set per alert type under "What to alert on"
    if min_severity not in ("info", "warning", "critical"):
        return _alerts_redirect("Unknown severity", True)
    base_url = base_url.strip().rstrip("/")
    if base_url and not re.match(r"^https?://[^\s/]+", base_url):
        return _alerts_redirect("The site URL must start with https:// (or http://)", True)
    d = config.alert_settings_dict(cfg)
    d["min_severity"], d["base_url"] = min_severity, base_url
    _save_alert_settings(d, who, f"min_severity={min_severity} base_url={base_url}")
    return _alerts_redirect("General alert settings saved")


@app.post("/settings/alerts/email")
async def alert_settings_email(request: Request, enabled: str = Form(""), smtp_host: str = Form(""),
                               smtp_port: str = Form("587"), security: str = Form("starttls"),
                               username: str = Form(""), password: str = Form(""), clear_password: str = Form(""),
                               sender: str = Form(""), recipients: str = Form("")):
    who = admin(request)
    to = [x.strip() for x in re.split(r"[,;\s]+", recipients) if x.strip()]
    try:
        port = int(smtp_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        return _alerts_redirect("SMTP port must be a number between 1 and 65535", True, "#email")
    bad = [x for x in to + ([sender.strip()] if sender.strip() else []) if not EMAIL_RE.match(x)]
    if bad:
        return _alerts_redirect(f"Not a valid email address: {', '.join(bad)}", True, "#email")
    if enabled and (not smtp_host.strip() or not to or not sender.strip()):
        return _alerts_redirect("To turn email on, fill in the SMTP server, the From address and at least one "
                                "recipient", True, "#email")
    if security not in ("starttls", "ssl", "none"):
        return _alerts_redirect("Unknown security mode", True, "#email")
    d = config.alert_settings_dict(cfg)
    old_pw = d["email"].get("password", "")
    d["email"] = {"enabled": bool(enabled), "smtp_host": smtp_host.strip(), "smtp_port": port,
                  "starttls": security == "starttls", "ssl": security == "ssl", "username": username.strip(),
                  "password": "" if clear_password else (password if password else old_pw),
                  "from": sender.strip(), "to": to}
    _save_alert_settings(d, who, f"email enabled={bool(enabled)} host={smtp_host.strip()}:{port} to={to}")
    return _alerts_redirect("Email settings saved" + ("" if enabled else " (email alerts are off)"), anchor="#email")


def _check_hook(name, fmt, url, require_url=True):
    if fmt not in ("slack", "teams", "json"):
        return "Format must be slack, teams or json"
    if (url or require_url) and not re.match(r"^https?://[^\s/]+/\S*$", url or ""):
        return "The webhook URL must start with https://"
    if len(name) > 40:
        return "Keep the name under 40 characters"
    return None


@app.post("/settings/alerts/webhooks/add")
async def alert_webhook_add(request: Request, name: str = Form(""), format: str = Form("slack"),
                            url: str = Form(...)):
    who = admin(request)
    name, url = name.strip(), url.strip()
    problem = _check_hook(name, format, url)
    if problem:
        return _alerts_redirect(problem, True, "#webhooks")
    d = config.alert_settings_dict(cfg)
    d["webhooks"].append({"name": name, "format": format, "url": url, "enabled": True})
    _save_alert_settings(d, who, f"added webhook {name or format}")
    return _alerts_redirect(f"Webhook {name or format} added. Click Test to send a sample message.",
                            anchor="#webhooks")


@app.post("/settings/alerts/webhooks/{i}/update")
async def alert_webhook_update(request: Request, i: int, name: str = Form(""), format: str = Form("slack"),
                               url: str = Form(""), enabled: str = Form("")):
    who = admin(request)
    d = config.alert_settings_dict(cfg)
    if not 0 <= i < len(d["webhooks"]):
        return _alerts_redirect("Unknown webhook", True, "#webhooks")
    name, url = name.strip(), url.strip()
    problem = _check_hook(name, format, url, require_url=False)
    if problem:
        return _alerts_redirect(problem, True, "#webhooks")
    w = d["webhooks"][i]
    w.update({"name": name, "format": format, "enabled": bool(enabled)})
    if url:
        w["url"] = url
    _save_alert_settings(d, who, f"updated webhook {name or format} enabled={bool(enabled)}")
    return _alerts_redirect(f"Webhook {name or format} saved", anchor="#webhooks")


@app.post("/settings/alerts/webhooks/{i}/delete")
async def alert_webhook_delete(request: Request, i: int):
    who = admin(request)
    d = config.alert_settings_dict(cfg)
    if not 0 <= i < len(d["webhooks"]):
        return _alerts_redirect("Unknown webhook", True, "#webhooks")
    w = d["webhooks"].pop(i)
    _save_alert_settings(d, who, f"deleted webhook {w['name'] or w['format']}")
    return _alerts_redirect(f"Webhook {w['name'] or w['format']} deleted", anchor="#webhooks")


@app.post("/settings/alerts/test")
async def alert_settings_test(request: Request, channel: str = Form(...)):
    who = admin(request)
    ev = {"id": None, "kind": "test", "severity": "info", "prefix": ", ".join(cfg.prefixes), "start_ts": time.time(),
          "end_ts": None, "title": f"Test alert from {cfg.site_name} (sent by {who})"}
    res = await alerts.send(cfg, svc.aconn, ev, "test", only=channel)
    anchor = "#email" if channel == "email" else "#webhooks"
    if not res:
        return _alerts_redirect("Nothing was sent. For email, add at least one recipient first.", True, anchor)
    ch, ok, e = res[0]
    return _alerts_redirect(f"{ch}: test sent OK" if ok else f"{ch}: FAILED - {e}", not ok, anchor)


@app.post("/settings/test-alert")
async def test_alert(request: Request):
    admin(request)
    ev = {"id": None, "kind": "test", "severity": "info", "prefix": ", ".join(cfg.prefixes), "start_ts": time.time(),
          "end_ts": None, "title": f"Test alert from {cfg.site_name} (requested by {request.session['user']})"}
    res = await alerts.send(cfg, svc.aconn, ev, "test")
    if not res:
        msg = "No alert channels are configured"
    else:
        msg = "; ".join(f"{c}: {'OK' if ok else 'FAILED - ' + (err or '')}" for c, ok, err in res)
    return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)


@app.get("/account/password", response_class=HTMLResponse)
async def account_password(request: Request, msg: str = ""):
    u = user(request, allow_must_change=True)
    forced = bool(auth.get_user(svc.aconn, u)["must_change"])
    return render(request, "password.html", msg=msg, forced=forced)


@app.post("/settings/password")
async def change_password(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...),
                          back: str = Form("/settings")):
    u = user(request, allow_must_change=True)
    back = back if back in ("/settings", "/account/password") else "/settings"
    if not auth.check_user(svc.aconn, u, current):
        return RedirectResponse(f"{back}?msg=" + quote("Current password is wrong"), status_code=303)
    if new != confirm or len(new) < auth.MIN_PASSWORD:
        return RedirectResponse(f"{back}?msg=" + quote(f"New passwords must match and be at least "
                                                       f"{auth.MIN_PASSWORD} characters"), status_code=303)
    if new == current:
        return RedirectResponse(f"{back}?msg=" + quote("Choose a password different from the current one"),
                                status_code=303)
    auth.set_user(svc.aconn, u, new)   # keeps role; clears must_change
    if back == "/account/password":
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/settings?msg=" + quote("Password changed"), status_code=303)


# ---------------------------------------------------------------------- user management (admins)
@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, msg: str = "", err: int = 0):
    me = admin(request)
    return render(request, "users.html", users=auth.list_users(svc.aconn), me=me, msg=msg, err=err,
                  min_pw=auth.MIN_PASSWORD)


def _users_redirect(msg, err=False):
    return RedirectResponse(f"/users?msg={quote(msg)}" + ("&err=1" if err else ""), status_code=303)


@app.post("/users/add")
async def users_add(request: Request, username: str = Form(...), password: str = Form(...),
                    role: str = Form("viewer"), must_change: str = Form("")):
    me = admin(request)
    username = username.strip()
    if not auth.USERNAME_RE.match(username):
        return _users_redirect("Usernames are 2-64 characters: letters, digits, . _ - @", True)
    if auth.get_user(svc.aconn, username):
        return _users_redirect(f"User {username} already exists", True)
    if role not in auth.ROLES:
        return _users_redirect("Unknown role", True)
    if len(password) < auth.MIN_PASSWORD:
        return _users_redirect(f"Passwords must be at least {auth.MIN_PASSWORD} characters", True)
    auth.set_user(svc.aconn, username, password, role=role, must_change=1 if must_change else 0)
    log.info("user %s added user %s (%s)", me, username, role)
    return _users_redirect(f"Added {username} as {role}")


@app.post("/users/{username}/role")
async def users_role(request: Request, username: str, role: str = Form(...)):
    me = admin(request)
    target = auth.get_user(svc.aconn, username)
    if not target or role not in auth.ROLES:
        return _users_redirect("Unknown user or role", True)
    if target["role"] == "admin" and role != "admin" and auth.admin_count(svc.aconn) <= 1:
        return _users_redirect("Can't demote the last admin. Make another user an admin first.", True)
    auth.set_role(svc.aconn, username, role)
    log.info("user %s set role of %s to %s", me, username, role)
    return _users_redirect(f"{username} is now {'an admin' if role == 'admin' else 'a viewer (read-only)'}")


@app.post("/users/{username}/reset")
async def users_reset(request: Request, username: str, password: str = Form(...), must_change: str = Form("")):
    me = admin(request)
    if not auth.get_user(svc.aconn, username):
        return _users_redirect("Unknown user", True)
    if len(password) < auth.MIN_PASSWORD:
        return _users_redirect(f"Passwords must be at least {auth.MIN_PASSWORD} characters", True)
    auth.set_user(svc.aconn, username, password, must_change=1 if must_change else 0)
    log.info("user %s reset the password of %s", me, username)
    return _users_redirect(f"Password reset for {username}" + (" (must change at next login)" if must_change else ""))


@app.post("/users/{username}/delete")
async def users_delete(request: Request, username: str):
    me = admin(request)
    target = auth.get_user(svc.aconn, username)
    if not target:
        return _users_redirect("Unknown user", True)
    if username == me:
        return _users_redirect("You can't delete your own account", True)
    if target["role"] == "admin" and auth.admin_count(svc.aconn) <= 1:
        return _users_redirect("Can't delete the last admin", True)
    auth.delete_user(svc.aconn, username)
    log.info("user %s deleted user %s", me, username)
    return _users_redirect(f"Deleted {username}")


# ---------------------------------------------------------------------- RIPE Atlas (optional)
def _pctl(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo = int(k)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (k - lo)


def _atlas_targets(conn):
    """Targets with stored results: your own first, the public sample last."""
    rows = conn.execute("SELECT target, MAX(purpose='demo') demo, MAX(purpose='baseline') base, MAX(created) c "
                        "FROM atlas_msm GROUP BY target ORDER BY demo, base DESC, c DESC").fetchall()
    return [{"target": r["target"], "demo": bool(r["demo"]), "baseline": bool(r["base"]),
             "label": svc.atlas.label(r["target"]), "group": svc.atlas.group(r["target"])} for r in rows]


def atlas_payload(conn, target, start, end, msm=None, bins=96, nbrs=None):
    """Everything the Atlas page draws: latest traceroute per probe (radial view + carrier grouping),
    binned ping latency/loss per probe (trend charts), per-probe reachability counts (scorecard)."""
    msms = conn.execute("SELECT * FROM atlas_msm WHERE target=?", (target,)).fetchall()
    empty = {"target": target, "probes": {}, "traces": [], "pings": {}, "names": {}, "start": start, "end": end,
             "bin": 60, "my_asn": cfg.asn, "upstreams": cfg.expected_upstreams}
    if not msms:
        return empty
    ids = [m["id"] for m in msms]
    q = ",".join("?" * len(ids))
    my_ups = set(cfg.expected_upstreams)
    nbrs = {u: {a for a, _p in v} for u, v in (nbrs or {}).items()}
    row = conn.execute("SELECT asn FROM ip_asn WHERE ip=?", (target,)).fetchone()
    dest = row["asn"] if row and row["asn"] else None
    if not dest:   # a host name: the network the reached traceroutes end in
        row = conn.execute(f"SELECT last_asn, COUNT(*) n FROM atlas_result WHERE msm_id IN ({q}) AND reached=1 "
                           f"AND last_asn IS NOT NULL GROUP BY last_asn ORDER BY n DESC LIMIT 1", ids).fetchone()
        dest = row["last_asn"] if row else cfg.asn
    tr_ids = [msm] if msm else [m["id"] for m in msms if m["kind"] == "traceroute"]
    # traceroutes: latest per probe in the window; reach counts over the whole window
    traces, reach = {}, {}
    if tr_ids:
        tq = ",".join("?" * len(tr_ids))
        for r in conn.execute(f"SELECT prb_id, ts, reached, hops, path_asns FROM atlas_result WHERE kind='traceroute' "
                              f"AND msm_id IN ({tq}) AND ts>=? AND ts<=? ORDER BY ts", tr_ids + [start, end]):
            n, ok = reach.get(r["prb_id"], (0, 0))
            reach[r["prb_id"]] = (n + 1, ok + (r["reached"] or 0))
            traces[r["prb_id"]] = r
    out_tr, asns = [], {dest}
    for pid, r in traces.items():
        path = json.loads(r["path_asns"] or "[]")
        asns.update(path)
        inferred = False
        if dest in path:
            i = path.index(dest)
            up = path[i - 1] if i >= 1 else None       # network handing traffic to the destination (your upstream)
            carrier = path[i - 2] if i >= 2 else up    # the network in front of that (the upstream's transit)
            if dest == cfg.asn and up is not None and up not in my_ups and nbrs:
                # Your upstream's routers often answer traceroute from addresses its transit numbered the link
                # with, so the upstream itself never shows up. If the last network seen is a transit/peer of
                # exactly one of your upstreams (from BGP), that upstream is where the traffic went.
                cands = [u for u in my_ups if up in nbrs.get(u, ())]
                if len(cands) == 1:
                    up, carrier, inferred = cands[0], up, True
                else:
                    carrier, up = up, None
        else:
            up = carrier = None
        hops = json.loads(r["hops"] or "[]")
        for i in range(1, len(hops)):   # Atlas numbers a late reply from the target "hop 255"
            if hops[i]["h"] == 255:
                hops[i]["h"] = hops[i - 1]["h"] + 1
        asns.add(up)
        out_tr.append({"prb": pid, "ts": r["ts"], "reached": r["reached"], "hops": hops, "inferred": inferred,
                       "path": path, "up": up, "carrier": carrier, "last_asn": path[-1] if path else None})
    # pings: per probe, per time bin -> [bin_ts, min, median, max, sent, rcvd]
    width = max(60, (end - start) / bins)
    acc = {}
    for r in conn.execute(f"SELECT prb_id, ts, sent, rcvd, rtt_min, rtt_avg, rtt_max FROM atlas_result "
                          f"WHERE kind='ping' AND msm_id IN ({q}) AND ts>=? AND ts<=?", ids + [start, end]):
        b = acc.setdefault(r["prb_id"], {}).setdefault(int((r["ts"] - start) // width), [[], None, None, 0, 0])
        if r["rtt_avg"] is not None:
            b[0].append(r["rtt_avg"])
            b[1] = r["rtt_min"] if b[1] is None else min(b[1], r["rtt_min"])
            b[2] = r["rtt_max"] if b[2] is None else max(b[2], r["rtt_max"])
        b[3] += r["sent"] or 0
        b[4] += r["rcvd"] or 0
    pings = {pid: [[round(start + (k + .5) * width), b[1], _pctl(b[0], .5), b[2], b[3], b[4]]
                   for k, b in sorted(bs.items())] for pid, bs in acc.items()}
    pids = set(traces) | set(acc)
    probes = {}
    if pids:
        pq = ",".join("?" * len(pids))
        for p in conn.execute(f"SELECT * FROM atlas_probe WHERE prb_id IN ({pq})", list(pids)):
            n, ok = reach.get(p["prb_id"], (0, 0))
            probes[p["prb_id"]] = {"asn": p["asn"], "cc": p["cc"], "region": atlas.CONTINENT.get(p["cc"] or "", "Other"),
                                   "runs": n, "ok": ok}
            asns.add(p["asn"])
    empty.update({"label": svc.atlas.label(target), "group": svc.atlas.group(target), "dest_asn": dest, "bin": width, "demo": any(m["purpose"] == "demo" for m in msms),
                  "probes": probes, "traces": out_tr, "pings": pings,
                  "names": {str(k): v for k, v in names(conn, [a for a in asns if a]).items()}})
    return empty


@app.get("/atlas", response_class=HTMLResponse)
async def atlas_page(request: Request, target: str = "", hours: float = 24, start: float = None, end: float = None,
                     msm: int = None, msg: str = "", err: int = 0, compare: str = ""):
    user(request)
    conn = rconn()
    targets = _atlas_targets(conn)
    known = {t["target"] for t in targets}
    selected = [t for t in dict.fromkeys(compare.split(",")) if t in known] if compare else []
    if len(selected) < 2:
        selected = []
    if not target and targets:
        target = selected[0] if selected else targets[0]["target"]
    groups = sorted({t["group"] for t in targets if t["group"] and t["baseline"]})
    m = conn.execute("SELECT m.*, e.title FROM atlas_msm m LEFT JOIN events e ON e.id=m.event_id WHERE m.id=?",
                     (msm,)).fetchone() if msm else None
    recent = conn.execute("SELECT m.*, e.title FROM atlas_msm m LEFT JOIN events e ON e.id=m.event_id "
                          "WHERE m.purpose!='demo' ORDER BY m.created DESC LIMIT 15").fetchall()
    conn.close()
    s = atlas.settings(svc.aconn)
    return render(request, "atlas.html", targets=targets, target=target, hours=hours, start=start, end=end,
                  compare=selected, groups=groups,
                  msm=m, recent=recent, on=atlas.enabled(svc.aconn), has_key=bool(s["api_key"]), msg=msg, err=err,
                  last_error=svc.atlas.last_error, credits=await svc.atlas.credits() if s["api_key"] else None,
                  committed=round(svc.atlas._spent_today()), credit_cap=s["credit_cap"], outage_probes=s["outage_probes"])


@app.get("/api/atlas/compare")
async def api_atlas_compare(request: Request, targets: str, start: float = None, end: float = None,
                            hours: float = 24):
    """Several targets at once (same window), for the side-by-side view."""
    user(request)
    end = end or time.time()
    start = start or end - hours * 3600
    nbrs = await tree.upstream_neighbors(svc.sourceapp, sorted(set(cfg.expected_upstreams)))
    conn = rconn()
    try:
        return {"start": start, "end": end,
                "items": [atlas_payload(conn, t, start, end, nbrs=nbrs) for t in list(dict.fromkeys(targets.split(",")))[:20] if t]}
    finally:
        conn.close()


@app.get("/api/atlas")
async def api_atlas(request: Request, target: str, start: float = None, end: float = None, hours: float = 24,
                    msm: int = None):
    user(request)
    end = end or time.time()
    start = start or end - hours * 3600
    nbrs = await tree.upstream_neighbors(svc.sourceapp, sorted(set(cfg.expected_upstreams)))
    conn = rconn()
    try:
        return atlas_payload(conn, target, start, end, msm, nbrs=nbrs)
    finally:
        conn.close()


@app.post("/atlas/trace")
async def atlas_trace_now(request: Request):
    """On-demand traceroute burst (same probe mix as an outage burst)."""
    me = admin(request)
    if not atlas.enabled(svc.aconn):
        return RedirectResponse("/atlas?err=1&msg=" + quote("RIPE Atlas is off (Settings > RIPE Atlas)."), status_code=303)
    try:
        msm = await svc.atlas.burst("manual", description=f"bgpcanary manual traceroute ({me})")
    except Exception as ex:
        return RedirectResponse("/atlas?err=1&msg=" + quote(f"Traceroute not started: {ex}"), status_code=303)
    log.info("user %s started Atlas traceroute burst %s", me, msm)
    return RedirectResponse(f"/atlas?target={quote(svc.atlas.target())}&msm={msm}&msg=" + quote(
        "Traceroute started. Results arrive over the next few minutes; reload to see more probes fill in."),
        status_code=303)


@app.post("/atlas/adhoc")
async def atlas_adhoc(request: Request, target: str = Form(""), kind: str = Form("both"), source: str = Form("world"),
                      value: str = Form(""), probes: int = Form(20)):
    """One-off test toward any IP / host name, from probes worldwide, in a network, or in a country."""
    me = admin(request)

    def back(msg):
        return RedirectResponse("/atlas?err=1&msg=" + quote(msg), status_code=303)
    if not atlas.enabled(svc.aconn):
        return back("RIPE Atlas is off (Settings > RIPE Atlas).")
    try:
        t = atlas.check_target(target)
    except ValueError as ex:
        return back(str(ex))
    n = max(1, min(int(probes or 20), 100))
    kinds = {"ping": ["ping"], "traceroute": ["traceroute"]}.get(kind, ["ping", "traceroute"])
    try:
        if source == "asn":
            v = value.strip().upper().removeprefix("AS")
            if not v.isdigit():
                return back("Enter a network number (ASN), e.g. 3356")
            ids = await svc.atlas.probes_in(n, asn=int(v))
        elif source == "country":
            v = value.strip()
            if not re.fullmatch(r"[A-Za-z]{2}", v):
                return back("Enter a two-letter country code, e.g. US")
            ids = await svc.atlas.probes_in(n, country=v)
        else:
            ids = await svc.atlas.pick_probes(n, 0, [])
        if not ids:
            return back("No connected probes found there.")
        made = await svc.atlas.adhoc(t, kinds, ids[:n], me)
    except Exception as ex:
        return back(f"Test not started: {ex}")
    log.info("user %s started Atlas ad-hoc %s to %s from %d probes: %s", me, kind, t, len(ids[:n]), made)
    now = int(time.time())
    return RedirectResponse(f"/atlas?target={quote(t)}&start={now - 120}&end={now + 3600}&msg=" + quote(
        f"Started {' and '.join(kinds)} to {t} from {len(ids[:n])} probes. Results arrive over the next few minutes; "
        "reload to see them fill in."), status_code=303)


@app.post("/atlas/demo")
async def atlas_demo(request: Request, action: str = Form("load")):
    admin(request)
    if action == "clear":
        svc.atlas.clear_demo()
        return RedirectResponse("/atlas?msg=" + quote("Sample data removed."), status_code=303)
    try:
        await svc.atlas.load_demo()
    except Exception as ex:
        return RedirectResponse("/atlas?err=1&msg=" + quote(f"Couldn't load the sample data: {ex}"), status_code=303)
    return RedirectResponse(f"/atlas?target={atlas.DEMO_TARGET}&msg=" + quote(
        "Loaded the last 24 hours of RIPE Atlas' public measurements toward k.root-servers.net from about 30 probes "
        "worldwide, so you can try the views. It's sample data, not yours."), status_code=303)


def _atlas_estimate(s):
    """Daily credit use for the chosen settings. Uses the real baseline probe count when the probe settings
    match the running baseline, else assumes ~8 upstream transit carriers."""
    st = db.kv_get(svc.aconn, atlas.KV_STATE, {}) or {}
    sig = st.get("baseline_probes_sig") or {}
    real = len(st.get("baseline_probes") or [])
    same = real and sig.get("probes") == s["baseline_probes"] and sig.get("carrier_probes") == s["carrier_probes"]
    n = real if same else s["baseline_probes"] + s["carrier_probes"] * 8
    k = max(1, len(s.get("targets") or cfg.probe_targets))   # one ping + one traceroute per baseline target
    ping = k * n * 1440 / max(1, s["ping_minutes"]) * atlas.COST["ping"] if s["baseline_ping"] else 0
    trace = k * n * 1440 / max(1, s["trace_minutes"]) * atlas.COST["traceroute"] if s["baseline_trace"] else 0
    burst = s["outage_probes"] * atlas.COST["traceroute"] * 2 if s["outage_trace"] else 0
    return {"probes": n, "real": bool(same), "targets": k, "ping": round(ping), "trace": round(trace),
            "burst": round(burst), "oneoff": round(svc.atlas.spent_oneoff())}


@app.get("/settings/atlas", response_class=HTMLResponse)
async def atlas_settings_page(request: Request, msg: str = "", err: int = 0):
    admin(request)
    return await _atlas_settings(request, msg, err)


async def _atlas_settings(request, msg="", err=0, checks=None, form=None):
    """The settings page. `form` = submitted values to show again after an input error, so nothing typed is lost."""
    s = dict(atlas.settings(svc.aconn))
    has_key = bool(s.pop("api_key"))
    est = _atlas_estimate(s)
    targets_text = None
    if form is not None:
        for k in ("credit_cap", "baseline_probes", "carrier_probes", "ping_minutes", "trace_minutes", "outage_probes"):
            s[k] = form.get(k, "")
        for k in ("enabled", "baseline_ping", "baseline_trace", "outage_trace"):
            s[k] = form.get(k) == "1"
        s["my_probes"] = _parse_list(form.get("my_probes", ""))
        targets_text = form.get("targets", "")
    credits = await svc.atlas.credits() if has_key else None
    return render(request, "atlas_settings.html", msg=msg, err=err, a=s, has_key=has_key, credits=credits,
                  probe=await atlas.local_probe(), my_probes=await svc.atlas.my_probes(),
                  est=est, spent=round(svc.atlas._spent_today()), targets_text=targets_text,
                  state=db.kv_get(svc.aconn, atlas.KV_STATE, {}) or {}, target=svc.atlas.target(),
                  targets=svc.atlas.targets(), suggested=atlas.suggest_targets(cfg),
                  last_error=svc.atlas.last_error, checks=checks)


@app.post("/settings/atlas/test", response_class=HTMLResponse)
async def atlas_settings_test(request: Request, api_key: str = Form("")):
    """Run the self-test with the key typed in the form, or the saved key. Saves nothing."""
    me = admin(request)
    checks = await svc.atlas.self_test(api_key.strip() or None)
    log.info("user %s ran the RIPE Atlas self-test: %s", me, ", ".join(f"{c}={ok}" for c, ok, _ in checks))
    return await _atlas_settings(request, checks=checks)


@app.post("/settings/atlas", response_class=HTMLResponse)
async def atlas_settings_save(request: Request):
    me = admin(request)
    f = await request.form()
    s = atlas.settings(svc.aconn)
    was_on = atlas.enabled(svc.aconn)

    async def again(msg):   # input error: show the form again with what was typed
        return await _atlas_settings(request, f"Not saved: {msg}", 1, form=f)

    def num(k, lo, hi):
        try:
            v = int(f.get(k) or s[k])
        except ValueError:
            raise ValueError(f"{k.replace('_', ' ')} must be a whole number")
        if not lo <= v <= hi:
            raise ValueError(f"{k.replace('_', ' ')} must be between {lo:,} and {hi:,}")
        return v
    try:
        s.update({"enabled": f.get("enabled") == "1",
                  "baseline_ping": f.get("baseline_ping") == "1", "baseline_trace": f.get("baseline_trace") == "1",
                  "outage_trace": f.get("outage_trace") == "1",
                  "credit_cap": num("credit_cap", 100, 10_000_000), "baseline_probes": num("baseline_probes", 1, 200),
                  "carrier_probes": num("carrier_probes", 0, 10), "ping_minutes": num("ping_minutes", 4, 1440),
                  "trace_minutes": num("trace_minutes", 15, 10080), "outage_probes": num("outage_probes", 5, 500)})
        try:
            s["my_probes"] = sorted({int(x) for x in _parse_list(f.get("my_probes", ""))})
        except ValueError:
            raise ValueError("probe IDs must be numbers")
        s["targets"], s["target_labels"], s["target_groups"] = atlas.parse_targets(f.get("targets", ""))
        if len(s["targets"]) > 20:
            raise ValueError("at most 20 baseline targets")
    except ValueError as ex:
        return await again(str(ex))
    key = (f.get("api_key") or "").strip()
    if f.get("clear_key") == "1":
        s["api_key"] = ""
    elif key:
        s["api_key"] = key
    if s["enabled"] and not s["api_key"]:
        return await again("add an API key to turn RIPE Atlas on.")
    db.kv_set(svc.aconn, atlas.KV_SETTINGS, s)
    svc.aconn.commit()
    log.info("user %s changed RIPE Atlas settings (enabled=%s)", me, s["enabled"])
    if key or f.get("clear_key") == "1":
        await svc.atlas.credits(force=True)
    err = False
    if was_on and not atlas.enabled(svc.aconn):
        await svc.atlas.disable()
        msg = "RIPE Atlas turned off. The periodic measurements were stopped; stored results are kept."
    elif atlas.enabled(svc.aconn):
        try:
            await svc.atlas.ensure_baseline()
            msg = "Saved. Baseline measurements are scheduled; Atlas spreads the first run over one interval, so " \
                  "allow up to ~30 minutes for new targets' first results."
        except Exception as ex:
            msg, err = f"Saved, but Atlas refused: {ex}", True
        running = set((db.kv_get(svc.aconn, atlas.KV_STATE, {}) or {}).get("baseline_by", {}))
        waiting = [t for t in svc.atlas.targets() if t not in running]
        if waiting:
            est = _atlas_estimate(s)
            fit = -(-(est["ping"] + est["trace"] + est["oneoff"] + est["burst"] * 2) // 5000) * 5000
            msg = (f"Saved. {len(waiting)} of {len(svc.atlas.targets())} baseline targets "
                   f"({', '.join(waiting)}) are waiting: the daily credit cap ({s['credit_cap']:,}) doesn't cover them. "
                   f"Raise it to {fit:,} or more and they start within 5 minutes.")
            err = True
    else:
        msg = "Saved."
    return RedirectResponse(f"/settings/atlas?msg={quote(msg)}" + ("&err=1" if err else ""), status_code=303)
