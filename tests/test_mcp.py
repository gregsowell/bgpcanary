"""The MCP server (/mcp): the protocol by hand through the test client (sign-in rules, the on/off switch, JSON-RPC,
the tools, errors), and interoperability with the official MCP client over real HTTP (skipped without the `mcp`
package; CI installs it)."""
import json
import threading
import time

import pytest

from conftest import NOW, P1, bearer, login, new_client

TOOLS = {"bgpcanary_status", "bgpcanary_events", "bgpcanary_event", "bgpcanary_neighbour", "bgpcanary_whoami"}


def rpc(client, method, params=None, mid=1, headers=None):
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if mid is not None:
        msg["id"] = mid
    return client.post("/mcp", json=msg, headers=headers or {})


def result(r):
    assert r.status_code == 200, r.text
    d = r.json()
    assert "error" not in d, d
    return d["result"]


def call(client, headers, name, **args):
    return result(rpc(client, "tools/call", {"name": name, "arguments": args}, headers=headers))


@pytest.fixture
def on(web):
    from bgpcanary import mcp
    mcp.set_enabled(web.svc.aconn, True)
    yield
    mcp.set_enabled(web.svc.aconn, False)


# ------------------------------------------------------------------------------------------------ who may
def test_off_by_default_answers_404_to_everyone(web, client, token):
    from bgpcanary import mcp
    assert mcp.enabled(web.svc.aconn) is False
    r = rpc(client, "ping", headers=bearer(token))
    assert r.status_code == 404 and "Settings" in r.json()["error"]


def test_on_it_takes_a_token_and_nothing_else(on, client, admin_client, token):
    r = rpc(client, "ping")
    assert r.status_code == 401 and r.json() == {"error": "login required"}
    assert r.headers["www-authenticate"].startswith("Bearer")
    r = rpc(admin_client, "ping")   # the browser's session doesn't count on /mcp
    assert r.status_code == 401
    r = rpc(client, "ping", headers=bearer("bgpc_not-a-token"))
    assert r.status_code == 401
    assert result(rpc(client, "ping", headers=bearer(token))) == {}


def test_only_post(on, client, token):
    r = client.get("/mcp", headers=bearer(token))
    assert r.status_code == 405 and r.headers["allow"] == "POST"
    assert client.delete("/mcp", headers=bearer(token)).status_code == 405


def test_admins_switch_it_on_and_off(web, admin_client, viewer_client, client, token):
    from bgpcanary import mcp
    r = admin_client.get("/settings/mcp")
    assert r.status_code == 200 and "/mcp" in r.text and "Serve the MCP endpoint" in r.text
    r = viewer_client.get("/settings/mcp")
    assert r.status_code == 200 and "Serve the MCP endpoint" not in r.text
    assert viewer_client.post("/settings/mcp", data={"enabled": "1"}, follow_redirects=False).status_code == 403
    r = admin_client.post("/settings/mcp", data={"enabled": "1"}, follow_redirects=False)
    assert r.status_code == 303 and mcp.enabled(web.svc.aconn) is True
    assert rpc(client, "ping", headers=bearer(token)).status_code == 200
    assert "badge b-ok\">on" in admin_client.get("/settings").text
    r = admin_client.post("/settings/mcp", data={}, follow_redirects=False)
    assert r.status_code == 303 and mcp.enabled(web.svc.aconn) is False
    assert rpc(client, "ping", headers=bearer(token)).status_code == 404


# ------------------------------------------------------------------------------------------------ the protocol
def test_initialize_and_the_handshake(on, client, token):
    h = bearer(token)
    d = result(rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "0"}}, headers=h))
    assert d["protocolVersion"] == "2025-06-18"
    assert d["capabilities"] == {"tools": {"listChanged": False}}
    assert d["serverInfo"]["name"] == "bgpcanary" and d["serverInfo"]["version"]
    assert "bgpcanary_status" in d["instructions"]
    assert result(rpc(client, "initialize", {"protocolVersion": "1999-01-01"}, headers=h))["protocolVersion"] == "2026-07-28"
    assert result(rpc(client, "initialize", {}, headers=h))["protocolVersion"] == "2025-03-26"
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    assert r.status_code == 202 and r.content == b""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "result": {}}, headers=h)   # a client's response
    assert r.status_code == 202


