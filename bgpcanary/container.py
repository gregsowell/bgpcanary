"""Entry point of the container image:  python -m bgpcanary.container   (`healthcheck` for the image's HEALTHCHECK)

What the installer and systemd do for a server install, this does inside the container:
- first start: creates the config file next to the database, with a random secret key, and the initial 'admin'
  account (its password goes to the container log and to <data>/initial-admin-password until it's changed);
- before a new version first runs: backs up the database into <data>/backups (the newest 3 are kept);
- runs the web app (uvicorn, one process). Settings that need a restart restart it in place, so the container
  keeps running whatever its restart policy is.

Environment (all optional; see docs/DOCKER.md):
  BGPCANARY_CONFIG           config file; created if missing (image default: /var/lib/bgpcanary/config.toml)
  BGPCANARY_BASE_URL         the address people use, e.g. https://bgp.example.com (only used to create the config)
  BGPCANARY_HOST, _PORT      where to listen (0.0.0.0, 8080)
  BGPCANARY_TRUSTED_PROXIES  addresses/networks of reverse proxies whose X-Forwarded-For/-Proto are believed
                             (default: the private networks; this machine itself always is)
  BGPCANARY_INSECURE_COOKIES=1  allow signing in over plain HTTP (for a quick test without a proxy)
"""
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import sys
import tempfile
import time

from . import __version__, auth, config, db

log = logging.getLogger("bgpcanary.container")
EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "config.example.toml")
DEFAULT_TRUSTED = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"   # (and always this machine: trusted_proxies)
KEEP_BACKUPS = 3


def _logging():
    """Log lines with UTC timestamps (as the rest of bgpcanary uses UTC). Set up before the web app is imported,
    so its own basicConfig() leaves this in place; uvicorn's loggers use it too (log_config=None)."""
    h = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    fmt.converter = time.gmtime
    h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[h])


def _fail(msg, code=1):
    log.critical("%s", msg)
    sys.exit(code)


def _writable(d):
    try:
        fd, p = tempfile.mkstemp(dir=d, prefix=".write-test-")
        os.close(fd)
        os.unlink(p)
        return True
    except OSError:
        return False


def _not_writable(d):
    _fail(f"{d} isn't writable by this container's user (uid {os.getuid()}, gid {os.getgid()}). With a bind mount, "
          f"give that uid the directory, e.g. `sudo chown -R {os.getuid()}:{os.getgid()} ./data`, or use a named "
          "volume. See docs/DOCKER.md.")


def base_url():
    v = os.environ.get("BGPCANARY_BASE_URL", "").strip().rstrip("/")
    if v and not re.fullmatch(r"https?://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?", v):
        _fail(f"BGPCANARY_BASE_URL={v!r} isn't an address like https://bgp.example.com", 2)
    return v


def make_config(path):
    """Write a new config file from deploy/config.example.toml: random secret key, database beside the file."""
    d = os.path.dirname(path)
    if not _writable(d):
        _not_writable(d)
    with open(EXAMPLE, encoding="utf-8") as f:
        text = f.read()
    subs = [
        ("# After editing: sudo systemctl restart bgpcanary",
         "# After editing: restart the container (docker restart bgpcanary).\n"
         "# Created by the bgpcanary container on first start. In the container, the listen address and port come\n"
         "# from BGPCANARY_HOST and BGPCANARY_PORT (default 0.0.0.0 and 8080), not from [web] below."),
        ('db_path = "/var/lib/bgpcanary/bgpcanary.db"', f"db_path = {json.dumps(os.path.join(d, 'bgpcanary.db'))}"),
        ('listen_host = "127.0.0.1"', 'listen_host = "0.0.0.0"'),
        ('base_url = "https://__FQDN__"', f"base_url = {json.dumps(base_url())}"),
        ('secret_key = "__SECRET__"', f'secret_key = "{secrets.token_hex(32)}"'),
    ]
    for old, new in subs:
        if text.count(old) != 1:   # the example changed: fail here rather than write a half-made config
            _fail(f"can't create the config: {old!r} isn't in {EXAMPLE} exactly once. Please report this as a bug.")
        text = text.replace(old, new)
    tmp = path + ".new"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        config.load(tmp)
    except config.ConfigError as ex:
        os.unlink(tmp)
        _fail(f"the generated config was invalid ({ex}). Please report this as a bug.")
    os.replace(tmp, path)
    log.info("created %s", path)


