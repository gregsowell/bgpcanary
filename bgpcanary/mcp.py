"""The MCP server (/mcp; docs/MCP.md): an AI assistant reads your bgpcanary through the Model Context Protocol with an
API token (Settings › API tokens), once an admin has turned it on (Settings › MCP server).

It speaks MCP's Streamable HTTP transport itself: JSON-RPC 2.0 over POST, one JSON answer per request, no sessions
and no server-to-client stream (both optional in the protocol), so it needs no extra dependency and nothing stays
open between calls. Tools only, all read-only, on the same payloads as the API (api.py): the status, the events,
one event with its report, a neighbouring network, and a connection check. tests/test_mcp.py drives it with the
official MCP client as well as by hand."""
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import __version__, api, db

log = logging.getLogger("bgpcanary.mcp")
router = APIRouter()
PATH = "/mcp"
KV = "mcp_settings"
MAX_BODY = 256 * 1024
# The protocol revisions this server knows. It answers in the one the client asks for when it's one of these, else in
# the newest: a tools-only server looks the same in all of them.
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")

INSTRUCTIONS = """bgpcanary watches how the rest of the internet sees the prefixes one network (its ASN) announces, from
public route collectors (RIPE RIS, about 330 peers), and records outages, hijacks, leaks and upstream changes as
events. These tools read one bgpcanary instance, as the owner of the token, read-only.

Vocabulary: a prefix's `visible` is how many RIS peers see it right now; `baseline` is its normal count (the 95th
percentile of the last 24 hours); `pct` is the ratio; `state` is ok, degraded (below the outage threshold but not
yet for the sustain time), down (an outage is open) or starting (bgpcanary isn't live yet). `upstreams` are the
neighbours the prefix's paths come in through that aren't peers: the network's transit providers, plus any network
it hasn't filed as anything, which the dashboard calls "unexpected". Events have a kind (outage, blip,
upstream_down, upstream_shift, upstream_missing, unexpected_upstream, new_upstream, chain_change, origin_hijack,
more_specific, covering_prefix, peer_leak, exchange_loss, transit_filter, registry, path_change, ...), a severity
(info, warning, critical), and are open while `end_ts` is null. Times are unix seconds; the `*_iso` copies are UTC.

Start with bgpcanary_status for "is anything wrong now"; bgpcanary_events with state "open" for what's going on;
bgpcanary_events with hours 24 for what happened; bgpcanary_event with with_report for the evidence to send a
provider; bgpcanary_neighbour for "what is this network to me". Report the numbers as they are, and don't guess at
causes the data doesn't show."""

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def _tool(name, title, description, properties=None, required=()):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required),
                            "additionalProperties": False},
            "annotations": dict(READ_ONLY, title=title)}


