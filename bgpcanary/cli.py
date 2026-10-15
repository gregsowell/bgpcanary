"""bgpcanary admin CLI: python -m bgpcanary.cli <command>"""
import argparse
import asyncio
import getpass
import os
import shutil
import sqlite3
import sys
import tempfile
import time

from . import alerts, auth, config, db


def _password(prompt):
    """Prompt without echo at a terminal; read a plain line when the password is piped in (scripts)."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    line = sys.stdin.readline()
    if not line:
        sys.exit("no password given on standard input")
    return line.rstrip("\r\n")


def _copy_db(conn, path):
    dst = sqlite3.connect(path)
    try:
        conn.backup(dst)   # SQLite's online backup: consistent even while the service writes
    finally:
        dst.close()


def backup(conn, db_path, out):
    """Copy the database to a new file that only its owner can read (it holds password hashes and API keys), or
    with '-' to standard output, e.g.  docker exec bgpcanary bgpcanary-admin backup - > bgpcanary.db"""
    if out == "-":
        if sys.stdout.isatty():
            sys.exit("redirect the output to a file, e.g.  bgpcanary-admin backup - > bgpcanary.db")
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(db_path)), prefix=".backup-")
        os.close(fd)
        try:
            _copy_db(conn, tmp)
            with open(tmp, "rb") as f:
                shutil.copyfileobj(f, sys.stdout.buffer)
            sys.stdout.buffer.flush()
        finally:
            os.unlink(tmp)
        return
    try:
        os.close(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        sys.exit(f"{out} already exists")
    except OSError as ex:
        sys.exit(f"can't create {out}: {ex.strerror}")
    _copy_db(conn, out)
    print(f"database copied to {out} ({os.path.getsize(out) // 1024} KB)")


def main():
    ap = argparse.ArgumentParser(prog="bgpcanary-admin")
    ap.add_argument("--config", default=config.DEFAULT_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("useradd", help="create a web user or reset its password")
    u.add_argument("username")
    u.add_argument("--role", choices=auth.ROLES, help="admin (full access) or viewer (read-only); "
                   "default: admin for new users, unchanged for existing ones")
    u.add_argument("--must-change", action="store_true", help="require a new password at next login")
    d = sub.add_parser("userdel", help="delete a web user")
    d.add_argument("username")
    sub.add_parser("users", help="list web users")
    sub.add_parser("test-alert", help="send a test alert on all channels")
    sub.add_parser("version", help="print the installed bgpcanary version")
    sub.add_parser("ensure-admin", help="create the default 'admin' account if no users exist (prints its password)")
    b = sub.add_parser("backup", help="write a consistent copy of the database to FILE, or - for standard output "
                       "(safe while bgpcanary runs)")
    b.add_argument("file")
    args = ap.parse_args()

    try:
        cfg = config.load(args.config)
    except config.ConfigError as ex:
        sys.exit(str(ex))
    db.init(cfg.db_path)
    conn = db.connect(cfg.db_path)
    config.apply_alert_settings(cfg, db.kv_get(conn, config.ALERT_KV))
    config.apply_monitor_settings(cfg, db.kv_get(conn, config.MONITOR_KV))
    config.apply_tuning(cfg, db.kv_get(conn, config.TUNING_KV))
    if args.cmd == "version":
        from . import __version__
        print(__version__)
        return
    if args.cmd == "useradd":
        pw = _password("Password: ")
        if len(pw) < 10:
            sys.exit("password must be at least 10 characters")
        if pw != _password("Confirm: "):
            sys.exit("passwords do not match")
        if not auth.USERNAME_RE.match(args.username):
            sys.exit("usernames are 2-64 characters: letters, digits, . _ - @")
        auth.set_user(conn, args.username, pw, role=args.role, must_change=1 if args.must_change else 0)
        r = auth.get_user(conn, args.username)
        print(f"user {args.username} saved (role: {r['role']})")
    elif args.cmd == "userdel":
        auth.delete_user(conn, args.username)   # (and its sessions)
        conn.commit()
        print("deleted")
    elif args.cmd == "users":
        for r in auth.list_users(conn):
            print(f"{r['username']:24} {r['role']:7} {time.strftime('%Y-%m-%d', time.localtime(r['created'] or 0))}"
                  + ("  (must change password)" if r["must_change"] else ""))
    elif args.cmd == "ensure-admin":
        pw = auth.ensure_default_admin(conn)
        if pw:
            print("username: admin")
            print(f"password: {pw}")
    elif args.cmd == "backup":
        backup(conn, cfg.db_path, args.file)
    elif args.cmd == "test-alert":
        ev = {"id": None, "kind": "test", "severity": "info", "prefix": ", ".join(cfg.prefixes),
              "start_ts": time.time(), "end_ts": None, "title": "Test alert from bgpcanary CLI"}
        results = asyncio.run(alerts.send(cfg, conn, ev, "test"))
        if not results:
            sys.exit("No alert channels are enabled. Set up email or a webhook under Settings › Alerts, then try again.")
        for ch, ok, err in results:
            print(f"{ch}: {'OK' if ok else 'FAILED ' + (err or '')}")
        if not all(ok for _ch, ok, _err in results):
            sys.exit(1)


if __name__ == "__main__":
    main()