def backup_before_upgrade(db_path):
    """The first time a version runs on an existing database, copy the database (before it's migrated) into
    <data>/backups: running the previous image with that copy undoes an update. The newest 3 are kept."""
    d = os.path.dirname(db_path)
    marker = os.path.join(d, ".bgpcanary-version")
    try:
        with open(marker, encoding="utf-8") as f:
            if f.read().strip() == __version__:
                return
    except OSError:
        pass
    if os.path.isfile(db_path) and os.path.getsize(db_path) > 0:
        bk = os.path.join(d, "backups")
        out = os.path.join(bk, f"before-{__version__}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.db")
        try:
            os.makedirs(bk, mode=0o700, exist_ok=True)
            src = sqlite3.connect(db_path)
            dst = sqlite3.connect(out + ".part")
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            os.replace(out + ".part", out)
        except Exception as ex:   # no backup: say so, and try again next start (the marker isn't written)
            log.error("couldn't back up the database before starting %s: %s", __version__, ex)
            try:
                os.unlink(out + ".part")
            except OSError:
                pass
            return
        log.info("backed up the database to %s", out)
        old = sorted((os.path.join(bk, n) for n in os.listdir(bk) if n.startswith("before-") and n.endswith(".db")),
                     key=os.path.getmtime, reverse=True)
        for p in old[KEEP_BACKUPS:]:
            os.unlink(p)
    tmp = marker + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(__version__ + "\n")
    os.replace(tmp, marker)


def initial_admin(db_path):
    """Create 'admin' when there are no users yet; tidy up its password file once that password is changed."""
    pw_file = os.path.join(os.path.dirname(db_path), "initial-admin-password")
    db.init(db_path)
    conn = db.connect(db_path)
    try:
        pw = auth.ensure_default_admin(conn)
        if pw:
            fd = os.open(pw_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"bgpcanary initial admin login (created {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}). "
                        "You must change the password at first login.\n"
                        f"username: admin\npassword: {pw}\n\n"
                        "This file is only a record of the generated password: editing it does NOT change the password.\n"
                        "To set a new admin password: docker exec -it bgpcanary bgpcanary-admin useradd admin\n")
            bar = "=" * 72
            print(f"\n{bar}\n bgpcanary: initial admin login\n   username: admin\n   password: {pw}\n"
                  " You'll choose a new password when you first sign in.\n"
                  f" (Until then it's also in {pw_file}.)\n{bar}\n", file=sys.stderr, flush=True)
        elif os.path.exists(pw_file):
            users = auth.list_users(conn)
            if users and not any(u["must_change"] for u in users):
                os.unlink(pw_file)   # it no longer works: don't leave it lying around
                log.info("removed %s (its temporary password has been changed)", pw_file)
            else:
                log.info("the initial admin password is in %s until it's changed", pw_file)
    finally:
        conn.close()


def own_addresses():
    """This container's own IP addresses (not loopback). With rootless Podman's default network (pasta), a
    connection from the host, e.g. from a reverse proxy there, arrives from the host's address, which the
    container shares; so a connection from one of these comes from this machine, like one from loopback."""
    out = set()
    try:
        with open("/proc/net/fib_trie", encoding="ascii") as f:
            lines = f.read().splitlines()
        out |= {lines[i - 1].split()[-1] for i, x in enumerate(lines) if x.strip() == "/32 host LOCAL" and i}
    except OSError:
        pass
    try:
        with open("/proc/net/if_inet6", encoding="ascii") as f:
            out |= {str(ipaddress.IPv6Address(int(x.split()[0], 16))) for x in f if x.strip()}
    except (OSError, ValueError):
        pass
    good = []
    for a in out:
        try:
            if not ipaddress.ip_address(a).is_loopback:
                good.append(a)
        except ValueError:
            pass
    return sorted(good)


