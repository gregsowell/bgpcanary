"""FastAPI web UI."""
import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadData, URLSafeTimedSerializer
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware

from . import (REPO, __version__, alerts, atlas, auth, config, db, evidence, pathfinder, paths, peering, registry, ripe,
               tracer, transit, tree, updates, watch)
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
# The container image (BGPCANARY_CONTAINER=1) has no bundled nginx in front and no systemd: there the app sends
# nginx's security headers itself, and the pages show docker commands instead of server ones.
CONTAINER = os.environ.get("BGPCANARY_CONTAINER") == "1"


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
templates.env.filters["mask_urls"] = alerts.mask_urls   # older alert-log rows may still hold a webhook URL
templates.env.globals.update(container=CONTAINER, config_path=config.DEFAULT_PATH, image="ghcr.io/" + REPO.lower(),
                             repo=REPO)


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

# The headers the bundled nginx adds (deploy/nginx-bgpcanary.conf: keep the two the same). The app sends them itself
# only in the container image; behind that nginx they'd be sent twice.
SECURITY_HEADERS = [
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' "
     "'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; "
     "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"),
]


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                h = MutableHeaders(scope=message)
                for k, v in SECURITY_HEADERS:
                    h.setdefault(k, v)
                if scope.get("scheme") == "https":   # (as the proxy in front reported it)
                    h.setdefault("Strict-Transport-Security", "max-age=31536000")
            await send(message)

        await self.app(scope, receive, send_with_headers)


if CONTAINER:
    app.add_middleware(SecurityHeaders)


def _origin(url):
    """(scheme, host, port) of an http(s) URL, the default port filled in; ValueError if it isn't one."""
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(url)
    return p.scheme, p.hostname.lower(), p.port or (443 if p.scheme == "https" else 80)


def _our_origins(request):
    """Where this site's own pages are: the address the browser used (scheme from the proxy, Host as the browser sent
    it), any X-Forwarded-Host from a proxy in front, and the configured Site URL."""
    out, scheme = set(), request.url.scheme   # (uvicorn applies X-Forwarded-Proto from nginx)
    for h in (request.headers.get("host"), request.headers.get("x-forwarded-host")):
        if h:
            try:
                out.add(_origin(f"{scheme}://{h.split(',')[0].strip()}"))
            except ValueError:
                pass
    if cfg.base_url:
        try:
            out.add(_origin(cfg.base_url))
        except ValueError:
            pass
    return out


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """CSRF protection on top of SameSite=Strict cookies. A browser always says where a POST comes from (Origin, or at
    least Referer); only this site's own origin (scheme, host and port) may change anything, so a form on another
    site, a sibling subdomain or another port of this host is refused. A POST that carries a session cookie but
    neither header isn't trusted either. Scripts without a session (e.g. curl) pass.
    A proxy that rewrites Host can make real pages look foreign: set the Site URL ([web] base_url) to the address
    people use."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        src = request.headers.get("origin") or request.headers.get("referer")
        if src is None:
            ok = "bgpcanary" not in request.cookies
        else:
            try:
                ok = src != "null" and _origin(src) in _our_origins(request)
            except ValueError:   # malformed: refused, cleanly
                ok = False
        if not ok:
            log.warning("refused a cross-site %s %s (from %s)", request.method, request.url.path,
                        (src or "no Origin/Referer")[:100])
            return PlainTextResponse("Refused: this request didn't come from this site's own pages. If you use a "
                                     "different address or a proxy, set the Site URL ([web] base_url) to it.",
                                     status_code=403)
    return await call_next(request)


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
SETUP_PATHS = ("/settings/monitor", "/restarting", "/account/password", "/settings/password", "/settings/signout-others",
               "/logout", "/users",
               "/settings/alerts")


@app.exception_handler(NeedLogin)
async def _need_login(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login required"}, status_code=401)
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")   # (a shared link keeps its query)
    return RedirectResponse(f"/login?next={quote(here, safe='/')}", status_code=303)


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
    if (not row or request.session.get("v", 0) != row["session_ver"]
            or not auth.session_valid(svc.aconn, u, request.session.get("sid"))):
        # deleted, signed out (here or everywhere), or a password / role change since this session started.
        # Cookies from before 1.2.3 have no session id: they sign in once more.
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


def client_ip(request):
    """The browser's address (login limits, session list, log lines). uvicorn has already taken it from
    X-Forwarded-For if the connection came from a proxy it trusts (--forwarded-allow-ips; in the container,
    BGPCANARY_TRUSTED_PROXIES). X-Real-IP counts only from a proxy on this machine (the bundled nginx), so a client
    that can reach the app directly can't choose its own address."""
    host = request.client.host if request.client else ""
    real = request.headers.get("x-real-ip")
    if real:
        try:
            if ipaddress.ip_address(host).is_loopback:
                return real
        except ValueError:
            pass
    return host


def cookies_need_https(request):
    """True if this page came over plain HTTP from a non-local address while the sign-in cookies are HTTPS-only:
    the browser won't keep them, so signing in would silently fail. The login page says so."""
    if os.environ.get("BGPCANARY_INSECURE_COOKIES") == "1" or request.url.scheme == "https":
        return False
    host = (request.url.hostname or "").lower()
    return host not in ("localhost", "127.0.0.1", "::1") and not host.endswith(".localhost")


def start_session(request, username):
    """A new server-side session for this browser (a fresh id each time: nothing carries over from before)."""
    row = auth.get_user(svc.aconn, username)
    request.session.clear()
    request.session["user"] = username
    request.session["v"] = row["session_ver"] if row else 0
    request.session["sid"] = auth.new_session(svc.aconn, username, client_ip(request),
                                              request.headers.get("user-agent", ""))
    return row


def keep_session(request, username):
    """After changing your own password or role (which ends all of the account's sessions), stay signed in here
    with a new session; every other browser stays signed out."""
    if request.session.get("user") == username:
        start_session(request, username)


def rconn():
    return db.connect(cfg.db_path)


def names(conn, asns):
    asns = [int(a) for a in asns if a is not None and str(a).isdigit()]
    if not asns:
        return {}
    q = ",".join("?" * len(asns))
    return {r["asn"]: r["name"] for r in conn.execute(f"SELECT asn,name FROM asn_names WHERE asn IN ({q})", asns)}


def loading():
    """What the "not live yet" banner shows (templates/_loading.html), or None once bgpcanary is live: the first
    start's history load with its progress and an estimate, finishing it, connecting to RIS Live, starting up, or
    a prefix added later whose history loads while live monitoring runs."""
    st, now = svc.engine.status, time.time()
    h, phase = st.get("history"), st.get("phase") or ""
    if h:
        frac, at = h["done"] / h["total"], h["upto"]
        if h["for"] == "all" and svc.engine.historical and svc.engine.last_bucket:   # where the replay has got to
            at = min(max(svc.engine.last_bucket, h["start"]), h["end"])
            frac = max(frac, (at - h["start"]) / max(h["end"] - h["start"], 1))
        elapsed = now - h["started"]
        eta = elapsed * (1 - frac) / frac if frac >= 0.03 and elapsed >= 30 else None
        return {"what": "history" if h["for"] == "all" else "prefix", "prefix": h["for"],
                "week": min(h["done"] + 1, h["total"]), "weeks": h["total"], "pct": int(frac * 100),
                "days": round((h["end"] - h["start"]) / 86400), "at": at,
                "eta_min": None if eta is None else max(1, round(eta / 60))}
    if phase == "finishing the history load":
        return {"what": "finishing"}
    if phase == "connecting to RIS Live":
        return {"what": "connecting", "slow": now - (st.get("connecting_since") or now) > 120}
    if phase == "starting":
        return {"what": "starting"}
    return None


def render(request, name, **ctx):
    role = getattr(request.state, "role", None)
    ctx.update(request=request, cfg=cfg, user=request.session.get("user"), status=svc.engine.status,
               now=time.time(), role=role, is_admin=role == "admin", version=__version__,
               update=updates.state(svc.aconn) if role == "admin" else None,
               loading=loading() if request.session.get("user") else None,
               health=svc.health.state() if request.session.get("user") else None,
               outage=bool(request.session.get("user")) and any(m.state == "down" for m in svc.engine.mons.values()))
    return templates.TemplateResponse(request, name, ctx)