TOOLS = [
    _tool("bgpcanary_status", "bgpcanary: the network right now",
          "How the network looks right now: whether bgpcanary is live, each monitored prefix's state (ok, degraded, "
          "down, starting), how many RIS peers see it against its normal count (visible, baseline, pct), the "
          "neighbours its paths come in through (upstreams: transits and any unfiled network; peers: at the "
          "exchanges), the expected upstreams carrying nothing right now (missing), and how many warning or critical "
          "events are open. Start here for \"is anything wrong?\"."),
    _tool("bgpcanary_events", "bgpcanary: events",
          "The events bgpcanary recorded (outages, brief dips, hijacks, upstream changes, unexpected neighbours, "
          "registry problems...), newest first. Filters: state open (still going on) or closed; kind (as the results "
          "show it, e.g. outage, upstream_down, unexpected_upstream); severity; prefix (one monitored prefix); a time "
          "window as the last `hours`, or since/until (ISO 8601 or unix seconds), for the events going on at some "
          "point in it. Info events (brief dips, FYI notices) are left out unless include_info is true or severity is "
          "info. `total` counts every match; limit (at most 500) and offset page through them.",
          {"state": {"type": "string", "enum": ["open", "closed"], "description": "open: still going on; closed: ended"},
           "kind": {"type": "string", "description": "one kind, as the results show it"},
           "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
           "prefix": {"type": "string", "description": "one monitored prefix, e.g. 192.0.2.0/24"},
           "hours": {"type": "number", "exclusiveMinimum": 0, "description": "the last N hours"},
           "since": {"type": "string", "description": "ISO 8601 (UTC when it names no zone) or unix seconds"},
           "until": {"type": "string", "description": "ISO 8601 (UTC when it names no zone) or unix seconds"},
           "include_info": {"type": "boolean", "default": False, "description": "include the info events too"},
           "limit": {"type": "integer", "minimum": 1, "maximum": api.LIMIT_MAX, "default": 50},
           "offset": {"type": "integer", "minimum": 0, "default": 0}}),
    _tool("bgpcanary_event", "bgpcanary: one event",
          "One event in full: its fields, the kind-specific detail, the alerts sent for it (channel, delivered or "
          "not), and, with with_report, the plain-text evidence report bgpcanary builds for a ticket with the "
          "provider: the timeline, the upstream mix before and during, the raw AS paths, Route Views and RIPE Atlas "
          "results.",
          {"event_id": {"type": "integer", "minimum": 1},
           "with_report": {"type": "boolean", "default": False, "description": "include the text report"}},
          ["event_id"]),
    _tool("bgpcanary_neighbour", "bgpcanary: a neighbouring network",
          "What a neighbouring network (by AS number) is to you: its name; its role, transit (one of your expected "
          "upstreams), peer (a listed peer), exchange_peer (a peer at one of your internet exchanges, per PeeringDB), "
          "or null for a network you haven't filed as anything, which the dashboard calls \"unexpected\"; the "
          "exchange; how many RIS peers reach you through it right now; and the events about it (unexpected or new "
          "upstream, new peer, peer leak).",
          {"asn": {"type": "integer", "minimum": 1, "maximum": 4294967295}}, ["asn"]),
    _tool("bgpcanary_whoami", "bgpcanary: connection check",
          "Which bgpcanary this is and as whom you're asking: the site, its version and phase, and the account and "
          "token behind the connection. Use it when another call fails, to tell a wrong address from a revoked "
          "token."),
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ------------------------------------------------------------------------------------------------ on / off
def enabled(conn):
    return bool((db.kv_get(conn, KV) or {}).get("enabled"))


def set_enabled(conn, on):
    db.kv_set(conn, KV, {"enabled": bool(on)})
    conn.commit()


# ------------------------------------------------------------------------------------------------ the tools
class BadArguments(ValueError):
    """A tool called with arguments it can't take: the answer is a tool error the model reads and corrects."""


class InvalidParams(ValueError):
    """A request the protocol rejects (an unknown tool): a JSON-RPC error."""


def validate(schema, args):
    """A small JSON Schema check, enough for TOOLS: known names, required ones, types, enums, ranges, defaults.
    A whole number sent as a string or as 3.0 is taken as the integer: models do that."""
    if not isinstance(args, dict):
        raise BadArguments("arguments must be an object")
    props = schema["properties"]
    for k in args:
        if k not in props:
            raise BadArguments(f"unknown argument {k!r}; this tool takes " + (", ".join(props) or "no arguments"))
    for k in schema.get("required", ()):
        if args.get(k) is None:
            raise BadArguments(f"{k} is required")
    out = {}
    for k, v in args.items():
        if v is None:
            continue
        p, t = props[k], props[k]["type"]
        if t == "integer":
            if isinstance(v, bool) or not isinstance(v, int):
                if isinstance(v, float) and v.is_integer():
                    v = int(v)
                elif isinstance(v, str) and v.strip().lstrip("-").isdigit():
                    v = int(v)
                else:
                    raise BadArguments(f"{k} must be an integer")
        elif t == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    raise BadArguments(f"{k} must be a number") from None
        elif t == "boolean":
            if not isinstance(v, bool):
                raise BadArguments(f"{k} must be true or false")
        elif t == "string":
            if not isinstance(v, str):
                raise BadArguments(f"{k} must be a string")
            if "enum" in p and v not in p["enum"]:
                raise BadArguments(f"{k} must be one of: " + ", ".join(p["enum"]))
        if "minimum" in p and v < p["minimum"]:
            raise BadArguments(f"{k} must be at least {p['minimum']}")
        if "maximum" in p and v > p["maximum"]:
            raise BadArguments(f"{k} must be at most {p['maximum']}")
        if "exclusiveMinimum" in p and v <= p["exclusiveMinimum"]:
            raise BadArguments(f"{k} must be more than {p['exclusiveMinimum']}")
        out[k] = v
    for k, p in props.items():
        if k not in out and "default" in p:
            out[k] = p["default"]
    return out


def iso(ts):
    """Unix seconds as ISO 8601 UTC, which reads better than a number; None stays None."""
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def with_times(ev):
    """An event with its start and end also in ISO 8601 UTC."""
    ev = dict(ev)
    ev["start_iso"], ev["end_iso"] = iso(ev.get("start_ts")), iso(ev.get("end_ts"))
    return ev


def parse_time(s, name):
    """Unix seconds, or an ISO 8601 time (UTC when it names no zone), as unix seconds; None for nothing."""
    if s is None or str(s).strip() == "":
        return None
    s = str(s).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise BadArguments(f"{name}: give an ISO 8601 time such as 2026-10-29T14:00:00Z, or unix seconds") from None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


async def run_tool(name, args, request, u):
    """The tool's result object. BadArguments and the API's HTTPExceptions become tool errors (see call_tool)."""
    if name == "bgpcanary_status":
        conn = web.rconn()
        try:
            d = api.status_payload(conn)
        finally:
            conn.close()
        d["last_message_iso"], d["now_iso"] = iso(d.get("last_message")), iso(time.time())
        return d
    if name == "bgpcanary_events":
        since, until = parse_time(args.get("since"), "since"), parse_time(args.get("until"), "until")
        if args.get("hours") is not None and since is None:
            since = time.time() - args["hours"] * 3600
        conn = web.rconn()
        try:
            d = api.events_payload(conn, state=args.get("state", ""), kind=args.get("kind", ""),
                                   severity=args.get("severity", ""), prefix=args.get("prefix", ""), since=since,
                                   until=until, info=1 if args.get("include_info") else 0,
                                   limit=args.get("limit", 50), offset=args.get("offset", 0))
        finally:
            conn.close()
        d["events"] = [with_times(e) for e in d["events"]]
        return d
    if name == "bgpcanary_event":
        conn = web.rconn()
        try:
            d = with_times(api.event_payload(conn, args["event_id"]))
        finally:
            conn.close()
        for a in d.get("alerts", []):
            a["ts_iso"] = iso(a.get("ts"))
        if args.get("with_report"):
            resp = await web.report_response(args["event_id"])
            d["report"] = resp.body.decode("utf-8", errors="replace") if resp is not None else None
        return d
    if name == "bgpcanary_neighbour":
        conn = web.rconn()
        try:
            d = api.neighbour_payload(conn, args["asn"])
        finally:
            conn.close()
        d["events"] = [with_times(e) for e in d.get("events", [])]
        return d
    if name == "bgpcanary_whoami":
        st = web.svc.engine.status
        phase = st.get("phase") or ""
        return {"url": web.cfg.base_url or str(request.base_url).rstrip("/"), "version": __version__, "phase": phase,
                "live": phase.startswith("live"), "ris_connected": bool(st.get("ws_connected")),
                "me": {"user": u, "role": request.state.role, "auth": "token",
                       "token": getattr(request.state, "token", None)}}
    raise InvalidParams(f"unknown tool: {name!r}")


async def call_tool(params, request, u):
    name, args = params.get("name"), params.get("arguments") or {}
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        raise InvalidParams(f"unknown tool: {name!r}; the tools are " + ", ".join(TOOLS_BY_NAME))
    try:
        data = await run_tool(name, validate(spec["inputSchema"], args), request, u)
    except BadArguments as ex:
        return {"content": [{"type": "text", "text": f"{name}: {ex}"}], "isError": True}
    except HTTPException as ex:   # the API's own refusals: an unknown event, a bad prefix
        return {"content": [{"type": "text", "text": f"{name}: bgpcanary answered {ex.status_code}: {ex.detail}"}],
                "isError": True}
    log.info("MCP: %s (token %r) called %s", u, getattr(request.state, "token", None), name)
    return {"content": [{"type": "text", "text": json.dumps(data, indent=1)}], "structuredContent": data,
            "isError": False}


# ------------------------------------------------------------------------------------------------ JSON-RPC
def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def initialize(params):
    want = str(params.get("protocolVersion") or PROTOCOL_VERSIONS[1])
    return {"protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "bgpcanary", "title": "bgpcanary", "version": __version__},
            "instructions": INSTRUCTIONS}


async def dispatch(msg, request, u):
    """One JSON-RPC message -> its response object; None for a notification, or for a response from the client (this
    server never asks the client anything, so there's nothing to match it to)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request: not JSON-RPC 2.0")
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or mid is None:   # a client's response, or a notification (initialized, cancelled, ...)
        return None
    if not isinstance(method, str) or not isinstance(params, dict):
        return _error(mid, -32600, "invalid request")
    try:
        if method == "initialize":
            result = initialize(params)
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = await call_tool(params, request, u)
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            return _error(mid, -32601, f"method not found: {method}")
    except InvalidParams as ex:
        return _error(mid, -32602, str(ex))
    return {"jsonrpc": "2.0", "id": mid, "result": result}


@router.post(PATH)
async def mcp_post(request: Request):
    if not enabled(web.svc.aconn):
        return JSONResponse({"error": "the MCP server is off: an admin turns it on under Settings › MCP server"},
                            status_code=404)
    u = web.mcp_user(request)
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return JSONResponse(_error(None, -32600, "request too large"), status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse(_error(None, -32600, "request too large"), status_code=413)
    try:
        msg = json.loads(body)
    except ValueError:
        return JSONResponse(_error(None, -32700, "parse error: the body isn't JSON"), status_code=400)
    if isinstance(msg, list):   # a batch (the protocol allowed them until its 2025-06-18 revision)
        out = [r for r in [await dispatch(m, request, u) for m in msg] if r is not None]
    else:
        r = await dispatch(msg, request, u)
        out = [r] if r is not None else []
    if not out:
        return Response(status_code=202)
    return JSONResponse(out if isinstance(msg, list) else out[0])


@router.api_route(PATH, methods=["GET", "HEAD", "DELETE"])
async def mcp_other(request: Request):
    """No server-to-client stream (GET) and no sessions to end (DELETE): the protocol makes both optional."""
    return JSONResponse({"error": "this MCP server speaks JSON-RPC over POST only"}, status_code=405,
                        headers={"Allow": "POST"})


# Last, on purpose: web.py imports this module at its own end to include the router (see api.py).
from . import web  # noqa: E402
