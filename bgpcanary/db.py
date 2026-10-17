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
    chains TEXT,                   -- json {"64510 64502 64501": n, ...}  (transit chain -> count), NULL = unchanged
    upstreams TEXT,                -- json {"64501": n}, NULL = unchanged (the same as the bucket before: see MIX)
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

-- Signed-in browsers. The cookie carries a random session id; only its SHA-256 is stored, and a session is valid
-- only while its row exists, so signing out (or a password change) ends it for good, even for a copied cookie.
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,                     -- sha256(session id)
    username TEXT NOT NULL,
    created REAL NOT NULL,
    seen REAL NOT NULL,
    ip TEXT,
    agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);

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
    if "session_ver" not in cols:   # bumped on password / role changes: older sessions stop working
        conn.execute("ALTER TABLE users ADD COLUMN session_ver INTEGER NOT NULL DEFAULT 0")
    # Before 1.2.2 a failed webhook's error text could include its (secret) URL: keep only scheme + host.
    from .alerts import mask_urls
    for r in conn.execute("SELECT id, error FROM alert_log WHERE error LIKE '%http%://%'").fetchall():
        masked = mask_urls(r["error"])
        if masked != r["error"]:
            conn.execute("UPDATE alert_log SET error=? WHERE id=?", (masked, r["id"]))
    # Buckets stored before 1.3.2 keep their chains / upstreams in every row; the service compacts them in the
    # background (Service._compact_buckets). A new database has none.
    if kv_get(conn, COMPACT_KV) is None and not conn.execute("SELECT 1 FROM buckets LIMIT 1").fetchone():
        kv_set(conn, COMPACT_KV, True)
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


# A bucket stores its chains / upstreams JSON only when it changes: NULL means "the same as this prefix's bucket
# before" (Engine.tick). The first bucket of each UTC day and the first after any break store it anyway, so the
# value in effect is never more than a day back. Read buckets through fill_mix to get it in every row.
MIX = ("chains", "upstreams")
COMPACT_KV = "buckets_compacted"   # older buckets' migration: [prefix, ts] done so far, "vacuum", then true


def mix_at(conn, prefix, ts, col="chains"):
    """The chains (or upstreams) JSON in effect at bucket `ts` of `prefix`: the newest one stored at or before it."""
    r = conn.execute(f"SELECT {col} FROM buckets WHERE prefix=? AND ts<=? AND {col} IS NOT NULL "
                     "ORDER BY ts DESC LIMIT 1", (prefix, ts)).fetchone()
    return r[0] if r else None


def fill_mix(conn, rows, prefix=None):
    """Bucket rows, oldest first, as dicts with every NULL chains / upstreams filled in from the bucket before.
    The rows are all of `prefix`, or have a prefix column."""
    out, last = [], {}
    for r in rows:
        d = dict(r)
        for col in MIX:
            if col in d:
                k = (d.get("prefix", prefix), col)
                if d[col] is not None:
                    last[k] = d[col]
                elif k in last:
                    d[col] = last[k]
                else:   # the first bucket asked for relies on one before it
                    d[col] = last[k] = mix_at(conn, k[0], d["ts"], col)
        out.append(d)
    return out


def pin_mix(conn, prefix, start, end):
    """Store the chains / upstreams in effect on each bucket of `prefix` from `start` to `end` that relies on the one
    before it, so a replay that rewrites some of them (a gap fill) can't change what the others show."""
    rows = conn.execute("SELECT ts, chains, upstreams FROM buckets WHERE prefix=? AND ts>=? AND ts<=? ORDER BY ts",
                        (prefix, start, end)).fetchall()
    conn.executemany("UPDATE buckets SET chains=?, upstreams=? WHERE prefix=? AND ts=?",
                     [(d["chains"], d["upstreams"], prefix, d["ts"]) for r, d in zip(rows, fill_mix(conn, rows, prefix))
                      if r["chains"] is None or r["upstreams"] is None])


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


def compact_mix(conn, B, at, limit=1000):
    """One step of the migration of buckets stored before 1.3.2, which keep their chains / upstreams in every row:
    from the bucket after `at` = (prefix, ts), in that order, drop them where Engine.tick now would. Returns (where
    to go on from, or None once every bucket is done; buckets changed)."""
    with conn._lock:   # read and update as one step: gap fills write buckets from a worker thread
        rows = conn.execute("SELECT * FROM buckets WHERE (prefix, ts) > (?, ?) ORDER BY prefix, ts LIMIT ?",
                            (*at, limit)).fetchall()
        prev, todo = None, []
        if rows and rows[0]["prefix"] == at[0]:   # carrying on within a prefix: its bucket before, as it shows
            prev = {"prefix": at[0], "ts": at[1], **{c: mix_at(conn, at[0], at[1], c) for c in MIX}}
        for r in rows:
            if prev and prev["prefix"] != r["prefix"]:
                prev = None
            # the bucket before, on the same UTC day (within a minute: they may have been recorded at another size)
            same = prev and r["ts"] - prev["ts"] <= max(B, 60) and r["ts"] // 86400 == prev["ts"] // 86400
            cur, new = {"prefix": r["prefix"], "ts": r["ts"]}, None
            for c in MIX:
                cur[c] = r[c] if r[c] is not None else prev[c] if prev else mix_at(conn, r["prefix"], r["ts"], c)
                if same and r[c] is not None and r[c] == prev[c]:
                    new = new or dict(r)
                    new[c] = None
            if new:
                todo.append(new)
            prev = cur
        # Each one is written again whole: an UPDATE that only shrinks a row leaves its page almost empty, and then
        # the database looks many times bigger than it is until a VACUUM (whose check for room would refuse it).
        if todo:
            cols = list(todo[0])
            conn.executemany(f"INSERT OR REPLACE INTO buckets({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                             [[d[c] for c in cols] for d in todo])
        conn.commit()
    return ((rows[-1]["prefix"], rows[-1]["ts"]) if len(rows) == limit else None), len(todo)


def vacuum(conn):
    """Rewrite the database file without its free space (VACUUM), then empty the WAL. Everything else that uses `conn`
    waits meanwhile."""
    with conn._lock:
        conn.commit()   # (VACUUM can't run inside a transaction)
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