@app.get("/api/status")
async def api_status(request: Request):
    """For the "not live yet" banner, which polls this: its new content, or html=null once bgpcanary is live."""
    user(request)
    ld = loading()
    return {"phase": svc.engine.status.get("phase"), "what": ld["what"] if ld else None,
            "html": templates.env.get_template("_loading.html").render(ld=ld) if ld else None}


# ---------------------------------------------------------------------- auth
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    return render(request, "login.html", next=next, error=None, need_https=cookies_need_https(request))


_login_slots = asyncio.Semaphore(4)   # password hashing is deliberately expensive: a few at a time
_login_waiting = 0                    # ...and only so many may queue for it; the rest are turned away at once
LOGIN_QUEUE_MAX = 32

# "Known device" cookie: set when a browser signs in, it exempts that browser from the per-account login limit
# (see auth.Throttle), so someone guessing at a known username can't lock its owner out. It isn't a credential:
# it only changes which attempt budget a login uses. Signed with the secret key; sent only to /login.
DEVICE_COOKIE, DEVICE_MAX_AGE = "bgpcanary_device", 180 * 86400
_devices = URLSafeTimedSerializer(cfg.secret_key, salt="bgpcanary-known-device")


def known_device(request, username):
    raw = request.cookies.get(DEVICE_COOKIE) or ""
    if not raw or len(raw) > 512:
        return None
    try:
        d = _devices.loads(raw, max_age=DEVICE_MAX_AGE)
    except BadData:
        return None
    if isinstance(d, dict) and d.get("u") == username and isinstance(d.get("id"), str):
        return d["id"][:32]
    return None


_refusals = {}   # message -> (time, page)


def login_refusal(request, message, status):
    """The login page with `message`, rendered at most every 10 s and reused: under a flood of attempts, turning
    each one away costs next to nothing, so the monitoring keeps running."""
    hit = _refusals.get(message)
    if not hit or time.time() - hit[0] > 10:
        hit = (time.time(), render(request, "login.html", next="/", error=message).body)
        _refusals[message] = hit
    return HTMLResponse(hit[1], status_code=status, headers={"Retry-After": "10"})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    global _login_waiting
    ip = client_ip(request)
    if _login_waiting >= LOGIN_QUEUE_MAX:
        return login_refusal(request, "The server is busy. Try again in a moment.", 503)
    device = known_device(request, username)
    if len(username) > 64 or len(password) > 1024 or not throttle.attempt(ip, username, device):
        if throttle.first_refusal(ip, username):   # once per window, so a flood can't fill the journal
            log.warning("login refused for %r from %s (too many attempts)", username[:64], ip)
        return login_refusal(request, "Too many failed attempts. Try again later.", 429)
    _login_waiting += 1
    try:
        async with _login_slots:
            conn = rconn()
            try:
                ok = await asyncio.to_thread(auth.check_user, conn, username, password)
            finally:
                conn.close()
    finally:
        _login_waiting -= 1
    if not ok:
        log.warning("failed login for %r from %s", username[:64], ip)
        await asyncio.sleep(1)
        return render(request, "login.html", next=next, error="Invalid username or password.",
                      need_https=cookies_need_https(request))
    throttle.success(ip, username, device)
    row = start_session(request, username)
    if not next.startswith("/") or next.startswith(("//", "/\\")):   # (only ever a page on this site)
        next = "/"
    resp = RedirectResponse("/account/password" if row and row["must_change"] else next, status_code=303)
    resp.set_cookie(DEVICE_COOKIE, _devices.dumps({"u": username, "id": device or secrets.token_urlsafe(12)}),
                    max_age=DEVICE_MAX_AGE, path="/login", httponly=True, samesite="strict",
                    secure=os.environ.get("BGPCANARY_INSECURE_COOKIES") != "1")
    return resp


@app.get("/logout")
async def logout(request: Request):
    auth.end_session(svc.aconn, request.session.get("sid"))   # ended on the server: a copied cookie stops working
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------- pages
def split_neighbours(counts):
    """{neighbour: RIS peers reaching you through it} -> (transits and unknown neighbours, most paths first;
    peers by exchange: [{"name", "peers": [(asn, paths), ...], "paths"}], exchanges by name, private peering last)."""
    ups, groups = [], {}
    for u, n in sorted(counts.items(), key=lambda x: -x[1]):
        if u is None:
            continue
        g = svc.engine.peer_group(u)
        if g:
            groups.setdefault(g, []).append((u, n))
        else:
            ups.append((u, n))
    return ups, [{"name": g, "peers": lst, "paths": sum(n for _, n in lst)}
                 for g, lst in sorted(groups.items(), key=lambda kv: (kv[0] == peering.PRIVATE, kv[0].lower()))]


def group_chains(d):
    """{chain: paths}, with every chain that ends at a peer folded into one "peers:<exchange>" entry."""
    out = {}
    for c, n in d.items():
        last = c.rsplit(" ", 1)[-1]
        g = svc.engine.peer_group(int(last)) if last.isdigit() else None
        k = f"peers:{g}" if g else c
        out[k] = out.get(k, 0) + n
    return out


def grouped_chains(cache, s):
    """group_chains of a bucket's chains JSON, worked out once per request for each different one: buckets keep the
    same chains (the same string, see db.fill_mix) until they change. The result is shared: don't change it."""
    if s not in cache:
        cache[s] = group_chains(json.loads(s or "{}"))
    return cache[s]


def chain_label(c, nm):
    if c.startswith("peers:"):
        return "Private peers" if c[6:] == peering.PRIVATE else f"Peers at {c[6:]}"
    return " → ".join(f"AS{x} {nm.get(int(x), '')}".strip() for x in c.split())


def chain_asns(chains):
    return {int(x) for c in chains if not c.startswith("peers:") for x in c.split()}


