"""The JSON API's contract (docs/API.md). The key sets below are the promise: a field renamed or removed under
/api/v1/ fails here on purpose. When you add one, add it here and to the documentation."""
import re

import pytest

from conftest import EVENTS, NOW, P1, P2, bearer, login, new_client

STATUS_KEYS = {"version", "phase", "live", "ris_connected", "last_message", "asn", "expected_upstreams",
               "open_events", "prefixes"}
PREFIX_KEYS = {"prefix", "state", "visible", "baseline", "pct", "upstreams", "peers", "missing"}
EVENT_KEYS = {"id", "kind", "kind_name", "severity", "prefix", "start_ts", "end_ts", "open", "title", "min_visible",
              "baseline", "historical", "url", "neighbour", "detail"}
EVENT_DETAIL_KEYS = EVENT_KEYS | {"alerts", "report_url"}
NEIGHBOUR_KEYS = {"asn", "name", "role", "exchange", "paths_now", "events"}
ME_KEYS = {"user", "role", "auth", "token"}
PATHS = ("/api/v1/me", "/api/v1/status", "/api/v1/events", "/api/v1/events/1", "/api/v1/events/1/report",
         "/api/v1/neighbours/64496")


# ------------------------------------------------------------------------------------------------ who may
def test_without_a_token_everything_is_401_json(client):
    for p in PATHS:
        r = client.get(p)
        assert r.status_code == 401 and r.json() == {"error": "login required"}, p


def test_a_bad_token_is_401_json(client):
    r = client.get("/api/v1/me", headers=bearer("bgpc_not-a-token-at-all"))
    assert r.status_code == 401 and r.json() == {"error": "login required"}
    r = client.get("/api/v1/me", headers={"Authorization": "Basic abc"})   # another scheme: just not signed in
    assert r.status_code == 401


def test_a_token_signs_in_with_its_owners_role(web, client, token):
    from bgpcanary import auth
    r = client.get("/api/v1/me", headers=bearer(token))
    assert r.status_code == 200 and set(r.json()) == ME_KEYS
    assert r.json() == {"user": "alice", "role": "admin", "auth": "token", "token": "test"}
    bob = auth.new_token(web.svc.aconn, "bob", "viewer's")
    assert client.get("/api/v1/me", headers=bearer(bob)).json()["role"] == "viewer"


def test_the_browsers_session_works_on_the_api_too(admin_client):
    r = admin_client.get("/api/v1/me")
    assert r.status_code == 200
    assert r.json() == {"user": "alice", "role": "admin", "auth": "session", "token": None}


def test_tokens_are_only_accepted_on_the_api(client, token):
    r = client.get("/events", headers=bearer(token), follow_redirects=False)   # a page: not signed in
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = client.get("/api/status", headers=bearer(token))                      # the pages' own route: neither
    assert r.status_code == 401


def test_role_changes_and_deletions_apply_at_once(web, client):
    from bgpcanary import auth
    conn = web.svc.aconn
    auth.set_user(conn, "gina", "test-password-123", role="viewer")
    tok = auth.new_token(conn, "gina", "x")
    assert client.get("/api/v1/me", headers=bearer(tok)).json()["role"] == "viewer"
    auth.set_role(conn, "gina", "admin")
    assert client.get("/api/v1/me", headers=bearer(tok)).json()["role"] == "admin"
    auth.delete_user(conn, "gina")
    assert client.get("/api/v1/me", headers=bearer(tok)).status_code == 401
    assert not conn.execute("SELECT 1 FROM api_tokens WHERE username='gina'").fetchone()


def test_an_account_that_must_change_its_password_is_403(web, client):
    from bgpcanary import auth
    auth.set_user(web.svc.aconn, "erin", "test-password-123", role="viewer", must_change=1)
    tok = auth.new_token(web.svc.aconn, "erin", "x")
    r = client.get("/api/v1/me", headers=bearer(tok))
    assert r.status_code == 403 and r.json() == {"error": "password change required"}


