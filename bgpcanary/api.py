"""The JSON API (/api/v1/) for scripts and dashboards: docs/API.md.

Read-only, used with a per-user token ("Authorization: Bearer bgpc_...": auth.py, web.user) or a signed-in browser's
session. It answers from the pages' own helpers, but its shapes are a promise: within v1 a field is only ever added,
never renamed or removed (tests/test_api.py holds the contract). The unversioned /api/... routes in web.py belong to
the pages and may change in any release.

The payload functions (status_payload, events_payload, ...) build the answers; the routes add the sign-in check.
The MCP server (mcp.py) serves the same payloads, so the two never drift apart."""
import json
import time

from fastapi import APIRouter, HTTPException, Request

from . import __version__, alerts, config

router = APIRouter(prefix="/api/v1")

LIMIT_MAX = 500
EVENT_FIELDS = ("id", "kind", "severity", "prefix", "start_ts", "end_ts", "title", "min_visible", "baseline")  # the webhook's
SEVERITIES = ("info", "warning", "critical")
ROLE = {"transit": "transit", "listed peer": "peer", "exchange peer": "exchange_peer"}   # neighbour_info's words -> the API's


def link(path):
    """A full link when the Site URL is set, else the path."""
    return f"{web.cfg.base_url}{path}"


def event_json(conn, ev, names=None):
    """One event as the API shows it: the webhook payload's fields, plus kind_name, open, historical, url, the
    neighbour it's about and its kind-specific detail (which isn't part of the compatibility promise)."""
    d = {k: ev[k] for k in EVENT_FIELDS}
    nb = web.event_neighbour(ev)
    if nb and names is None:
        names = web.names(conn, [nb])
    try:
        detail = json.loads(ev["detail"] or "{}")
    except ValueError:
        detail = {}
    d.update(kind_name=web.KIND_NAMES.get(ev["kind"], ev["kind"]), open=ev["end_ts"] is None,
             historical=bool(ev["historical"]), url=link(f"/events/{ev['id']}"),
             neighbour={"asn": nb, "name": names.get(nb, "")} if nb else None, detail=detail)
    return d


def events_json(conn, rows):
    names = web.names(conn, {web.event_neighbour(r) for r in rows} - {None})
    return [event_json(conn, r, names) for r in rows]


# ------------------------------------------------------------------------------------------------ the payloads
def status_payload(conn):
    """The monitor right now: live or not, each prefix's visibility, state and neighbours (the dashboard's table),
    and how many warning or critical events are open."""
    st = web.svc.engine.status
    summ = web.prefix_summary(conn)
    asns = set(web.cfg.expected_upstreams)
    for p in summ:
        asns |= {a for a, _ in p["upstreams"]} | {a for g in p["peers"] for a, _ in g["peers"]} | set(p["missing"])
    nm = web.names(conn, asns)
    open_events = conn.execute("SELECT COUNT(*) c FROM events WHERE end_ts IS NULL AND severity!='info'").fetchone()["c"]
    phase = st.get("phase") or ""
    return {"version": __version__, "phase": phase, "live": phase.startswith("live"),
            "ris_connected": bool(st.get("ws_connected")), "last_message": st.get("last_msg"),
            "asn": web.cfg.asn,
            "expected_upstreams": [{"asn": a, "name": nm.get(a, "")} for a in web.cfg.expected_upstreams],
            "open_events": open_events,
            "prefixes": [{"prefix": p["prefix"], "state": p["state"], "visible": p["visible"], "baseline": p["baseline"],
                          "pct": round(p["pct"], 1) if p["pct"] is not None else None,
                          "upstreams": [{"asn": a, "name": nm.get(a, ""), "paths": n} for a, n in p["upstreams"]],
                          "peers": [{"asn": a, "name": nm.get(a, ""), "exchange": g["name"], "paths": n}
                                    for g in p["peers"] for a, n in g["peers"]],
                          "missing": [{"asn": a, "name": nm.get(a, "")} for a in p["missing"]]} for p in summ]}