def test_tools_list(on, client, token):
    tools = {t["name"]: t for t in result(rpc(client, "tools/list", headers=bearer(token)))["tools"]}
    assert set(tools) == TOOLS
    for t in tools.values():
        assert t["description"] and t["title"], t["name"]
        assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["openWorldHint"] is False
        assert t["inputSchema"]["type"] == "object"
    ev = tools["bgpcanary_events"]["inputSchema"]
    assert set(ev["properties"]) == {"state", "kind", "severity", "prefix", "hours", "since", "until", "include_info",
                                     "limit", "offset"} and ev["required"] == []
    assert tools["bgpcanary_event"]["inputSchema"]["required"] == ["event_id"]
    assert tools["bgpcanary_neighbour"]["inputSchema"]["required"] == ["asn"]


def test_errors_the_protocol_way(on, client, token):
    h = bearer(token)
    r = rpc(client, "nope/method", headers=h)
    assert r.status_code == 200 and r.json()["error"]["code"] == -32601
    r = rpc(client, "tools/call", {"name": "bgpcanary_fly", "arguments": {}}, headers=h)
    assert r.json()["error"]["code"] == -32602 and "unknown tool" in r.json()["error"]["message"]
    r = client.post("/mcp", content=b"{not json", headers=dict(h, **{"Content-Type": "application/json"}))
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700 and r.json()["id"] is None
    r = client.post("/mcp", json={"hello": "world"}, headers=h)
    assert r.status_code == 200 and r.json()["error"]["code"] == -32600
    r = client.post("/mcp", content=b"x" * (256 * 1024 + 1), headers=dict(h, **{"Content-Type": "application/json"}))
    assert r.status_code == 413
    for extra in ("resources/list", "prompts/list", "resources/templates/list"):   # friendly to clients that ask anyway
        assert list(result(rpc(client, extra, headers=h)).values()) == [[]]


def test_a_batch(on, client, token):
    h = bearer(token)
    r = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                  {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                  {"jsonrpc": "2.0", "id": "two", "method": "tools/list"}], headers=h)
    assert r.status_code == 200 and [x["id"] for x in r.json()] == [1, "two"]
    r = client.post("/mcp", json=[{"jsonrpc": "2.0", "method": "notifications/initialized"}], headers=h)
    assert r.status_code == 202


# ------------------------------------------------------------------------------------------------ the tools
def test_status(on, client, token):
    d = call(client, bearer(token), "bgpcanary_status")
    assert d["isError"] is False and d["content"][0]["type"] == "text"
    assert json.loads(d["content"][0]["text"]) == d["structuredContent"]
    s = d["structuredContent"]
    assert s["live"] is True and s["asn"] == 64500 and s["open_events"] == 1
    assert [p["state"] for p in s["prefixes"]] == ["ok", "down"]
    assert s["last_message_iso"].endswith("Z") and s["now_iso"].endswith("Z")


def test_events_filters_and_time_windows(on, client, token):
    from bgpcanary import mcp
    h = bearer(token)
    ids = lambda d: [e["id"] for e in d["structuredContent"]["events"]]
    assert ids(call(client, h, "bgpcanary_events")) == [1, 3, 5]
    assert ids(call(client, h, "bgpcanary_events", state="open")) == [1]
    assert ids(call(client, h, "bgpcanary_events", include_info=True)) == [1, 2, 3, 4, 5]
    assert ids(call(client, h, "bgpcanary_events", hours=3)) == [1]
    assert ids(call(client, h, "bgpcanary_events", hours=3, include_info=True)) == [1, 2]
    assert ids(call(client, h, "bgpcanary_events", since=mcp.iso(NOW - 90000), until=mcp.iso(NOW - 79000))) == [3]
    assert ids(call(client, h, "bgpcanary_events", since=str(NOW - 90000), until=str(NOW - 79000))) == [3]
    assert ids(call(client, h, "bgpcanary_events", prefix="198.51.100.0/24", include_info=True)) == [4, 5]
    d = call(client, h, "bgpcanary_events", limit=1)
    assert d["structuredContent"]["total"] == 3 and ids(d) == [1]
    assert ids(call(client, h, "bgpcanary_events", limit="2")) == [1, 3]   # a number as a string: taken
    e = call(client, h, "bgpcanary_events", state="open")["structuredContent"]["events"][0]
    assert e["start_iso"] == mcp.iso(e["start_ts"]) and e["end_iso"] is None and e["open"] is True
    for args, words in ((dict(since="last tuesday"), "ISO 8601"), (dict(hours=-1), "more than 0"),
                        (dict(state="bogus"), "one of"), (dict(colour="red"), "unknown argument"),
                        (dict(limit=9999), "at most 500"), (dict(prefix="junk"), "400")):
        d = call(client, h, "bgpcanary_events", **args)
        assert d["isError"] is True and words in d["content"][0]["text"], (args, d)