def test_bad_tokens_are_throttled_per_address(web, client, token, monkeypatch):
    from bgpcanary import auth
    monkeypatch.setattr(web, "token_throttle", auth.Throttle(limit=3, window=900, device_limit=3))
    for _ in range(3):
        assert client.get("/api/v1/me", headers=bearer("bgpc_wrong-wrong-wrong-wrong")).status_code == 401
    r = client.get("/api/v1/me", headers=bearer("bgpc_wrong-wrong-wrong-wrong"))
    assert r.status_code == 429 and "error" in r.json()
    assert client.get("/api/v1/me", headers=bearer(token)).status_code == 429   # the address is refused for a while


# ------------------------------------------------------------------------------------------------ status
def test_status(client, token):
    r = client.get("/api/v1/status", headers=bearer(token))
    assert r.status_code == 200
    d = r.json()
    assert set(d) == STATUS_KEYS
    assert d["live"] is True and d["phase"] == "live" and d["ris_connected"] is True and d["asn"] == 64500
    assert d["last_message"] == pytest.approx(NOW)
    assert d["expected_upstreams"] == [{"asn": 64496, "name": "UPSTREAM-ONE"}, {"asn": 64497, "name": "UPSTREAM-TWO"}]
    assert d["open_events"] == 1   # the outage (open info events wouldn't count)
    assert [p["prefix"] for p in d["prefixes"]] == [P1, P2]
    p1, p2 = d["prefixes"]
    assert set(p1) == set(p2) == PREFIX_KEYS
    assert (p1["state"], p1["visible"], p1["baseline"], p1["pct"]) == ("ok", 18, 18.0, 100.0)
    assert p1["upstreams"] == [{"asn": 64496, "name": "UPSTREAM-ONE", "paths": 10},
                               {"asn": 64497, "name": "UPSTREAM-TWO", "paths": 5},
                               {"asn": 64499, "name": "STRANGER", "paths": 1}]   # not filed: shown, like the dashboard
    assert p1["peers"] == [{"asn": 64498, "name": "PEER-ONE", "exchange": "Private peering", "paths": 2}]
    assert p1["missing"] == []
    assert (p2["state"], p2["visible"], p2["baseline"], p2["pct"]) == ("down", 0, None, None)
    assert [m["asn"] for m in p2["missing"]] == [64496, 64497]


# ------------------------------------------------------------------------------------------------ events
def ids(r):
    assert r.status_code == 200, r.text
    return [e["id"] for e in r.json()["events"]]


def test_events_list_and_filters(client, token):
    h = bearer(token)
    r = client.get("/api/v1/events", headers=h)
    assert set(r.json()) == {"total", "limit", "offset", "events"}
    assert r.json()["total"] == 3 and ids(r) == [1, 3, 5]                      # info left out, newest first
    assert ids(client.get("/api/v1/events?info=1", headers=h)) == [1, 2, 3, 4, 5]
    assert ids(client.get("/api/v1/events?state=open", headers=h)) == [1]
    assert ids(client.get("/api/v1/events?state=closed", headers=h)) == [3, 5]
    assert ids(client.get("/api/v1/events?severity=info", headers=h)) == [2, 4]  # asking for info gets it
    assert ids(client.get("/api/v1/events?kind=new_peer&info=1", headers=h)) == [4]
    assert ids(client.get("/api/v1/events?prefix=198.51.100.0/24", headers=h)) == [5]
    assert ids(client.get("/api/v1/events?prefix=198.51.100.0/24&info=1", headers=h)) == [4, 5]
    # since / until: the events going on at some point in the window
    assert ids(client.get(f"/api/v1/events?since={NOW - 90000}&until={NOW - 79000}", headers=h)) == [3]
    assert ids(client.get(f"/api/v1/events?since={NOW - 60}", headers=h)) == [1]  # still open: counts as now
    assert ids(client.get(f"/api/v1/events?until={NOW - 4 * 86400}&info=1", headers=h)) == [5]
    # paging
    r = client.get("/api/v1/events?limit=1", headers=h)
    assert r.json()["total"] == 3 and r.json()["limit"] == 1 and ids(r) == [1]
    assert ids(client.get("/api/v1/events?limit=1&offset=1", headers=h)) == [3]
    assert client.get("/api/v1/events?limit=9999", headers=h).json()["limit"] == 500