def prefix_summary(conn):
    out = []
    eng = svc.engine
    for p, m in eng.mons.items():
        v = eng.visible(p)
        base = m.current_baseline()
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
        neighbours, peers = split_neighbours(ups)
        out.append({"prefix": p, "visible": v, "baseline": base, "state": state,
                    "pct": (100 * v / base) if base else None,
                    "chains": sorted(group_chains(chains).items(), key=lambda x: -x[1]),
                    "upstreams": neighbours, "peers": peers,
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
        asns |= chain_asns(c for c, _ in s["chains"])
        asns.update(u for u, _ in s["upstreams"])
        asns.update(u for g in s["peers"] for u, _ in g["peers"])
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
    chains, nb = {}, {}
    for s in summ:
        for c, n in s["chains"]:
            t = chains.setdefault(c, [0, 0])
            t[0] += n
            t[1] += 1
        for u, n in s["upstreams"] + [x for g in s["peers"] for x in g["peers"]]:
            nb[u] = nb.get(u, 0) + n
    c_ups, c_peers = split_neighbours(nb)
    combined = {
        "chains": sorted(((c, n, k) for c, (n, k) in chains.items()), key=lambda x: -x[1]),
        "upstreams": c_ups, "peers": c_peers,
        "missing": [u for u in cfg.expected_upstreams if u not in nb],
        "paths": sum(s["visible"] for s in summ),
        "ok": sum(1 for s in summ if s["state"] == "ok"),
        "worst": min((s for s in summ if s["pct"] is not None), key=lambda s: s["pct"], default=None),
    }
    return render(request, "dashboard.html", summ=summ, all_prefixes=[s["prefix"] for s in summ_all], sel=sel,
                  every=every, combined=combined, events=events, blips=blips, probes=probe, rv=rv, names=nm,
                  rv_latest=rv_latest, atlas_targets=svc.atlas.targets() if has_atlas else [], rvb=rv_budget())


@app.get("/api/series")
async def api_series(request: Request, prefix: str = None, start: float = None, end: float = None):
    user(request)
    prefix = prefix or cfg.prefixes[0]
    end = end or time.time()
    start = start or end - 86400
    conn = rconn()
    rows = db.fill_mix(conn, conn.execute("SELECT ts,visible,min_visible,baseline,chains,updates,withdrawals FROM buckets "
                                          "WHERE prefix=? AND ts>=? AND ts<=? ORDER BY ts", (prefix, start, end)), prefix)
    step = max(1, len(rows) // 1500)
    pts, grouped = [], {}
    for i in range(0, len(rows), step):
        grp = rows[i:i + step]
        low = min(grp, key=lambda r: r["min_visible"] if r["min_visible"] is not None else r["visible"])
        pts.append({"ts": grp[0]["ts"], "visible": min(r["visible"] for r in grp),
                    "min": min((r["min_visible"] if r["min_visible"] is not None else r["visible"]) for r in grp),
                    "max": max(r["visible"] for r in grp),
                    "baseline": grp[-1]["baseline"],
                    "updates": sum(r["updates"] or 0 for r in grp),
                    "withdrawals": sum(r["withdrawals"] or 0 for r in grp),
                    "chains": grouped_chains(grouped, low["chains"])})
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
    nm = names(conn, chain_asns(top))
    conn.close()
    chain_series = {c: [None if p.get("gap") else p["chains"].get(c, 0) for p in pts] for c in top}
    chain_series["other"] = [None if p.get("gap") else sum(v for k, v in p["chains"].items() if k not in top)
                             for p in pts]
    return {"prefix": prefix, "start": start, "end": end, "step": step * cfg.bucket_seconds,
            "ts": [p["ts"] for p in pts], "visible": [p["visible"] for p in pts], "min": [p["min"] for p in pts],
            "baseline": [p["baseline"] for p in pts], "updates": [p["updates"] for p in pts],
            "withdrawals": [p["withdrawals"] for p in pts], "chains": chain_series,
            "chain_labels": {c: chain_label(c, nm) for c in top},
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
    rows = db.fill_mix(conn, conn.execute(f"SELECT prefix,ts,visible,min_visible,baseline,chains,updates,withdrawals "
                                          f"FROM buckets WHERE prefix IN ({q}) AND ts>=? AND ts<=? ORDER BY ts",
                                          (*want, start, end)))
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
    chains, grouped = [dict() for _ in range(nslots)], {}
    for (p, i), r in lowest_row.items():
        for c, n in grouped_chains(grouped, r["chains"]).items():
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
    nm = names(conn, chain_asns(top))
    conn.close()
    return {"prefixes": want, "start": start, "end": end, "step": slot_s,
            "ts": [start + i * slot_s for i in range(nslots)], "lines": lines, "others": others,
            "updates": upd, "withdrawals": wd, "chains": chain_series,
            "chain_labels": {c: chain_label(c, nm) for c in top},
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
async def events_page(request: Request, kind: str = "", severity: str = "", info: int = 0, state: str = "",
                      page: int = 1):
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
    state = state if state in ("open", "closed") else ""   # open: still going on (no end yet)
    if state:
        where.append("end_ts IS NULL" if state == "open" else "end_ts IS NOT NULL")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) c FROM events {w}", args).fetchone()["c"]
    rows = conn.execute(f"SELECT * FROM events {w} ORDER BY start_ts DESC LIMIT 100 OFFSET ?",
                        args + [(page - 1) * 100]).fetchall()
    kinds = [r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM events ORDER BY kind")]
    conn.close()
    return render(request, "events.html", rows=rows, total=total, page=page, kind=kind, severity=severity,
                  info=info, state=state, kinds=kinds, kind_names=KIND_NAMES)


KIND_NAMES = {t["kind"]: t["name"].replace(" (FYI)", "") for t in config.ALERT_TYPES}
KIND_NAMES.update({"tracking_limit": "Tracking limit reached", "registry": "Registry problem"})


def event_neighbour(ev):
    """The neighbour an event is about (unexpected / new upstream, new peer, peer leak), for the page's
    "what is it to you?" buttons; None for other events."""
    d = json.loads(ev["detail"] or "{}")
    if ev["kind"] in ("new_upstream", "new_peer", "peer_leak"):
        return d.get("asn")
    if ev["kind"] == "unexpected_upstream":
        a = (d.get("key") or "").rsplit("|", 1)[-1]
        return int(a) if a.isdigit() else None
    return None


REPLAY_MAX_EVENTS = 50


def replay_events(conn, ids):
    """The events picked to replay together (?id=..&id=..): those with a prefix, oldest first, at most
    REPLAY_MAX_EVENTS."""
    want = [int(i) for i in dict.fromkeys(ids) if str(i).isdigit()][:REPLAY_MAX_EVENTS]
    rows = [conn.execute("SELECT * FROM events WHERE id=?", (i,)).fetchone() for i in want]
    return sorted((r for r in rows if r and r["prefix"]), key=lambda r: r["start_ts"])


@app.get("/events/replay", response_class=HTMLResponse)
async def events_replay_page(request: Request):
    """Several events in one path-tree replay, one after another, with the quiet time between them cut out."""
    user(request)
    conn = rconn()
    try:
        rows = replay_events(conn, request.query_params.getlist("id"))
    finally:
        conn.close()
    if not rows:
        return RedirectResponse("/events", status_code=303)
    return render(request, "events_replay.html", rows=rows, ids=[r["id"] for r in rows],
                  prefixes=list(dict.fromkeys(r["prefix"] for r in rows)))


@app.get("/events/{eid}", response_class=HTMLResponse)
async def event_page(request: Request, eid: int, msg: str = ""):
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
    nb = event_neighbour(ev)
    nb_info = None
    if nb:
        nb_info = {"asn": nb, "name": names(conn, [nb]).get(nb, ""),
                   "role": "transit" if nb in cfg.expected_upstreams else "listed peer" if nb in cfg.peers
                   else "exchange peer" if svc.engine.peer_group(nb) else None,
                   "exchange": svc.peering.label(nb)}
    conn.close()
    return render(request, "event.html", ev=ev, ups=ups, probes=probes, snaps=snaps, alerts=alerts_sent,
                  win_start=ev["start_ts"] - pad, win_end=end + pad, atlas_msms=atlas_msms,
                  atlas_target=base["target"] if base else None, evd=ev_data, compare_all=",".join(svc.atlas.targets()),
                  nb=nb_info, msg=msg)


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




def event_windows(rows, pad=300):
    """The stretches of time around events replayed together: each event with `pad` seconds either side (or 8% of a
    long one, up to 30 minutes), merging any that overlap or are less than 10 minutes apart."""
    now, wins = time.time(), []
    for r in sorted(rows, key=lambda r: r["start_ts"]):
        end = r["end_ts"] or now
        p = min(1800, max(pad, (end - r["start_ts"]) * 0.08))
        s, e = r["start_ts"] - p, min(end + p, now)
        if wins and s - wins[-1][1] <= 600:
            wins[-1][1] = max(wins[-1][1], e)
        else:
            wins.append([s, e])
    return wins


def activity_windows(conn, prefixes, rows, gap=120, margin=45, min_cut=90):
    """The stretches of time worth replaying for events replayed together: a minute around each event, and wherever
    one of `prefixes`' paths changed within 10 minutes of an event, with `margin` seconds around each cluster of
    changes (changes within `gap` seconds of each other belong together). Quiet time between those longer than
    `min_cut` seconds is cut out; shorter pauses are kept, so the replay doesn't stutter."""
    now, keep, stamps = time.time(), [], []
    for r in rows:
        keep.append([r["start_ts"] - 60, min((r["end_ts"] or now) + 60, now)])
    for p in prefixes:
        for s, e in event_windows(rows, pad=600):
            path_of = lambda r: r["path"] if r["kind"] == "A" else None   # noqa: E731
            last = {(r["collector"], r["peer"]): path_of(r) for r in db.state_at(conn, p, s)}
            for r in conn.execute("SELECT ts, collector, peer, kind, path FROM updates WHERE prefix=? AND ts>? "
                                  "AND ts<=? ORDER BY ts, id", (p, s, e)):
                k, path = (r["collector"], r["peer"]), path_of(r)
                if last.get(k) != path:   # (a re-announcement of the same path changes nothing)
                    last[k] = path
                    stamps.append(r["ts"])
    stamps.sort()
    i = 0
    while i < len(stamps):
        j = i
        while j + 1 < len(stamps) and stamps[j + 1] - stamps[j] <= gap:
            j += 1
        keep.append([stamps[i] - margin, stamps[j] + margin])
        i = j + 1
    out = []
    for s, e in sorted(keep):
        e = min(e, now)
        if out and s - out[-1][1] <= min_cut:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def stitched_payload(conn, prefix, wins):
    """replay_payload() over several stretches of time strung together, the quiet time between them cut out (runs in
    a worker thread). Its times are on a "stitched" clock: the first stretch keeps its own, every later one moves back
    by the time cut before it, less a short cut of its own (1% of the whole, 6 to 20 s) where the paths that changed
    in the meantime change all at once. "stitch": the stretches on that clock ([start, end, real start]) and the cuts
    ({"at", "skipped", "from", "to"})."""
    cut = max(6.0, min(20.0, sum(e - s for s, e in wins) * 0.01))
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

    init = {pi(r): pa(r) for r in db.state_at(conn, prefix, wins[0][0])}
    last, updates, segs, cuts, shift = dict(init), [], [], [], 0.0
    for n, (s, e) in enumerate(wins):
        if n:
            gap, at = s - wins[n - 1][1], wins[n - 1][1] - shift + cut / 2
            now = {pi(r): pa(r) for r in db.state_at(conn, prefix, s)}
            for i in sorted(set(last) | set(now)):
                if last.get(i, -1) != now.get(i, -1):
                    last[i] = now.get(i, -1)
                    updates.append([round(at, 1), i, last[i]])
            shift += gap - cut
            cuts.append({"at": round(at, 1), "skipped": round(gap), "from": wins[n - 1][1], "to": s})
        segs.append([round(s - shift, 1), round(e - shift, 1), s])
        for r in conn.execute("SELECT ts, collector, peer, peer_asn, kind, path FROM updates "
                              "WHERE prefix=? AND ts>? AND ts<=? ORDER BY ts, id", (prefix, s, e)):
            i, j = pi(r), pa(r)
            if last.get(i, -1) != j:   # (a re-announcement of the same path is no change for the tree)
                last[i] = j
                updates.append([round(r["ts"] - shift, 1), i, j])
    asns = {int(x) for p in plist for x in p.split()}
    return {"asn": cfg.asn, "prefix": prefix, "start": wins[0][0], "end": wins[-1][1] - shift, "peers": peers,
            "paths": plist, "init": [init.get(i, -1) for i in range(len(peers))], "updates": updates, "events": [],
            "names": {str(k): v for k, v in names(conn, asns).items()}, "stitch": {"segments": segs, "cuts": cuts}}


def stitched_time(stitch, t):
    """A real time on the stitched clock (see stitched_payload): in the stretch it falls in, else the next one."""
    for s, e, real in stitch["segments"]:
        if t <= real + (e - s):
            return s + max(0.0, t - real)
    return stitch["segments"][-1][1]


async def upstream_neighbors(data):
    """Attach each direct upstream's own neighbours (its transits/peers, i.e. ASes seen to its left in the
    global table, from RIPEstat asn-neighbours) so the tree can show who is NOT carrying our prefix.
    Upstreams = configured expected upstreams + any AS seen adjacent to our origin in the window, except peers:
    those go under the tree's "Exchanges" branch instead (data["exchanges"]: peer -> exchange)."""
    ups, exchanges = set(cfg.expected_upstreams), {}
    for p in data["paths"]:
        a = [int(x) for x in p.split()]
        if len(a) >= 2 and a[-1] == cfg.asn:
            g = svc.engine.peer_group(a[-2])
            if g:
                exchanges[str(a[-2])] = g
            else:
                ups.add(a[-2])
    data["exchanges"] = exchanges
    nbrs = await tree.upstream_neighbors(svc.sourceapp, sorted(ups))
    out = {str(u): [{"asn": a, "power": pw} for a, pw in nbrs.get(u, []) if a != cfg.asn] for u in sorted(ups)}
    # neighbours an upstream had in the last month and no longer has at all: "no longer peered" on the tree
    gone = {str(u): [{"asn": a, "last": e["last"], "peak": e["peak"], "peak_at": e["peak_at"]}
                     for a, e in tree.gone_neighbors(svc.aconn, u) if a != cfg.asn] for u in sorted(ups)}
    data["upstreams"] = sorted(ups)
    data["upstream_neighbors"] = out
    data["upstream_gone"] = {u: g for u, g in gone.items() if g}
    # names for neighbours that never appear in our own paths
    want = ({n["asn"] for lst in out.values() for n in lst} | {n["asn"] for lst in gone.values() for n in lst}
            | ups | {int(a) for a in exchanges})
    missing = [a for a in want if str(a) not in data["names"]]
    if missing:
        for a, nm in (await svc.as_names(missing)).items():
            data["names"][str(a)] = nm
    # for the tree's "not carrying you" (see replay.js build): the neighbours you've configured, and the transit
    # filter check's latest look at each (upstream, transit) pair: how many RIS peers see the transit carrying the
    # upstream's own prefixes (ref), yours from it (ours) and yours at all (elsewhere), and its verdict (how, n,
    # via: see transit.verdict)
    data["known_neighbours"] = sorted(set(cfg.expected_upstreams) | set(cfg.peers))
    pfx = set(data.get("prefixes") or data.get("combined") or [data.get("prefix")])
    rows = {}
    for r in svc.transit.last_result:
        key = f"{r['upstream']}|{r['transit']}"
        if r["prefix"] in pfx and (key not in rows or r["ref"] > rows[key]["ref"]):
            rows[key] = {k: r.get(k) for k in ("ref", "ours", "elsewhere", "flag", "how", "n", "via")}
    data["transit_check"] = {"rows": rows, "min_peers": cfg.transit_filter_min_peers, "at": svc.transit.last_run}
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


@app.get("/api/events/replay")
async def api_events_replay(request: Request, ids: str = ""):
    """Path-tree data for events replayed together (ids=1,2,3): one after another on a stitched clock, the quiet time
    between them cut out. Events on several prefixes share the tree, one strand each (up to MAX_STRANDS)."""
    user(request)
    conn = rconn()
    try:
        rows = replay_events(conn, ids.split(","))
        if not rows:
            raise HTTPException(404)
        prefixes = list(dict.fromkeys(r["prefix"] for r in rows))[:MAX_STRANDS]
        wins = await asyncio.to_thread(activity_windows, conn, prefixes, rows)
        parts = [await asyncio.to_thread(stitched_payload, conn, p, wins) for p in prefixes]
    finally:
        conn.close()
    out = parts[0] if len(parts) == 1 else merge_replays(parts)
    st = out["stitch"] = parts[0]["stitch"]
    out["events"] = [{"id": r["id"], "title": r["title"], "severity": r["severity"], "kind": r["kind"],
                      "prefix": r["prefix"], "start": stitched_time(st, r["start_ts"]),
                      "end": stitched_time(st, r["end_ts"]) if r["end_ts"] else None,
                      "real_start": r["start_ts"], "real_end": r["end_ts"]} for r in rows if r["prefix"] in prefixes]
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


# ------------------------------------------------------------------------------------------------ BGP data page
DATA_TABS = ("paths", "changes", "raw", "routeviews")
LIVE_SOURCES = ("ris-live", "bgplay")   # updates that are news; every other source only corrects the stored state
CHANGES_MAX_DAYS = 31


def chain_key(p):
    """The way in of a path toward you: (transit, upstream); (None, upstream) when the RIS peer is your upstream
    itself; None for a path without one."""
    if len(p) >= 3:
        return (p[-3], p[-2])
    return (None, p[-2]) if len(p) == 2 else None


def chain_text(c, nm):
    """'AS64510 TRANSIT → AS64501 UPSTREAM' for a chain_key, with the first word of each name."""
    if not c:
        return "no route"
    lbl = lambda a: f"AS{a}" + (f" {nm[a].split(' ')[0]}" if nm.get(a) else "")   # noqa: E731
    return (f"{lbl(c[0])} → " if c[0] else "") + lbl(c[1])


def path_changes(conn, prefix, start, end, bookkeeping=False):
    """Every change of a RIS peer's path toward `prefix` between `start` and `end`, oldest first, judged against the
    stored state at `start`: [{"ts", "collector", "peer", "peer_asn", "old", "new", "source", "live"}], old / new the
    paths as ints (None: no route). A re-announcement of the same path is no change. `bookkeeping`: also the rows
    that only correct the stored state (the looking glass's resync, seed rows after a restart, stale withdrawals);
    they count toward the state either way."""
    last = {}
    for r in db.state_at(conn, prefix, start):
        last[(r["collector"], r["peer"])] = (paths.parse_str(r["path"]) if r["kind"] == "A" and r["path"] else None,
                                             r["peer_asn"])
    out = []
    for r in conn.execute("SELECT ts, collector, peer, peer_asn, kind, path, source FROM updates WHERE prefix=? AND ts>? "
                          "AND ts<=? ORDER BY ts, id", (prefix, start, end)):
        k = (r["collector"], r["peer"])
        new = paths.parse_str(r["path"]) if r["kind"] == "A" and r["path"] else None
        old, pa = last.get(k, (None, None))
        last[k] = (new, r["peer_asn"] or pa)
        if old == new:
            continue
        live = r["source"] in LIVE_SOURCES
        if live or bookkeeping:
            out.append({"ts": r["ts"], "collector": r["collector"], "peer": r["peer"], "peer_asn": r["peer_asn"] or pa,
                        "old": old, "new": new, "source": r["source"], "live": live})
    return out


def mark_diff(c):
    """For the page: each hop of a change's old and new paths with whether it differs ([(asn, changed)] each)."""
    old, new = c["old"] or [], c["new"] or []
    i = 0
    while i < min(len(old), len(new)) and old[i] == new[i]:
        i += 1
    j = 0
    while j < min(len(old), len(new)) - i and old[-1 - j] == new[-1 - j]:
        j += 1
    c["old_marks"] = [(a, i <= n < len(old) - j) for n, a in enumerate(old)]
    c["new_marks"] = [(a, i <= n < len(new) - j) for n, a in enumerate(new)]
    return c


def bursts_of(changes, gap=120):
    """Changes grouped into bursts (changes within `gap` seconds of each other), newest first: [{"start", "end",
    "peers", "changes", "moves": [((old chain, new chain), n)], biggest first}]."""
    out, cur = [], None
    for c in changes:
        if cur is None or c["ts"] - cur["end"] > gap:
            cur = {"start": c["ts"], "end": c["ts"], "changes": []}
            out.append(cur)
        cur["end"] = c["ts"]
        cur["changes"].append(c)
    for b in out:
        b["peers"] = len({(c["collector"], c["peer"]) for c in b["changes"]})
        moves = {}
        for c in b["changes"]:
            k = (chain_key(c["old"]) if c["old"] else None, chain_key(c["new"]) if c["new"] else None)
            moves[k] = moves.get(k, 0) + 1
        b["moves"] = sorted(moves.items(), key=lambda x: -x[1])
    out.reverse()
    return out


def burst_text(b, nm):
    """'152 moved from AS64510 A → AS64501 B to AS64520 C → AS64502 D; 34 withdrawn (were ...)'."""
    parts = []
    for (oc, nc), n in b["moves"][:3]:
        if oc == nc:
            parts.append(f"{n} changed further out (same way in)")
        elif nc is None:
            parts.append(f"{n} withdrawn (were {chain_text(oc, nm)})")
        elif oc is None:
            parts.append(f"{n} announced via {chain_text(nc, nm)}")
        else:
            parts.append(f"{n} moved from {chain_text(oc, nm)} to {chain_text(nc, nm)}")
    rest = sum(n for _, n in b["moves"][3:])
    return "; ".join(parts) + (f"; {rest} more" if rest else "")


def rv_latest(conn, prefix):
    """The latest good "show ip bgp" per collector for `prefix`: {server: [paths as ints]} and when the oldest is from."""
    out, oldest = {}, None
    for s in conn.execute("SELECT s.server, s.ts, s.paths FROM rv_snapshots s JOIN (SELECT server, MAX(ts) ts FROM "
                          "rv_snapshots WHERE prefix=? AND (error IS NULL OR error='') AND raw!='' GROUP BY server) l "
                          "ON l.server=s.server AND l.ts=s.ts WHERE s.prefix=?", (prefix, prefix)):
        out[s["server"]] = [paths.parse_str(p) for p in json.loads(s["paths"] or "[]")]
        oldest = s["ts"] if oldest is None else min(oldest, s["ts"])
    return out, oldest


def paths_tab(conn, prefix, now):
    """The Paths tab: the RIS peers' current paths grouped by the way in (transit → upstream), then by the next
    network out, with Route Views' count per way in and the week's changes."""
    cur = [c for c in svc.engine.current_paths(prefix) if c["path"] and c["path"][-1] == cfg.asn]
    groups = {}
    for c in cur:
        g = groups.setdefault(chain_key(c["path"]), {"chain": chain_key(c["path"]), "n": 0, "hops": {}})
        g["n"] += 1
        g["hops"].setdefault(c["path"][-4] if len(c["path"]) >= 4 else None, []).append(c)
    rv, rv_at = rv_latest(conn, prefix)
    rv_by = {}
    for srv, plist in rv.items():
        for p in plist:
            if p and p[-1] == cfg.asn:
                d = rv_by.setdefault(chain_key(p), {})
                d[srv] = d.get(srv, 0) + 1
    week = path_changes(conn, prefix, now - 7 * 86400, now)
    joined, left, last_change = {}, {}, {}
    for c in week:
        if c["new"]:
            joined[chain_key(c["new"])] = joined.get(chain_key(c["new"]), 0) + 1
        if c["old"]:
            left[chain_key(c["old"])] = left.get(chain_key(c["old"]), 0) + 1
        last_change[(c["collector"], c["peer"])] = c["ts"]
    for g in groups.values():
        for lst in g["hops"].values():
            lst.sort(key=lambda c: (-last_change.get((c["collector"], c["peer"]), 0), c["collector"], c["peer"]))
        g["hops"] = sorted(g["hops"].items(), key=lambda x: -len(x[1]))
        g.update(rv=rv_by.get(g["chain"], {}), joined=joined.get(g["chain"], 0), left=left.get(g["chain"], 0))
    return {"groups": sorted(groups.values(), key=lambda g: -g["n"]), "total": len(cur), "rv_servers": len(rv),
            "rv_at": rv_at, "latest": [mark_diff(c) for c in week[-5:]][::-1], "last_change": last_change}


def rv_tab(conn):
    """The Route Views tab: the latest routine "show ip bgp" per collector and prefix, the snapshot history, and
    each collector's share of its daily budget used today."""
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
    snaps = conn.execute("SELECT id,ts,server,prefix,n_paths,event_id,error FROM rv_snapshots ORDER BY ts DESC "
                         "LIMIT 50").fetchall()
    return {"latest": latest, "snaps": snaps, "budget": rv_budget()}


def rv_budget():
    """Each Route Views collector's use of its daily telnet budget from this server, today."""
    out = []
    for srv in cfg.rv_servers:
        b = svc._rv_state(srv)
        ok, why = svc.rv_allowed(srv, "scheduled")
        out.append({"server": srv, "ok": ok, "why": why, "conns": b.get("conns", 0), "kb": b.get("bytes_in", 0) / 1000})
    return out


@app.get("/data", response_class=HTMLResponse)
async def data_page(request: Request, tab: str = "paths", prefix: str = None, start: float = None, end: float = None,
                    q: str = "", source: str = "", all: int = 0, started: str = ""):
    """BGP data: the current paths grouped by the way in (Paths), what changed and when (Changes), the raw update
    log (Raw updates), and Route Views' tables (Route Views)."""
    user(request)
    tab = tab if tab in DATA_TABS else "paths"
    now = time.time()
    conn = rconn()
    # (more-specifics and other prefixes of yours seen in the RIB; not the control prefixes, which aren't routed to you)
    others = sorted(p for p in svc.engine.rib if p not in svc.engine.mons and p not in svc.engine.controls and
                    any(e.path for e in svc.engine.rib[p].values()))
    prefix = prefix if prefix in cfg.prefixes or prefix in others else (cfg.prefixes[0] if cfg.prefixes else "")
    ctx = {"tab": tab, "prefix": prefix, "others": others, "q": q, "source": source, "all": all, "started": started}
    asns = set()
    if tab == "paths":
        pt = paths_tab(conn, prefix, now)
        for g in pt["groups"]:
            asns.update(a for a in g["chain"] if a)
            for h, lst in g["hops"]:
                asns.add(h)
                for c in lst:
                    asns.update(c["path"])
                    asns.add(c["peer_asn"])
        for c in pt["latest"]:
            asns.update((c["old"] or []) + (c["new"] or []))
        ctx.update(pt)
    elif tab == "changes":
        end = min(end or now, now)
        start = max(start or end - 86400, end - CHANGES_MAX_DAYS * 86400)
        every = path_changes(conn, prefix, start, end, bookkeeping=True)
        n_book = sum(1 for c in every if not c["live"])
        ch = every if all else [c for c in every if c["live"]]
        if q:
            qq = q.strip().lower()
            text = lambda c: f"{c['collector']} {c['peer']} {' '.join(map(str, c['old'] or []))} {' '.join(map(str, c['new'] or []))}"  # noqa: E731
            ch = [c for c in ch if qq in text(c).lower()]
        for c in ch:
            mark_diff(c)
            asns.update((c["old"] or []) + (c["new"] or []))
            asns.add(c["peer_asn"])
        bursts = bursts_of(ch)
        evs = conn.execute("SELECT id, kind, severity, title, start_ts, end_ts FROM events WHERE (prefix=? OR prefix IS NULL) "
                           "AND severity!='info' AND start_ts<=? AND COALESCE(end_ts, ?)>=? ORDER BY start_ts",
                           (prefix, end + 300, now, start - 300)).fetchall()
        for b in bursts:
            b["events"] = [e for e in evs if e["start_ts"] <= b["end"] + 300 and (e["end_ts"] or now) >= b["start"] - 300]
        ctx.update(start=start, end=end, bursts=bursts, n_changes=len(ch), n_book=n_book,
                   n_big=sum(1 for b in bursts if b["peers"] >= 5))
    elif tab == "raw":
        end = min(end or now, now)
        start = start or end - 86400
        where, args = ["prefix=?", "ts>=?", "ts<=?"], [prefix, start, end]
        if q:
            where.append("(path LIKE ? OR peer LIKE ? OR collector LIKE ?)")
            args += [f"%{q}%"] * 3
        if source:
            where.append("source=?")
            args.append(source)
        rows = conn.execute(f"SELECT * FROM updates WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT 2000",
                            args).fetchall()
        ctx.update(start=start, end=end, rows=rows,
                   sources=[r[0] for r in conn.execute("SELECT DISTINCT source FROM updates ORDER BY source")])
    else:
        ctx.update(rv_tab(conn))
    nm = names(conn, asns)
    conn.close()
    nm_txt = {a: n for a, n in nm.items()}
    if tab == "changes":
        for b in ctx["bursts"]:
            b["text"] = burst_text(b, nm_txt)
    return render(request, "data.html", names=nm, **ctx)


@app.get("/paths")
async def paths_moved(request: Request, prefix: str = None):
    """The Paths page is the Paths tab of BGP data now (bookmarks keep working)."""
    return RedirectResponse("/data?tab=paths" + (f"&prefix={quote(prefix)}" if prefix else ""), status_code=301)


# ------------------------------------------------------------------------------------------------ path finder
def _pf_error(ex):
    return JSONResponse({"error": str(ex)}, status_code=ex.status)


def _pf_may_check(request):
    if not pathfinder.may_check(svc.aconn, request.state.role):
        raise pathfinder.PathError(pathfinder.check_refusal(svc.aconn), 403)


@app.get("/pathfinder", response_class=HTMLResponse)
async def pathfinder_page(request: Request, q: str = "", prefix: str = "", origin: str = "", rv: int = 0,
                          msg: str = "", err: int = 0):
    user(request)
    ws = watch.watches(svc.aconn)
    nm = names(svc.aconn, {a for w in ws for v in (w["state"].get("active"), w["state"].get("back")) if v
                           for a in v["path"]} | {w["state"].get("them") for w in ws})
    can = pathfinder.may_check(svc.aconn, request.state.role)
    return render(request, "pathfinder.html", q=q.strip()[:100], prefix=prefix.strip()[:50], origin=origin.strip()[:10],
                  rv=bool(rv) and can, trace_ok=tracer.supported(), can_check=can,
                  checks=pathfinder.settings(svc.aconn)["checks"], check_choices=pathfinder.CHECKS,
                  watches=ws, this_watch=watch.find(svc.aconn, q) if q.strip() else None, wnames=nm,
                  minutes=watch.MINUTES, back_minutes=watch.BACK_MINUTES, atlas_on=atlas.enabled(svc.aconn),
                  back_credits=atlas.COST["traceroute"] * 2 * pathfinder.BACK_PROBES, msg=msg, err=err)


async def _pf_lookup(request, q, prefix="", origin=0, rv=False):
    if rv:
        _pf_may_check(request)
    return await pathfinder.lookup(svc, q, prefix.strip() or None, origin, routeviews_too=rv)


@app.get("/api/pathfinder")
async def api_pathfinder(request: Request, q: str = "", prefix: str = "", origin: int = 0, rv: int = 0):
    """The known AS paths between your network and the one `q` (an IP address, prefix, AS number or host name) is
    in; see pathfinder.py. `prefix`: which of an AS's prefixes; `origin`: which AS, when several announce it; `rv`:
    ask Route Views' collectors too."""
    user(request)
    try:
        res = await _pf_lookup(request, q, prefix, origin, bool(rv))
    except pathfinder.PathError as ex:
        return _pf_error(ex)
    pathfinder.record(svc.aconn, res["key"], "known", pathfinder.known_value(res))
    res["may_check"] = pathfinder.may_check(svc.aconn, request.state.role)
    return res


@app.post("/api/pathfinder/trace")
async def api_pathfinder_trace(request: Request, q: str = Form(...), prefix: str = Form(""), origin: int = Form(0)):
    """Traceroute from this server toward `q` (its address, or an address in its prefix), matched against the known
    paths; see pathfinder.trace."""
    user(request)
    try:
        _pf_may_check(request)
        res = await _pf_lookup(request, q, prefix, origin)
        tr = await pathfinder.trace(svc, res["trace_to"], res["paths"], res["target"]["asn"])
    except pathfinder.PathError as ex:
        return _pf_error(ex)
    pathfinder.record(svc.aconn, res["key"], "active", pathfinder.active_value(tr))
    return tr


@app.post("/api/pathfinder/back")
async def api_pathfinder_back(request: Request, q: str = Form(...), prefix: str = Form(""), origin: int = Form(0)):
    """Start a RIPE Atlas traceroute to your address from probes in their network (or their main upstreams)."""
    u = user(request)
    try:
        _pf_may_check(request)
        res = await _pf_lookup(request, q, prefix, origin)
        return await pathfinder.back_start(svc, res["target"]["asn"], res["main"], user=u)
    except pathfinder.PathError as ex:
        return _pf_error(ex)


@app.get("/api/pathfinder/back/{msm}")
async def api_pathfinder_back_result(request: Request, msm: int, q: str, prefix: str = "", origin: int = 0):
    """What a trace back from them has shown so far, matched against the known paths."""
    user(request)
    try:
        res = await _pf_lookup(request, q, prefix, origin)
        out = await pathfinder.back_result(svc, msm, res["target"]["asn"], res["paths"])
    except pathfinder.PathError as ex:
        return _pf_error(ex)
    pathfinder.record(svc.aconn, res["key"], "back", pathfinder.back_value(out["results"]))
    return out


@app.get("/api/pathfinder/history")
async def api_pathfinder_history(request: Request, q: str, prefix: str = "", origin: int = 0):
    """What every lookup, traceroute and trace back for this target has shown, newest first (pathfinder.history)."""
    user(request)
    try:
        key = pathfinder.key_of(q, prefix.strip() or None, origin or None)
    except ValueError as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    rows = pathfinder.history(svc.aconn, key)
    asns = {a for r in rows for a in (r["value"].get("path") or [])}
    asns |= {int(x) for r in rows for p in [r["value"].get("shortest")] if p for x in p.split()}
    return {"key": key, "rows": rows, "names": {str(k): v for k, v in names(svc.aconn, asns).items()}}


@app.post("/pathfinder/watch")
async def pathfinder_watch(request: Request, q: str = Form(...), label: str = Form(""), minutes: int = Form(60),
                           alert: str = Form(""), back: str = Form("")):
    u = admin(request)
    try:
        watch.save(svc.aconn, q, label, minutes, bool(alert), bool(back), u)
    except ValueError as ex:
        return RedirectResponse(f"/pathfinder?{urlencode({'q': q, 'msg': str(ex), 'err': 1})}", status_code=303)
    w = watch.find(svc.aconn, q)
    msg = "Saved." if w and w["last_check"] else "Watching it. The first check runs within a minute."
    return RedirectResponse(f"/pathfinder?{urlencode({'q': q, 'msg': msg})}", status_code=303)


@app.post("/pathfinder/watch/{wid}/delete")
async def pathfinder_unwatch(request: Request, wid: int, back: str = Form("/pathfinder")):
    admin(request)
    watch.remove(svc.aconn, wid)
    return RedirectResponse(back if back.startswith("/pathfinder") else "/pathfinder", status_code=303)


@app.post("/pathfinder/watch/{wid}/check")
async def pathfinder_watch_check(request: Request, wid: int):
    admin(request)
    w = watch.get(svc.aconn, wid)
    if not w:
        raise HTTPException(404)
    await watch.check(svc, w)
    return RedirectResponse(f"/pathfinder?{urlencode({'q': w['query'], 'msg': 'Checked just now.'})}", status_code=303)


@app.post("/pathfinder/settings")
async def pathfinder_settings(request: Request, checks: str = Form(...)):
    u = admin(request)
    if checks not in pathfinder.CHECKS:
        raise HTTPException(400)
    db.kv_set(svc.aconn, pathfinder.SETTINGS_KV, {**pathfinder.settings(svc.aconn), "checks": checks})
    svc.aconn.commit()
    log.info("path finder checks: %s (set by %s)", checks, u)
    return RedirectResponse(f"/pathfinder?{urlencode({'msg': 'Saved.'})}", status_code=303)


@app.get("/updates")
async def updates_moved(request: Request):
    """The update log is the Raw updates tab of BGP data now (bookmarks keep working)."""
    qs = request.url.query
    return RedirectResponse("/data?tab=raw" + (f"&{qs}" if qs else ""), status_code=301)


@app.get("/probes")
async def probes_moved(request: Request, page: int = 1):
    """Route Views' ping and traceroute are on the Reachability page now, with RIPE Atlas; its BGP tables under
    BGP data (bookmarks keep working)."""
    return RedirectResponse(f"/reachability?page={page}#routeviews", status_code=301)


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
    try:
        target = config.plain_ip(target)   # only the normalized address goes to the Route Views session
    except ValueError:
        raise HTTPException(400, "target must be an IP address")
    if kind not in ("ping", "traceroute"):
        raise HTTPException(400)
    asyncio.create_task(svc.run_probe(kind, target, "manual"))
    return RedirectResponse("/reachability?started=1#routeviews", status_code=303)


@app.post("/actions/snapshot")
async def action_snapshot(request: Request):
    admin(request)
    asyncio.create_task(svc.snapshot_all(trigger="manual"))
    return RedirectResponse("/data?tab=routeviews&started=1", status_code=303)


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
    nm = names(conn, {r["upstream"] for r in tf_rows} | {r["transit"] for r in tf_rows}
               | {a for r in tf_rows for a in r.get("via", [])[:1]})
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
    # Reality next to the simulation: how each of each upstream's transits carries us right now (live RIS), read
    # the way the transit filter check and the connectivity tree read it (transit.verdict: straight from the
    # upstream, another way it prefers, only the long way round, or not at all), and whether that transit has its
    # own as-set listing us (which is how it can carry us even when the upstream's registration wouldn't allow it).
    mine = [e.path for p in cfg.prefixes for e in list(svc.engine.rib.get(p, {}).values())
            if e.path and e.path[-1] == cfg.asn]
    tails, direct = transit.reach_of(mine, cfg.asn, set(cfg.expected_upstreams) | set(cfg.peers))
    tf = {(r["upstream"], r["transit"]): r for r in svc.transit.last_result}
    ups = [f["upstream"] for f in (rep or {}).get("filters", [])]
    try:   # each upstream's full transit/peer list from the global table (cached 6 h), carrying us or not
        nbrs = await tree.upstream_neighbors(svc.sourceapp, ups)
    except Exception:
        nbrs = {}
    reality, asns = {}, set()
    for f in (rep or {}).get("filters", []):
        u = f["upstream"]
        transits = {p[-3] for p in mine if len(p) >= 3 and p[-2] == u} | {t for (uu, t) in tf if uu == u} | \
                   {t for t, _power in nbrs.get(u, []) if t != cfg.asn}
        rows = []
        for t in transits:
            own = [a["name"] for a in rep.get("assets", [])
                   if a["name"].upper().startswith(f"AS{t}:") or re.search(rf"\bAS{t}\b", a.get("mnt", "").upper())]
            v = transit.verdict(tails, direct, cfg.asn, u, t)
            rows.append({"transit": t, "ref": tf.get((u, t), {}).get("ref"), "own_sets": own, **v})
            asns.add(t)
            asns.update(v["via"][:1])
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


restart_requested = False   # (read by bgpcanary.container, which then starts the app again in place)


def _restart_soon(delay=1.0):
    """Exit gracefully; systemd (Restart=always) starts the service again with the new settings (in the container,
    bgpcanary.container does, in the same process)."""
    import signal
    global restart_requested
    restart_requested = True
    asyncio.get_running_loop().call_later(delay, os.kill, os.getpid(), signal.SIGTERM)


def _parse_list(text):
    return [x for x in re.split(r"[\s,;]+", text or "") if x]


@app.get("/settings/monitor", response_class=HTMLResponse)
async def monitor_settings_page(request: Request, msg: str = "", err: int = 0):
    admin(request)
    return render(request, "monitor_settings.html", msg=msg, err=err, m=config.monitor_settings_dict(cfg),
                  from_db=db.kv_get(svc.aconn, config.MONITOR_KV) is not None, first_run=not cfg.configured,
                  px=svc.peering.status())


@app.post("/settings/monitor")
async def monitor_settings_save(request: Request, asn: str = Form(""), prefixes: str = Form(""),
                                expected_upstreams: str = Form(""), probe_targets: str = Form(""),
                                owned_space: str = Form(""), site_name: str = Form(""), peers: str = Form("")):
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
            net = config.plain_prefix(x, strict=True)
        except ValueError:
            try:
                problems.append(f"{x} has host bits set - did you mean {config.plain_prefix(x)}?")
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
    prs = []
    for x in _parse_list(peers):
        y = x.upper().removeprefix("AS")
        if not y.isdigit():
            return back(f"Peer {x} isn't an ASN")
        if int(y) in ups:
            return back(f"AS{y} is listed both as an upstream and as a peer: keep it in one of them")
        if int(y) not in prs:
            prs.append(int(y))
    owned = []
    for x in _parse_list(owned_space):
        try:
            owned.append(config.normalize_prefix(x))
        except ValueError:
            return back(f"Owned space {x} isn't a valid prefix")
    targets = []
    for x in _parse_list(probe_targets):
        try:
            targets.append(config.plain_ip(x))
        except ValueError:
            return back(f"Probe target {x} isn't an IP address")
    data = {"asn": int(a), "prefixes": pfx, "expected_upstreams": ups, "peers": prs, "owned_space": owned,
            "probe_targets": targets, "site_name": site_name.strip() or f"BGP Canary · AS{a}"}
    old = config.monitor_settings_dict(cfg)
    db.kv_set(svc.aconn, config.MONITOR_KV, data)
    svc.aconn.commit()
    added = [p for p in pfx if p not in old["prefixes"]]
    log.info("user %s changed monitoring settings: %s (added %s)", who, data, added)
    _restart_soon()
    return RedirectResponse("/restarting?" + urlencode({"since": STARTED, "added": ",".join(added)}),
                            status_code=303)


@app.post("/settings/exchanges/refresh")
async def exchanges_refresh(request: Request):
    """Look your exchanges up in PeeringDB now, instead of waiting for the daily check. Applies at once."""
    who = admin(request)
    before = svc.peering.data.get("fetched")
    await svc.peering.refresh(force=True)
    st = svc.peering.status()
    log.info("user %s looked up the exchange list in PeeringDB", who)
    if st["fetched"] is None or st["fetched"] == before and st["error"]:
        msg = f"Couldn't reach PeeringDB ({st['error'] or 'no answer'}). The previous list is kept."
        return RedirectResponse(f"/settings/monitor?err=1&msg={quote(msg)}", status_code=303)
    n = len(st["exchanges"])
    msg = (f"PeeringDB lists AS{cfg.asn} at {n} exchange{'s' if n != 1 else ''}." if n else
           f"PeeringDB lists no exchanges for AS{cfg.asn}.")
    return RedirectResponse(f"/settings/monitor?msg={quote(msg)}", status_code=303)


@app.post("/settings/neighbor")
async def neighbor_role(request: Request, asn: int = Form(...), role: str = Form(...), back: str = Form("/events")):
    """One click from an event page: file a neighbour as one of your transits (an expected upstream) or as a peer.
    Takes effect at once, without a restart: detection reads these lists on every check, so the warning it was
    about closes by itself after the sustain time."""
    who = admin(request)
    if role not in ("transit", "peer") or not 0 < asn < 2 ** 32:
        raise HTTPException(400, "Bad request")
    back = back if re.fullmatch(r"/events/\d+", back) else "/events"
    data = dict(db.kv_get(svc.aconn, config.MONITOR_KV) or config.monitor_settings_dict(cfg))
    ups = [u for u in data.get("expected_upstreams", cfg.expected_upstreams) if u != asn]
    prs = [u for u in data.get("peers", cfg.peers) if u != asn]
    (ups if role == "transit" else prs).append(asn)
    data.update(expected_upstreams=ups, peers=prs)
    db.kv_set(svc.aconn, config.MONITOR_KV, data)
    svc.aconn.commit()
    cfg.expected_upstreams, cfg.peers = list(ups), list(prs)
    log.info("user %s marked AS%s as %s", who, asn, "a transit (expected upstream)" if role == "transit" else "a peer")
    msg = (f"AS{asn} is now one of your expected upstreams." if role == "transit" else f"AS{asn} is now listed as a peer.")
    return RedirectResponse(f"{back}?msg={quote(msg + ' Its warning closes by itself within a minute or two.')}",
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
    import ipaddress
    mine = [ipaddress.ip_network(x, strict=False) for x in list(cfg.prefixes) + list(cfg.owned_space)]
    for c in data.get("control_prefixes") or []:
        n = ipaddress.ip_network(c, strict=False)
        if any(n.version == m.version and n.overlaps(m) for m in mine):
            problems.append(f"Control prefix {c} overlaps your own address space: pick another network's prefix")
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
async def alert_settings_page(request: Request, msg: str = "", err: int = 0):
    return await _alert_settings(request, msg, err)


async def _alert_settings(request, msg="", err=0, form=None):
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
        return await _alert_settings(request, "Not saved: " + "; ".join(problems), 1, form=f)
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
    auth.set_user(svc.aconn, u, new)   # keeps role; clears must_change; signs out other sessions
    keep_session(request, u)
    if back == "/account/password":
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/settings?msg=" + quote("Password changed"), status_code=303)


@app.post("/settings/signout-others")
async def signout_others(request: Request):
    """Sign this account out of every other browser (e.g. after using a shared computer)."""
    u = user(request, allow_must_change=True)
    auth.sign_out_everywhere(svc.aconn, u)
    keep_session(request, u)
    log.info("user %s signed out their other sessions", u)
    return RedirectResponse("/settings?msg=" + quote("Signed out everywhere else"), status_code=303)


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
    keep_session(request, username)
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
    keep_session(request, username)
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


@app.get("/atlas")
async def atlas_moved(request: Request):
    """RIPE Atlas is the Reachability page now, with Route Views' ping and traceroute (bookmarks keep working)."""
    qs = request.url.query
    return RedirectResponse("/reachability" + (f"?{qs}" if qs else ""), status_code=301)


@app.get("/reachability", response_class=HTMLResponse)
async def reachability_page(request: Request, target: str = "", hours: float = 24, start: float = None,
                            end: float = None, msm: int = None, msg: str = "", err: int = 0, compare: str = "",
                            page: int = 1, started: str = ""):
    """Does traffic reach you, and where does it stop: RIPE Atlas probes worldwide, and Route Views' collectors'
    ping and traceroute (their BGP tables are under BGP data)."""
    user(request)
    conn = rconn()
    probes = db.annotate_probes(conn, conn.execute(
        "SELECT id,ts,server,kind,target,trigger,event_id,success_pct,rtt_avg,last_hop,last_hop_asn,reached,error "
        "FROM probes ORDER BY ts DESC LIMIT 100 OFFSET ?", ((page - 1) * 100,)).fetchall())
    probe_names = names(conn, [r["last_hop_asn"] for r in probes])
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
    return render(request, "reachability.html", targets=targets, target=target, hours=hours, start=start, end=end,
                  compare=selected, groups=groups, probes=probes, probe_names=probe_names, page=page, started=started,
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
        return RedirectResponse("/reachability?err=1&msg=" + quote("RIPE Atlas is off (Settings > RIPE Atlas)."), status_code=303)
    try:
        msm = await svc.atlas.burst("manual", description=f"bgpcanary manual traceroute ({me})")
    except Exception as ex:
        return RedirectResponse("/reachability?err=1&msg=" + quote(f"Traceroute not started: {ex}"), status_code=303)
    log.info("user %s started Atlas traceroute burst %s", me, msm)
    return RedirectResponse(f"/reachability?target={quote(svc.atlas.target())}&msm={msm}&msg=" + quote(
        "Traceroute started. Results arrive over the next few minutes; reload to see more probes fill in."),
        status_code=303)


@app.post("/atlas/adhoc")
async def atlas_adhoc(request: Request, target: str = Form(""), kind: str = Form("both"), source: str = Form("world"),
                      value: str = Form(""), probes: int = Form(20)):
    """One-off test toward any IP / host name, from probes worldwide, in a network, or in a country."""
    me = admin(request)

    def back(msg):
        return RedirectResponse("/reachability?err=1&msg=" + quote(msg), status_code=303)
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
    return RedirectResponse(f"/reachability?target={quote(t)}&start={now - 120}&end={now + 3600}&msg=" + quote(
        f"Started {' and '.join(kinds)} to {t} from {len(ids[:n])} probes. Results arrive over the next few minutes; "
        "reload to see them fill in."), status_code=303)


@app.post("/atlas/demo")
async def atlas_demo(request: Request, action: str = Form("load")):
    admin(request)
    if action == "clear":
        svc.atlas.clear_demo()
        return RedirectResponse("/reachability?msg=" + quote("Sample data removed."), status_code=303)
    try:
        await svc.atlas.load_demo()
    except Exception as ex:
        return RedirectResponse("/reachability?err=1&msg=" + quote(f"Couldn't load the sample data: {ex}"), status_code=303)
    return RedirectResponse(f"/reachability?target={atlas.DEMO_TARGET}&msg=" + quote(
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
    old_key = s.get("api_key", "")   # what running measurements were made with

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
    failed = []
    if was_on and not s["enabled"]:
        # stop them before the key is cleared or replaced: with the key they were made with
        failed = await svc.atlas.disable(key=old_key or True)
    db.kv_set(svc.aconn, atlas.KV_SETTINGS, s)
    svc.aconn.commit()
    log.info("user %s changed RIPE Atlas settings (enabled=%s)", me, s["enabled"])
    if key or f.get("clear_key") == "1":
        await svc.atlas.credits(force=True)
    err = False
    if was_on and not atlas.enabled(svc.aconn):
        if failed:
            msg = (f"RIPE Atlas turned off, but Atlas didn't confirm stopping {len(failed)} periodic measurement(s) "
                   f"({', '.join('#' + str(m) for m in failed)}). They keep using credits until they're stopped: "
                   + ("bgpcanary retries every few minutes with the saved key, or stop them on atlas.ripe.net."
                      if s["api_key"] else "with the key cleared, stop them on atlas.ripe.net (links below)."))
            err = True
        else:
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
