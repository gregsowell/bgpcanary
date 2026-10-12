"""bgpcanary admin CLI: python -m bgpcanary.cli <command>"""
import argparse
import asyncio
import getpass
import sys
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
        conn.execute("DELETE FROM users WHERE username=?", (args.username,))
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
