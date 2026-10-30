"""Shared fixtures: the web app in-process (Starlette's test client) on a temporary database, with no network.

bgpcanary.web builds the service when it's imported, from the config file BGPCANARY_CONFIG names, so the environment
is set here, before any test imports it. The service's background tasks (RIS Live, gap fills, update checks) only start
with the app's lifespan, which the test client never runs; the engine's live state is seeded directly instead (seed()).

Addresses and AS numbers are the documentation ones (192.0.2.0/24, AS64496...), so nothing here is a real network."""
import json
import os
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="bgpcanary-tests-")
CONFIG = os.path.join(TMP, "config.toml")
with open(CONFIG, "w") as f:
    f.write(f"""[monitor]
asn = 64500
prefixes = ["192.0.2.0/24", "198.51.100.0/24"]
expected_upstreams = [64496, 64497]
peers = [64498]
[storage]
db_path = {json.dumps(os.path.join(TMP, "test.db"))}
[web]
secret_key = "{'t' * 64}"
site_name = "Test Canary"
base_url = "https://canary.example.com"
[routeviews]
servers = []
poll_minutes = 0
probe_minutes = 0
probe_on_event = false
""")
os.environ["BGPCANARY_CONFIG"] = CONFIG
os.environ["BGPCANARY_INSECURE_COOKIES"] = "1"   # the test client speaks plain http

PASSWORD = "test-password-123"   # every test account's password (a fixture value, not a secret)
NOW = time.time()
P1, P2 = "192.0.2.0/24", "198.51.100.0/24"
NAMES = {64496: "UPSTREAM-ONE", 64497: "UPSTREAM-TWO", 64498: "PEER-ONE", 64499: "STRANGER", 64510: "FAR-AWAY"}
# id, kind, severity, prefix, start, end, title, detail, min_visible, baseline, historical
EVENTS = [
    (1, "outage", "critical", P1, NOW - 3600, None, "Outage: 192.0.2.0/24 seen by 4 of 320", {"confirm": "confirmed"}, 4, 320.0, 0),
    (2, "blip", "info", P1, NOW - 7200, NOW - 7150, "Brief dip: 192.0.2.0/24", {"duration": 50}, 300, 320.0, 0),
    (3, "unexpected_upstream", "warning", P1, NOW - 86400, NOW - 80000, "Unexpected upstream AS64499",
     {"key": f"{P1}|64499", "asn": 64499}, None, None, 1),
    (4, "new_peer", "info", P2, NOW - 2 * 86400, NOW - 2 * 86400 + 600, "New peer AS64498", {"asn": 64498}, None, None, 0),
    (5, "chain_change", "warning", P2, NOW - 5 * 86400, NOW - 5 * 86400 + 900, "Chain change", {}, 200, 210.0, 0),
]


def seed(w):
    """Accounts, names, events and the engine's live state: 18 RIS peers reach 192.0.2.0/24 (10 through AS64496, 5
    through AS64497, 2 through the peer AS64498 and 1 through AS64499, which isn't filed as anything), its baseline
    is 18; 198.51.100.0/24 is down with no paths at all."""
    from bgpcanary import auth
    from bgpcanary.engine import Entry
    conn = w.svc.aconn
    auth.set_user(conn, "alice", PASSWORD, role="admin")
    auth.set_user(conn, "bob", PASSWORD, role="viewer")
    conn.executemany("INSERT OR REPLACE INTO asn_names(asn, name) VALUES(?,?)", list(NAMES.items()))
    conn.executemany("INSERT INTO events(id,kind,severity,prefix,start_ts,end_ts,title,detail,min_visible,baseline,historical) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     [(i, k, s, p, st, en, t, json.dumps(d), mv, b, h) for i, k, s, p, st, en, t, d, mv, b, h in EVENTS])
    conn.executemany("INSERT INTO alert_log(ts, event_id, channel, ok, error) VALUES(?,?,?,?,?)",
                     [(NOW - 3540, 1, "email", 1, None), (NOW - 3540, 1, "webhook:ops", 0, "HTTP 500")])
    conn.commit()
    eng = w.svc.engine
    eng.status.update(phase="live", ws_connected=True, last_msg=NOW)
    rib = {}
    for i, (path, peer_asn) in enumerate([([64510, 64496, 64500], 64510)] * 10 + [([64511, 64497, 64500], 64511)] * 5
                                         + [([64498, 64500], 64498)] * 2 + [([64499, 64500], 64499)]):
        rib[("rrc00", f"203.0.113.{i + 1}")] = Entry(path=path, ts=NOW, peer_asn=peer_asn)
    eng.rib[P1] = rib
    eng.rib_ver[P1] = 1
    for i in range(25):
        eng.mons[P1].add_sample(NOW - 30 * i, 18)
    eng.mons[P2].state = "down"


@pytest.fixture(scope="session")
def web():
    from bgpcanary import web as w   # (the config above, a fresh database)
    seed(w)
    return w


def new_client(web):
    from starlette.testclient import TestClient
    c = TestClient(web.app)   # no `with`: the lifespan, and so the background tasks, never run
    c.headers["Origin"] = "http://testserver"   # the CSRF check wants a browser's Origin on every POST
    return c


def login(web, username):
    c = new_client(web)
    r = c.post("/login", data={"username": username, "password": PASSWORD, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and "bgpcanary" in c.cookies, r.text[:300]
    return c


@pytest.fixture(scope="session")
def client(web):
    """Not signed in."""
    return new_client(web)


@pytest.fixture(scope="session")
def admin_client(web):
    return login(web, "alice")


@pytest.fixture(scope="session")
def viewer_client(web):
    return login(web, "bob")


@pytest.fixture
def token(web):
    """A fresh token of alice's (an admin)."""
    from bgpcanary import auth
    return auth.new_token(web.svc.aconn, "alice", "test")


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}
