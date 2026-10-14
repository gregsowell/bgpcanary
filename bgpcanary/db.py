"""SQLite storage. Single-writer (the collector loop); web reads use separate connections."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    source TEXT NOT NULL,          -- ris-live | bgplay | resync
    collector TEXT NOT NULL,
    peer TEXT NOT NULL,
    peer_asn INTEGER,
    prefix TEXT NOT NULL,
    kind TEXT NOT NULL,            -- A | W
    path TEXT,
    communities TEXT
);
CREATE INDEX IF NOT EXISTS idx_updates_ts ON updates(ts);
CREATE INDEX IF NOT EXISTS idx_updates_prefix_peer ON updates(prefix, collector, peer, ts);

CREATE TABLE IF NOT EXISTS buckets (
    prefix TEXT NOT NULL,
    ts INTEGER NOT NULL,           -- bucket start (epoch seconds)
    visible INTEGER NOT NULL,
    baseline REAL,
    min_visible INTEGER,
    chains TEXT,                   -- json {"64510 64502 64501": n, ...}  (transit chain -> count)
    upstreams TEXT,                -- json {"64501": n}
    updates INTEGER DEFAULT 0,
    withdrawals INTEGER DEFAULT 0,
    PRIMARY KEY (prefix, ts)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,            -- outage | blip | new_upstream | origin_hijack | more_specific | chain_change | unexpected_prefix | collector
    severity TEXT NOT NULL,        -- info | warning | critical
    prefix TEXT,
    start_ts REAL NOT NULL,
    end_ts REAL,
    title TEXT NOT NULL,
    detail TEXT,                   -- json
    min_visible INTEGER,
    baseline REAL,
    alerted INTEGER DEFAULT 0,
    historical INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ts);

CREATE TABLE IF NOT EXISTS rv_snapshots (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    server TEXT NOT NULL,
    prefix TEXT NOT NULL,
    n_paths INTEGER,
    paths TEXT,                    -- json list of path strings
    raw TEXT,
    event_id INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_rv_ts ON rv_snapshots(ts);

CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    server TEXT NOT NULL,
    kind TEXT NOT NULL,            -- ping | traceroute
    target TEXT NOT NULL,
    trigger TEXT NOT NULL,         -- scheduled | event | manual
    event_id INTEGER,
    success_pct REAL,
    rtt_avg REAL,
    last_hop TEXT,
    last_hop_asn INTEGER,
    reached INTEGER,
    output TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_probes_ts ON probes(ts);

CREATE TABLE IF NOT EXISTS asn_names (
    asn INTEGER PRIMARY KEY,
    name TEXT,
    fetched REAL
);

CREATE TABLE IF NOT EXISTS seen_upstreams (
    prefix TEXT NOT NULL,
    asn INTEGER NOT NULL,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (prefix, asn)
);

CREATE TABLE IF NOT EXISTS seen_chains (
    prefix TEXT NOT NULL,
    chain TEXT NOT NULL,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (prefix, chain)
);

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    pw_hash TEXT NOT NULL,
    created REAL,
    role TEXT NOT NULL DEFAULT 'admin',      -- admin | viewer
    must_change INTEGER NOT NULL DEFAULT 0   -- 1 = must set a new password at next login
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    event_id INTEGER,
    channel TEXT,
    ok INTEGER,
    error TEXT
);
"""


class _Conn(sqlite3.Connection):
    """A connection that's safe to share between the event loop and worker threads (the history backfill writes
    from a thread while startup tasks write from the loop). sqlite3 checks "is a transaction open?" and then
    issues BEGIN for each write; two threads doing that at once failed with "cannot start a transaction within
    a transaction" and abandoned the backfill. Each statement and commit now runs under one lock, so the second
    writer simply joins the open transaction."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._lock = threading.RLock()

    def execute(self, *a, **k):
        with self._lock:
            return super().execute(*a, **k)

    def executemany(self, *a, **k):
        with self._lock:
            return super().executemany(*a, **k)

    def executescript(self, *a, **k):
        with self._lock:
            return super().executescript(*a, **k)

    def commit(self):
        with self._lock:
            return super().commit()

    def rollback(self):
        with self._lock:
            return super().rollback()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False, factory=_Conn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(path: str) -> None:
    conn = connect(path)
    conn.executescript(SCHEMA)
    # migrations for databases created by older versions
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "role" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")  # existing users = admins
    if "must_change" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


def kv_get(conn, k, default=None):
    r = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(conn, k, v):
    conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def state_at(conn, prefix, ts):
    """Latest update per (collector, peer) for `prefix` at or before `ts`."""
    return conn.execute(
        "SELECT collector, peer, peer_asn, kind, path, ts FROM ("
        " SELECT collector, peer, peer_asn, kind, path, ts,"
        "  ROW_NUMBER() OVER (PARTITION BY collector, peer ORDER BY ts DESC, id DESC) rn"
        " FROM updates WHERE prefix=? AND ts<=?) WHERE rn=1", (prefix, ts)).fetchall()


def annotate_probes(conn, rows):
    """Probe rows as dicts; traceroutes that didn't reach the target get ping_ok=True when a ping to the same
    target from the same collector within 15 min got replies, i.e. the target is fine and the routers past the
    last hop just don't answer traceroute (bgpcanary stops after a few silent hops)."""
    out = []
    for r in rows:
        d = dict(r)
        if d["kind"] == "traceroute" and not d["reached"] and not d["error"]:
            d["ping_ok"] = conn.execute(
                "SELECT 1 FROM probes WHERE kind='ping' AND target=? AND server=? AND ABS(ts-?)<=900 "
                "AND success_pct>0 LIMIT 1", (d["target"], d["server"], d["ts"])).fetchone() is not None
        out.append(d)
    return out


def prune(conn, raw_retention_days: int):
    """Drop raw updates older than the retention window, keeping a checkpoint of the RIB at the cutoff
    so event replays can still reconstruct state."""
    cutoff = time.time() - raw_retention_days * 86400
    conn.execute(
        "INSERT INTO updates(ts,source,collector,peer,peer_asn,prefix,kind,path,communities) "
        "SELECT ?, 'checkpoint', collector, peer, peer_asn, prefix, kind, path, NULL FROM ("
        " SELECT *, ROW_NUMBER() OVER (PARTITION BY prefix, collector, peer ORDER BY ts DESC, id DESC) rn"
        " FROM updates WHERE ts < ?) WHERE rn=1 AND kind='A'", (cutoff, cutoff))
    conn.execute("DELETE FROM updates WHERE ts < ?", (cutoff,))
    conn.commit()
