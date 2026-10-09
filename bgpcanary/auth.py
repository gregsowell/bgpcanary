"""Local user accounts (scrypt hashes) + simple login throttling."""
import hashlib
import hmac
import re
import secrets
import time

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
    """Create a user or reset its password. `role` None keeps an existing user's role (new users: admin)."""
    if role is not None and role not in ROLES:
        raise ValueError("bad role")
    conn.execute("INSERT INTO users(username,pw_hash,created,role,must_change) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash, must_change=excluded.must_change"
                 + (", role=excluded.role" if role else ""),
                 (username, hash_password(pw), time.time(), role or "admin", must_change))
    conn.commit()


def get_user(conn, username):
    return conn.execute("SELECT username, role, must_change, created FROM users WHERE username=?",
                        (username,)).fetchone()


def list_users(conn):
    return conn.execute("SELECT username, role, must_change, created FROM users ORDER BY username").fetchall()


def admin_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()["c"]


def set_role(conn, username, role):
    if role not in ROLES:
        raise ValueError("bad role")
    conn.execute("UPDATE users SET role=? WHERE username=?", (role, username))
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
    """Max `limit` failures per `window` seconds per client IP."""

    def __init__(self, limit=5, window=900):
        self.limit, self.window = limit, window
        self.fails: dict[str, list] = {}

    def blocked(self, ip):
        now = time.time()
        f = [t for t in self.fails.get(ip, []) if t > now - self.window]
        self.fails[ip] = f
        return len(f) >= self.limit

    def fail(self, ip):
        self.fails.setdefault(ip, []).append(time.time())

    def reset(self, ip):
        self.fails.pop(ip, None)