def test_one_event_with_its_report(on, web, client, token, monkeypatch):
    async def no_lookups(sourceapp, asns):   # the report asks RIPEstat about the upstreams' neighbours: not here
        return {}
    monkeypatch.setattr(web.tree, "upstream_neighbors", no_lookups)
    h = bearer(token)
    d = call(client, h, "bgpcanary_event", event_id=1)["structuredContent"]
    assert d["id"] == 1 and d["kind"] == "outage" and "report" not in d
    assert [a["channel"] for a in d["alerts"]] == ["email", "webhook:ops"] and d["alerts"][0]["ts_iso"].endswith("Z")
    d = call(client, h, "bgpcanary_event", event_id=1, with_report=True)["structuredContent"]
    assert P1 in d["report"]
    d = call(client, h, "bgpcanary_event", event_id=999)
    assert d["isError"] is True and "404" in d["content"][0]["text"] and "no such event" in d["content"][0]["text"]
    d = call(client, h, "bgpcanary_event")
    assert d["isError"] is True and "event_id is required" in d["content"][0]["text"]


def test_a_neighbour(on, client, token):
    h = bearer(token)
    d = call(client, h, "bgpcanary_neighbour", asn=64498)["structuredContent"]
    assert (d["role"], d["exchange"], d["paths_now"]) == ("peer", "Private peering", 2)
    assert [e["id"] for e in d["events"]] == [4] and d["events"][0]["end_iso"]
    d = call(client, h, "bgpcanary_neighbour", asn=64499)["structuredContent"]
    assert d["role"] is None and d["name"] == "STRANGER"
    d = call(client, h, "bgpcanary_neighbour", asn=0)
    assert d["isError"] is True and "at least 1" in d["content"][0]["text"]


def test_whoami(on, client, token):
    d = call(client, bearer(token), "bgpcanary_whoami")["structuredContent"]
    assert d["url"] == "https://canary.example.com" and d["version"] and d["live"] is True
    assert d["me"] == {"user": "alice", "role": "admin", "auth": "token", "token": "test"}


def test_time_parsing():
    from bgpcanary import mcp
    assert mcp.parse_time(None, "x") is None and mcp.parse_time("", "x") is None
    assert mcp.parse_time("1761780000", "x") == 1761780000.0
    assert mcp.parse_time("2026-10-29T14:00:00Z", "x") == 1793282400.0
    assert mcp.parse_time("2026-10-29T14:00:00", "x") == 1793282400.0           # no zone: UTC
    assert mcp.parse_time("2026-10-29T09:00:00-05:00", "x") == 1793282400.0
    assert mcp.iso(1793282400) == "2026-10-29T14:00:00Z" and mcp.iso(None) is None
    with pytest.raises(mcp.BadArguments):
        mcp.parse_time("yesterday", "since")


# ------------------------------------------------------------------------------------------------ the real client
@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def served(web, on):
    """The app on a real port, the way an assistant reaches it (uvicorn, no lifespan: no background tasks)."""
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=0, lifespan="off", log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    t.join(timeout=10)


@pytest.mark.anyio
async def test_the_official_client(served, token):
    pytest.importorskip("mcp")
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    hc = httpx2.AsyncClient(headers=bearer(token))
    async with Client(streamable_http_client(served, http_client=hc), raise_exceptions=True) as c:
        assert c.instructions and "bgpcanary" in c.instructions
        assert c.server_info.name == "bgpcanary"
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert set(tools) == TOOLS and tools["bgpcanary_status"].annotations.read_only_hint is True
        r = await c.call_tool("bgpcanary_status", {})
        assert not r.is_error and [p["state"] for p in r.structured_content["prefixes"]] == ["ok", "down"]
        r = await c.call_tool("bgpcanary_events", {"state": "open"})
        assert [e["id"] for e in r.structured_content["events"]] == [1]
        r = await c.call_tool("bgpcanary_neighbour", {"asn": 64496})
        assert r.structured_content["role"] == "transit"
        r = await c.call_tool("bgpcanary_event", {"event_id": 999})
        assert r.is_error and "404" in r.content[0].text


@pytest.mark.anyio
async def test_the_official_client_is_refused_without_a_token(served):
    pytest.importorskip("mcp")
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    with pytest.raises(Exception):
        async with Client(streamable_http_client(served)) as c:
            await c.list_tools()


def test_a_viewers_token_works_too(web, on, client):
    from bgpcanary import auth
    tok = auth.new_token(web.svc.aconn, "bob", "assistant")
    d = call(client, bearer(tok), "bgpcanary_whoami")["structuredContent"]
    assert d["me"]["role"] == "viewer"
    c = login(web, "bob")
    assert rpc(c, "ping").status_code == 401   # still: a session is no good here
    assert new_client(web).post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