def events_payload(conn, state="", kind="", severity="", prefix="", since=None, until=None, info=0, limit=100,
                   offset=0):
    """The events, newest first, with the Events page's filters: state (open | closed), kind, severity, and info=1 to
    include the info ones (left out unless asked for, as on the page); plus prefix, and since / until (unix seconds)
    for the events going on at some point in that window. HTTPException(400) for a bad filter."""
    if state not in ("", "open", "closed"):
        raise HTTPException(400, "state: open or closed")
    if severity not in ("",) + SEVERITIES:
        raise HTTPException(400, "severity: info, warning or critical")
    if prefix:
        try:
            prefix = config.normalize_prefix(prefix)
        except ValueError:
            raise HTTPException(400, "prefix: not a prefix") from None
    limit, offset = max(1, min(int(limit), LIMIT_MAX)), max(0, int(offset))
    where, args = [], []
    if kind:
        where.append("kind=?")
        args.append(kind)
    if severity:
        where.append("severity=?")
        args.append(severity)
    elif not info:
        where.append("severity!='info'")
    if state:
        where.append("end_ts IS NULL" if state == "open" else "end_ts IS NOT NULL")
    if prefix:
        where.append("prefix=?")
        args.append(prefix)
    if since is not None:   # still going on, or ended after `since`
        where.append("COALESCE(end_ts, ?) >= ?")
        args += [time.time(), since]
    if until is not None:
        where.append("start_ts <= ?")
        args.append(until)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) c FROM events {w}", args).fetchone()["c"]
    rows = conn.execute(f"SELECT * FROM events {w} ORDER BY start_ts DESC, id DESC LIMIT ? OFFSET ?",
                        args + [limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "events": events_json(conn, rows)}


def event_payload(conn, eid):
    """One event, with the alerts sent for it and where its text report is. HTTPException(404) when unknown."""
    ev = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not ev:
        raise HTTPException(404, "no such event")
    d = event_json(conn, ev)
    sent = conn.execute("SELECT ts, channel, ok, error FROM alert_log WHERE event_id=? ORDER BY ts", (eid,)).fetchall()
    d["alerts"] = [{"ts": r["ts"], "channel": r["channel"], "ok": bool(r["ok"]),
                    "error": alerts.mask_urls(r["error"]) if r["error"] else None} for r in sent]
    d["report_url"] = link(f"/api/v1/events/{eid}/report")
    return d


def neighbour_payload(conn, asn):
    """A neighbouring network: what it is to you (transit, peer, exchange_peer, or null for one you haven't filed),
    how many RIS peers reach you through it right now, and the events about it."""
    if not 0 < asn < 2 ** 32:
        raise HTTPException(404, "no such ASN")
    nb = web.neighbour_info(conn, asn)
    evs = events_json(conn, web.neighbour_events(conn, asn))
    return {"asn": asn, "name": nb["name"], "role": ROLE.get(nb["role"]), "exchange": nb["exchange"],
            "paths_now": web.neighbour_paths_now(asn), "events": evs}


# ------------------------------------------------------------------------------------------------ the routes
@router.get("/me")
async def me(request: Request):
    """Who you are to the API: the account, its role, and whether a token or the browser's session signed you in."""
    u = web.user(request)
    tok = getattr(request.state, "token", None)
    return {"user": u, "role": request.state.role, "auth": "token" if tok else "session", "token": tok}


@router.get("/status")
async def status(request: Request):
    web.user(request)
    conn = web.rconn()
    try:
        return status_payload(conn)
    finally:
        conn.close()


@router.get("/events")
async def events(request: Request, state: str = "", kind: str = "", severity: str = "", prefix: str = "",
                 since: float = None, until: float = None, info: int = 0, limit: int = 100, offset: int = 0):
    web.user(request)
    conn = web.rconn()
    try:
        return events_payload(conn, state, kind, severity, prefix, since, until, info, limit, offset)
    finally:
        conn.close()


@router.get("/events/{eid}")
async def event(request: Request, eid: int):
    web.user(request)
    conn = web.rconn()
    try:
        return event_payload(conn, eid)
    finally:
        conn.close()


@router.get("/events/{eid}/report")
async def event_report(request: Request, eid: int):
    """The event's evidence as plain text: the page's report.txt, for scripts that file tickets."""
    web.user(request)
    resp = await web.report_response(eid)
    if resp is None:
        raise HTTPException(404, "no such event")
    return resp


@router.get("/neighbours/{asn}")
async def neighbour(request: Request, asn: int):
    web.user(request)
    conn = web.rconn()
    try:
        return neighbour_payload(conn, asn)
    finally:
        conn.close()


# Last, on purpose: web.py imports this module at its own end to include the router, so each finds the other
# half-built here, and whole by the time a request arrives.
from . import web  # noqa: E402
