"""Local user accounts (scrypt hashes) + login throttling."""
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from collections import OrderedDict

N, R, P = 2 ** 14, 8, 1


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


ROLES = ("admin", "viewer")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{2,64}$")
MIN_PASSWORD = 10


def set_user(conn, username, pw, role=None, must_change=0):
    """Create a user or reset its password. `role` None keeps an existing user's role (new users: admin).
    Every password change signs the account out everywhere (see session_ver)."""
    if role is not None and role not in ROLES:
        raise ValueError("bad role")
    # A new account starts at a random session version, so if a deleted user's name is created again, cookies
    # from the old account (still signed, up to 7 days old) don't sign in to the new one.
    conn.execute("INSERT INTO users(username,pw_hash,created,role,must_change,session_ver) VALUES(?,?,?,?,?,?) "
                 "ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash, must_change=excluded.must_change, "
                 "session_ver=session_ver+1" + (", role=excluded.role" if role else ""),
                 (username, hash_password(pw), time.time(), role or "admin", must_change,
                  secrets.randbelow(2 ** 31) + 1))
    conn.commit()


def get_user(conn, username):
    return conn.execute("SELECT username, role, must_change, created, session_ver FROM users WHERE username=?",
                        (username,)).fetchone()


def list_users(conn):
    return conn.execute("SELECT username, role, must_change, created FROM users ORDER BY username").fetchall()


def admin_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()["c"]


def set_role(conn, username, role):
    if role not in ROLES:
        raise ValueError("bad role")
    conn.execute("UPDATE users SET role=?, session_ver=session_ver+1 WHERE username=?", (role, username))
    conn.commit()


def sign_out_everywhere(conn, username):
    conn.execute("UPDATE users SET session_ver=session_ver+1 WHERE username=?", (username,))
    conn.commit()


def delete_user(conn, username):
    conn.execute("DELETE FROM users WHERE username=?", (username,))
    conn.commit()


def ensure_default_admin(conn):
    """If there are no users at all, create 'admin' with a random password that must be changed on first
    login. Returns the password if an account was created, else None."""
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        return None
    pw = secrets.token_urlsafe(12)
    set_user(conn, "admin", pw, role="admin", must_change=1)
    return pw


def check_user(conn, username, pw) -> bool:
    r = conn.execute("SELECT pw_hash FROM users WHERE username=?", (username,)).fetchone()
    if not r:
        hash_password(pw)  # equalize timing
        return False
    return verify_password(pw, r["pw_hash"])


class Throttle:
    """Login attempts allowed per `window` seconds, counted *before* the password is checked (so sending many at once
    doesn't get around a limit) and given back by a successful login:

    - `limit` per client address; IPv6 clients by /64, so rotating through a subnet doesn't help;
    - `user_limit` per account, against guessing spread over many addresses. Browsers that have signed in to the
      account before (a "known device" cookie) don't count against it, so someone hammering a known username can't
      lock its owner out; each known device has its own smaller budget (`device_limit`) instead.

    Memory is bounded: least recently active entries are dropped first (O(1)), and expired ones are swept every 30 s.
    """

    MAX_KEYS = 20000

    def __init__(self, limit=5, window=900, user_limit=20, device_limit=10):
        self.limit, self.window, self.user_limit, self.device_limit = limit, window, user_limit, device_limit
        self.hits: OrderedDict[str, list] = OrderedDict()   # key -> attempt times, least recently active first
        self.noted: OrderedDict[str, float] = OrderedDict()  # refusals already logged this window
        self._swept = 0.0

    @staticmethod
    def client_key(ip):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return "ip:" + str(ip)[:64]
        if a.version == 6:
            if a.ipv4_mapped:   # ::ffff:192.0.2.1 is just an IPv4 client
                return "ip:" + str(a.ipv4_mapped)
            return "net:" + str(ipaddress.ip_network(f"{a}/64", strict=False))
        return "ip:" + str(a)

    def _count(self, key, now):
        return sum(1 for t in self.hits.get(key, ()) if t > now - self.window)

    def _add(self, key, now):
        lst = [t for t in self.hits.pop(key, ()) if t > now - self.window]
        lst.append(now)
        self.hits[key] = lst   # re-inserted: now the most recently active

    def _sweep(self, now):   # expired entries, at most every 30 s
        if now - self._swept >= 30:
            self._swept = now
            for d in (self.hits, self.noted):
                for k in [k for k, v in d.items() if (v[-1] if isinstance(v, list) else v) <= now - self.window]:
                    del d[k]

    def _trim(self):   # size cap: drop the least recently active (O(1) each)
        for d in (self.hits, self.noted):
            while len(d) > self.MAX_KEYS:
                d.popitem(last=False)

    def _keys(self, ip, username, device):
        ck = self.client_key(ip)
        if device:
            return ck, ("dev:" + device, self.device_limit)
        return ck, ("user:" + (username or "").lower()[:64], self.user_limit)

    def attempt(self, ip, username, device=None):
        """Reserve a slot for one login attempt; False if this client or account (or known device) is over its limit.
        `device` is the id from a valid known-device cookie for this username, if the browser sent one."""
        now = time.time()
        self._sweep(now)
        ck, (ak, alimit) = self._keys(ip, username, device)
        if self._count(ck, now) >= self.limit or self._count(ak, now) >= alimit:
            return False
        self._add(ck, now)
        self._add(ak, now)
        self._trim()
        return True

    def success(self, ip, username, device=None):
        """Give back the budgets this login used. Signing in from a known device leaves the account's limit for
        other browsers alone, so the owner signing in doesn't hand someone guessing at the account a fresh budget."""
        ck, (ak, _) = self._keys(ip, username, device)
        for k in (ck, ak):
            self.hits.pop(k, None)

    def first_refusal(self, ip, username):
        """True once per window for a client/account pair: log refusals without letting a flood fill the journal."""
        now = time.time()
        k = self.client_key(ip) + "|" + (username or "").lower()[:64]
        if now - self.noted.get(k, 0) < self.window:
            return False
        self.noted.pop(k, None)
        self.noted[k] = now
        self._trim()
        return True