def test_events_bad_parameters_are_400_json(client, token):
    h = bearer(token)
    for q in ("state=bogus", "severity=loud", "prefix=not-a-prefix", "limit=ten", "since=yesterday"):
        r = client.get(f"/api/v1/events?{q}", headers=h)
        assert r.status_code == 400 and set(r.json()) == {"error"}, q
    assert client.get("/api/v1/events?limit=ten", headers=h).json()["error"].startswith("limit")


def test_event_shape(client, token):
    evs = {e["id"]: e for e in client.get("/api/v1/events?info=1", headers=bearer(token)).json()["events"]}
    assert all(set(e) == EVENT_KEYS for e in evs.values())
    outage = evs[1]
    assert outage["open"] is True and outage["end_ts"] is None and outage["start_ts"] == pytest.approx(NOW - 3600)
    assert (outage["kind"], outage["kind_name"], outage["severity"]) == ("outage", "Outage", "critical")
    assert (outage["min_visible"], outage["baseline"], outage["historical"]) == (4, 320.0, False)
    assert outage["url"] == "https://canary.example.com/events/1"
    assert outage["neighbour"] is None and outage["detail"] == {"confirm": "confirmed"}
    assert evs[3]["neighbour"] == {"asn": 64499, "name": "STRANGER"} and evs[3]["historical"] is True
    assert evs[4]["neighbour"] == {"asn": 64498, "name": "PEER-ONE"}
    assert evs[2]["open"] is False and evs[2]["kind_name"] == "Brief dip"
    for e in evs.values():
        assert isinstance(e["kind_name"], str) and e["kind_name"]


def test_one_event(client, token):
    r = client.get("/api/v1/events/1", headers=bearer(token))
    assert r.status_code == 200 and set(r.json()) == EVENT_DETAIL_KEYS
    d = r.json()
    assert d["title"] == EVENTS[0][6]
    assert d["alerts"] == [{"ts": pytest.approx(NOW - 3540), "channel": "email", "ok": True, "error": None},
                           {"ts": pytest.approx(NOW - 3540), "channel": "webhook:ops", "ok": False, "error": "HTTP 500"}]
    assert d["report_url"] == "https://canary.example.com/api/v1/events/1/report"
    r = client.get("/api/v1/events/999", headers=bearer(token))
    assert r.status_code == 404 and r.json() == {"error": "no such event"}


def test_the_text_report(web, client, token, monkeypatch):
    async def no_lookups(sourceapp, asns):   # the report asks RIPEstat about the upstreams' neighbours: not here
        return {}
    monkeypatch.setattr(web.tree, "upstream_neighbors", no_lookups)
    r = client.get("/api/v1/events/1/report", headers=bearer(token))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert P1 in r.text and "bgpcanary-event-1-" in r.headers["content-disposition"]
    assert client.get("/api/v1/events/999/report", headers=bearer(token)).status_code == 404


# ------------------------------------------------------------------------------------------------ neighbours
def test_neighbours(client, token):
    h = bearer(token)
    r = client.get("/api/v1/neighbours/64496", headers=h)
    assert r.status_code == 200 and set(r.json()) == NEIGHBOUR_KEYS
    assert r.json() == {"asn": 64496, "name": "UPSTREAM-ONE", "role": "transit", "exchange": None, "paths_now": 10,
                        "events": []}
    d = client.get("/api/v1/neighbours/64498", headers=h).json()
    assert (d["role"], d["exchange"], d["paths_now"]) == ("peer", "Private peering", 2)
    assert [e["id"] for e in d["events"]] == [4] and set(d["events"][0]) == EVENT_KEYS
    d = client.get("/api/v1/neighbours/64499", headers=h).json()
    assert (d["name"], d["role"], d["paths_now"], [e["id"] for e in d["events"]]) == ("STRANGER", None, 1, [3])
    assert client.get("/api/v1/neighbours/0", headers=h).status_code == 404
    assert client.get("/api/v1/neighbours/abc", headers=h).status_code == 400


# ------------------------------------------------------------------------------------------------ errors & health
def test_unknown_api_routes_are_404_json(client):
    r = client.get("/api/v1/nope")
    assert r.status_code == 404 and r.json() == {"error": "Not Found"}