def trusted_proxies():
    """What uvicorn's --forwarded-allow-ips gets: this machine itself (loopback and this container's own
    addresses), plus BGPCANARY_TRUSTED_PROXIES (default: the private networks; 'none': nothing more)."""
    raw = os.environ.get("BGPCANARY_TRUSTED_PROXIES", DEFAULT_TRUSTED).strip()
    out = ["127.0.0.0/8", "::1/128"]
    if raw.lower() not in ("", "none"):
        for item in (x.strip() for x in raw.split(",")):
            if not item:
                continue
            try:
                net = str(ipaddress.ip_network(item, strict=False))
            except ValueError:
                _fail(f"BGPCANARY_TRUSTED_PROXIES: {item!r} isn't an IP address or network (e.g. 172.18.0.0/16)", 2)
            if net not in out:
                out.append(net)
    for a in own_addresses():
        if not any(ipaddress.ip_address(a) in ipaddress.ip_network(n) for n in out):
            out.append(str(ipaddress.ip_network(a)))
    return ",".join(out)


def listen():
    host = os.environ.get("BGPCANARY_HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(os.environ.get("BGPCANARY_PORT", "8080"))
        if not 0 < port < 65536:
            raise ValueError
    except ValueError:
        _fail(f"BGPCANARY_PORT={os.environ.get('BGPCANARY_PORT')!r} isn't a port number", 2)
    return host, port


def main():
    os.umask(0o077)   # the database holds password hashes and keys: private even in a host directory
    _logging()
    path = config.DEFAULT_PATH
    if not os.path.exists(path):
        make_config(path)
    try:
        cfg = config.load(path)
    except config.ConfigError as ex:
        _fail(str(ex), 2)
    data = os.path.dirname(os.path.abspath(cfg.db_path))
    if not os.path.isdir(data) or not _writable(data):
        _not_writable(data)
    backup_before_upgrade(cfg.db_path)
    initial_admin(cfg.db_path)
    host, port = listen()
    trusted = trusted_proxies()

    import uvicorn
    stops = []   # SIGTERM/SIGINT received (uvicorn passes them on after shutting down)

    def note(sig, frame):
        stops.append(sig)

    signal.signal(signal.SIGTERM, note)
    signal.signal(signal.SIGINT, note)
    log.info("bgpcanary %s listening on %s:%s (trusting proxy headers from %s)", __version__, host, port, trusted)
    server = uvicorn.Server(uvicorn.Config(
        "bgpcanary.web:app", host=host, port=port, workers=1, lifespan="on", proxy_headers=True,
        forwarded_allow_ips=trusted, access_log=False, log_config=None, server_header=False,
        timeout_graceful_shutdown=15))
    server.run()
    web = sys.modules.get("bgpcanary.web")
    if getattr(web, "restart_requested", False) and len(stops) <= 1:   # its own restart, and no stop on top
        log.info("restarting with the new settings")
        os.execv(sys.executable, [sys.executable, "-m", "bgpcanary.container"])
    sys.exit(0 if server.started else 1)


def healthcheck():
    """Exit 0 if the web app answers /healthz on this container's own listener (proxies are ignored)."""
    import urllib.request
    host, port = listen()
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/healthz"
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=8) as r:
            ok = r.status == 200 and json.load(r).get("ok") is True
    except Exception as ex:
        print(f"unhealthy: {ex}")
        sys.exit(1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if sys.argv[1:] == ["healthcheck"]:
        healthcheck()
    elif sys.argv[1:]:
        sys.exit("usage: python -m bgpcanary.container [healthcheck]")
    else:
        main()