def test_healthz_needs_no_token(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert {"ok", "version", "started", "phase", "configured"} <= set(r.json())
    assert r.json()["ok"] is True and r.json()["configured"] is True


def test_the_pages_own_status_and_loading_routes(admin_client):
    """1.3.1 had registered the banner's poll on /api/status too, over the outage check's route; each has its own now."""
    assert set(admin_client.get("/api/status").json()) == {"status", "prefixes"}
    assert set(admin_client.get("/api/loading").json()) == {"phase", "what", "html"}


# ------------------------------------------------------------------------------------------------ the token pages
TOKEN_RE = re.compile(r"bgpc_[A-Za-z0-9_-]{43}")


def test_creating_and_revoking_a_token_on_the_page(web, admin_client, client):
    from bgpcanary import auth
    r = admin_client.get("/account/tokens")
    assert r.status_code == 200 and "Create a token" in r.text
    r = admin_client.post("/account/tokens", data={"name": "  grafana   panel "})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    found = set(TOKEN_RE.findall(r.text))
    assert len(found) == 1, "the new token is shown, once (plus the curl line)"
    tok = found.pop()
    assert "grafana panel" in r.text
    assert client.get("/api/v1/me", headers=bearer(tok)).json()["token"] == "grafana panel"
    rows = auth.list_tokens(web.svc.aconn, "alice")
    assert rows[0]["name"] == "grafana panel" and rows[0]["hint"] == tok[:11]
    r = admin_client.get("/account/tokens")
    assert tok not in r.text and tok[:11] in r.text   # never shown again; its hint is
    r = admin_client.post(f"/account/tokens/{rows[0]['id']}/revoke", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/v1/me", headers=bearer(tok)).status_code == 401
    r = admin_client.post("/account/tokens", data={"name": "   "}, follow_redirects=False)
    assert r.status_code == 303 and "err=1" in r.headers["location"]


def test_viewers_may_create_tokens(viewer_client, client):
    r = viewer_client.post("/account/tokens", data={"name": "mine"})
    tok = TOKEN_RE.search(r.text).group(0)
    assert client.get("/api/v1/me", headers=bearer(tok)).json() == {"user": "bob", "role": "viewer", "auth": "token",
                                                                   "token": "mine"}


def test_at_most_twenty_tokens_per_account(web):
    from bgpcanary import auth
    conn = web.svc.aconn
    auth.set_user(conn, "hank", "test-password-123", role="viewer")
    for i in range(auth.TOKEN_MAX_PER_USER):
        auth.new_token(conn, "hank", f"t{i}")
    with pytest.raises(ValueError):
        auth.new_token(conn, "hank", "one too many")
    assert auth.revoke_tokens(conn, "hank") == auth.TOKEN_MAX_PER_USER


def test_admins_revoke_a_users_tokens_on_the_users_page(web, admin_client, client):
    from bgpcanary import auth
    tok = auth.new_token(web.svc.aconn, "bob", "to be revoked")
    assert client.get("/api/v1/me", headers=bearer(tok)).status_code == 200
    r = admin_client.get("/users")
    assert "API tokens" in r.text and "/users/bob/tokens/revoke" in r.text
    r = admin_client.post("/users/bob/tokens/revoke", follow_redirects=False)
    assert r.status_code == 303 and "Revoked" in r.headers["location"]
    assert client.get("/api/v1/me", headers=bearer(tok)).status_code == 401
    assert "/users/bob/tokens/revoke" not in admin_client.get("/users").text


def test_the_settings_card_counts_your_tokens(web, admin_client):
    from bgpcanary import auth
    n = len(auth.list_tokens(web.svc.aconn, "alice"))
    r = admin_client.get("/settings")
    assert "/account/tokens" in r.text and f"{n} token{'s' if n != 1 else ''} for <strong>alice</strong>" in r.text


def test_a_viewers_session_sees_the_api(web):
    c = login(web, "bob")
    assert c.get("/api/v1/status").status_code == 200
    assert c.get("/api/v1/me").json()["role"] == "viewer"
    c2 = new_client(web)
    assert c2.get("/api/v1/status").status_code == 401
