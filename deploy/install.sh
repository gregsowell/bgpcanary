#!/usr/bin/env bash
# bgpcanary installer for Rocky Linux / AlmaLinux / RHEL 9 and 10. Run as root from the source directory:
#
#   sudo ./deploy/install.sh [--fqdn bgp.example.com] [--asn 64500] [--prefix 192.0.2.0/24 ...]
#                            [--upstream 64501 ...] [--probe 192.0.2.1 ...] [--atlas-probe]
#
#   --fqdn NAME         host name for the web UI and its certificate (default: this server's name)
#   --asn / --prefix / --upstream / --probe
#                       pre-fill the monitoring settings (otherwise you enter them in the web UI)
#   --atlas-probe       also install the RIPE Atlas software probe (earns credits for the optional Atlas checks)
#   --atlas-probe-only  only install/upgrade the probe, leave bgpcanary as it is
#
# Safe to re-run: it upgrades the code and keeps the configuration, database, TLS certificate and users
# (a database backup is taken first). `sudo bgpcanary-update` does all of this for a new release.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/bgpcanary
DATA=/var/lib/bgpcanary
ETC=/etc/bgpcanary
FQDN="${BGPCANARY_FQDN:-}"; ASN=0; PREFIXES=(); UPSTREAMS=(); PROBES=(); ATLAS_PROBE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --fqdn) FQDN="$2"; shift 2 ;;
    --asn) ASN="${2#AS}"; ASN="${ASN#as}"; shift 2 ;;
    --prefix) for v in ${2//,/ }; do PREFIXES+=("$v"); done; shift 2 ;;
    --upstream) for v in ${2//,/ }; do v="${v#AS}"; UPSTREAMS+=("${v#as}"); done; shift 2 ;;
    --probe) for v in ${2//,/ }; do PROBES+=("$v"); done; shift 2 ;;
    --atlas-probe) ATLAS_PROBE=1; shift ;;
    --atlas-probe-only) echo "==> RIPE Atlas software probe"; exec bash "$(dirname "$0")/atlas-probe.sh" ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)"; exit 1 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo $0 $*"; exit 1; }
. /etc/os-release
EL="${PLATFORM_ID#platform:}"
case "$EL" in
  el9|el10) ;;
  *) echo "bgpcanary supports Rocky Linux, AlmaLinux and RHEL 9 or 10 (this is ${PRETTY_NAME:-unknown})."; exit 1 ;;
esac
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/bgpcanary/__init__.py")
for a in "$ASN" "${UPSTREAMS[@]}"; do
  [[ "$a" =~ ^[0-9]+$ ]] || { echo "Not an AS number: '$a' (use digits, e.g. --asn 64500 --upstream 64501)"; exit 1; }
done
echo "bgpcanary $VERSION installer on $PRETTY_NAME"

# ---------------------------------------------------------------- upgrade from the pre-1.0 name (bgpmon)
if [ -d /opt/bgpmon ] && [ ! -d "$APP" ]; then
  echo "==> migrating the old 'bgpmon' installation to 'bgpcanary' (data, settings and users are kept)"
  systemctl disable -q --now bgpmon 2>/dev/null || true
  mv /var/lib/bgpmon "$DATA"
  for x in "" -wal -shm; do   # the write-ahead log belongs to the database: rename it with it, never delete it
    [ -f "$DATA/bgpmon.db$x" ] && mv "$DATA/bgpmon.db$x" "$DATA/bgpcanary.db$x"
  done
  mv /etc/bgpmon "$ETC"
  sed -i 's|^db_path = "/var/lib/bgpmon/bgpmon.db"|db_path = "/var/lib/bgpcanary/bgpcanary.db"|' "$ETC/config.toml"
  [ -f /etc/pki/tls/certs/bgpmon.crt ] && mv /etc/pki/tls/certs/bgpmon.crt /etc/pki/tls/certs/bgpcanary.crt
  [ -f /etc/pki/tls/private/bgpmon.key ] && mv /etc/pki/tls/private/bgpmon.key /etc/pki/tls/private/bgpcanary.key
  [ -z "$FQDN" ] && [ -f /etc/nginx/conf.d/bgpmon.conf ] && \
    FQDN=$(awk '$1 == "server_name" {gsub(";", "", $2); print $2; exit}' /etc/nginx/conf.d/bgpmon.conf)
  rm -f /etc/nginx/conf.d/bgpmon.conf /etc/systemd/system/bgpmon.service /usr/bin/bgpmon-admin
  id bgpcanary &>/dev/null || useradd --system --home-dir "$DATA" --shell /sbin/nologin bgpcanary
  chown -R bgpcanary:bgpcanary "$DATA"
  chown -R root:bgpcanary "$ETC"
  rm -rf /opt/bgpmon
  userdel bgpmon 2>/dev/null || true
  systemctl daemon-reload
fi

if [ -z "$FQDN" ] && [ -f /etc/nginx/conf.d/bgpcanary.conf ]; then   # upgrade: keep the existing name
  FQDN=$(awk '$1 == "server_name" {gsub(";", "", $2); print $2; exit}' /etc/nginx/conf.d/bgpcanary.conf)
fi
[ -n "$FQDN" ] || FQDN=$(hostname -f 2>/dev/null || cat /proc/sys/kernel/hostname)   # (minimal systems lack `hostname`)
toml_list() { local out="" x; for x in "$@"; do out+="${out:+, }$x"; done; echo "[$out]"; }
toml_strs() { local out="" x; for x in "$@"; do out+="${out:+, }\"$x\""; done; echo "[$out]"; }
CHARTJS_URL="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"

echo "==> packages"
# Python 3.11+ is required: EL10's python3 is 3.12; on EL9 (python3 = 3.9) the python3.12 package is used
PY=python3
[ "$EL" = el9 ] && PY=python3.12
PKGS=("$PY" nginx policycoreutils-python-utils openssl sqlite dejavu-sans-fonts tar)
command -v curl >/dev/null || PKGS+=(curl)   # EL9 minimal ships curl-minimal, which conflicts with the curl package
dnf -y -q install "${PKGS[@]}" >/dev/null

# Check prefixes / probe addresses, and an existing config file, before changing anything (stdlib only)
"$PY" - "${PREFIXES[@]}" -- "${PROBES[@]}" <<'PYCHECK' || exit 1
import ipaddress, sys
args = sys.argv[1:]
cut = args.index("--")
for p in args[:cut]:
    try:
        ipaddress.ip_network(p, strict=False)
    except ValueError:
        sys.exit(f"Not a valid prefix: '{p}' (e.g. --prefix 192.0.2.0/24 --prefix 2001:db8::/48)")
for a in args[cut + 1:]:
    try:
        ipaddress.ip_address(a)
    except ValueError:
        sys.exit(f"Not a valid IP address for --probe: '{a}' (e.g. --probe 192.0.2.1)")
PYCHECK
check_config() {   # prints a readable error and fails if the config file isn't valid
  "$PY" -c 'import sys; sys.path.insert(0, sys.argv[2]); from bgpcanary import config
try:
    config.load(sys.argv[1])
except config.ConfigError as ex:
    sys.exit(str(ex))' "$1" "$SRC"
}
if [ -f "$ETC/config.toml" ] && ! check_config "$ETC/config.toml"; then
  echo
  echo "Nothing was changed. Fix $ETC/config.toml (see above), then run this again."
  exit 1
fi

echo "==> user and directories"
id bgpcanary &>/dev/null || useradd --system --home-dir "$DATA" --shell /sbin/nologin bgpcanary
install -d -o bgpcanary -g bgpcanary -m 750 "$DATA" "$DATA/backups"
install -d -o root -g bgpcanary -m 750 "$ETC"
install -d -m 755 "$APP"

if [ -s "$DATA/bgpcanary.db" ]; then
  echo "==> backing up the database before upgrading"
  B="$DATA/backups/before-$VERSION-$(date +%Y%m%d-%H%M%S).db"
  sqlite3 "$DATA/bgpcanary.db" ".backup '$B'" && chown bgpcanary:bgpcanary "$B" && echo "    $B"
  ls -1t "$DATA"/backups/before-*.db 2>/dev/null | tail -n +4 | xargs -r rm -f   # keep the newest 3
fi

echo "==> code"
rm -rf "$APP/bgpcanary.new"
cp -r "$SRC/bgpcanary" "$APP/bgpcanary.new"
find "$APP/bgpcanary.new" -name '__pycache__' -prune -exec rm -rf {} +
if [ -d "$APP/bgpcanary/static/vendor" ]; then cp -r "$APP/bgpcanary/static/vendor" "$APP/bgpcanary.new/static/"; fi
rm -rf "$APP/bgpcanary.old"; [ -d "$APP/bgpcanary" ] && mv "$APP/bgpcanary" "$APP/bgpcanary.old"
mv "$APP/bgpcanary.new" "$APP/bgpcanary"
install -m 644 "$SRC/requirements.txt" "$APP/requirements.txt"
install -d -m 755 "$APP/deploy"   # kept so the update / uninstall / probe commands work later
if [ "$SRC" != "$APP" ]; then
  for f in install.sh update.sh uninstall.sh atlas-probe.sh; do install -m 755 "$SRC/deploy/$f" "$APP/deploy/"; done
  for f in config.example.toml nginx-bgpcanary.conf bgpcanary.service; do install -m 644 "$SRC/deploy/$f" "$APP/deploy/"; done
fi

mkdir -p "$APP/bgpcanary/static/vendor"
if [ ! -s "$APP/bgpcanary/static/vendor/chart.umd.min.js" ]; then
  curl -fsSL "$CHARTJS_URL" -o "$APP/bgpcanary/static/vendor/chart.umd.min.js"
fi
for f in gif.js gif.worker.js; do   # GIF encoder for the path-tree replay export (worker must be same-origin)
  [ -s "$APP/bgpcanary/static/vendor/$f" ] || curl -fsSL "https://cdnjs.cloudflare.com/ajax/libs/gif.js/0.2.0/$f" -o "$APP/bgpcanary/static/vendor/$f"
done

echo "==> python environment"
# (re)create the venv if missing or built with an older Python than the one required
if [ ! -x "$APP/venv/bin/python" ] || ! "$APP/venv/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  rm -rf "$APP/venv"
  "$PY" -m venv "$APP/venv"
fi
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "==> config"
if [ ! -f "$ETC/config.toml" ]; then
  SECRET=$(openssl rand -hex 32)
  sed -e "s/__SECRET__/$SECRET/" -e "s/__FQDN__/$FQDN/" \
      -e "s|^asn = 0|asn = $ASN|" \
      -e "s|^prefixes = \[\]|prefixes = $(toml_strs "${PREFIXES[@]}")|" \
      -e "s|^expected_upstreams = \[\]|expected_upstreams = $(toml_list "${UPSTREAMS[@]}")|" \
      -e "s|^probe_targets = \[\]|probe_targets = $(toml_strs "${PROBES[@]}")|" \
      "$SRC/deploy/config.example.toml" > "$ETC/config.toml"
  [ "$ASN" != 0 ] && sed -i "s|^site_name = \"BGP Canary\"|site_name = \"BGP Canary · AS$ASN\"|" "$ETC/config.toml"
  if ! check_config "$ETC/config.toml"; then
    mv "$ETC/config.toml" "$ETC/config.toml.invalid"
    echo "The generated config was invalid (saved as $ETC/config.toml.invalid). Please report this as a bug."
    exit 1
  fi
  echo "    created $ETC/config.toml"
fi
chown root:bgpcanary "$ETC/config.toml"
chmod 640 "$ETC/config.toml"

cat > /usr/bin/bgpcanary-admin <<'EOF'
#!/bin/sh
# runs the bgpcanary command line as the service user (runuser is part of every EL system; sudo may not be)
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo bgpcanary-admin $*"; exit 1; }
cd /opt/bgpcanary && exec runuser -u bgpcanary -- env BGPCANARY_CONFIG=/etc/bgpcanary/config.toml /opt/bgpcanary/venv/bin/python -m bgpcanary.cli "$@"
EOF
chmod 755 /usr/bin/bgpcanary-admin
ln -sf "$APP/deploy/update.sh" /usr/bin/bgpcanary-update
ln -sf "$APP/deploy/uninstall.sh" /usr/sbin/bgpcanary-uninstall

echo "==> TLS (self-signed unless a certificate already exists)"
if [ ! -f /etc/pki/tls/certs/bgpcanary.crt ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout /etc/pki/tls/private/bgpcanary.key -out /etc/pki/tls/certs/bgpcanary.crt \
    -subj "/CN=$FQDN" -addext "subjectAltName=DNS:$FQDN" 2>/dev/null
  chmod 600 /etc/pki/tls/private/bgpcanary.key
fi

echo "==> nginx"
# HTTP/2 syntax: "http2 on;" from nginx 1.25.1 (EL10); "listen ... http2" before that (EL9)
NGV=$(nginx -v 2>&1 | sed -n 's|.*nginx/\([0-9.]*\).*|\1|p')
if [ "$(printf '%s\n1.25.1\n' "$NGV" | sort -V | head -1)" = 1.25.1 ]; then H2_LISTEN=""; H2_ON="http2 on;"; else H2_LISTEN=" http2"; H2_ON=""; fi
sed -e "s/__FQDN__/$FQDN/g" -e "s/__H2_LISTEN__/$H2_LISTEN/g" -e "s/__H2_ON__/$H2_ON/g" \
    "$SRC/deploy/nginx-bgpcanary.conf" > /etc/nginx/conf.d/bgpcanary.conf
chmod 644 /etc/nginx/conf.d/bgpcanary.conf
setsebool -P httpd_can_network_connect 1 2>/dev/null || true
nginx -t -q
systemctl enable -q --now nginx
systemctl reload nginx

echo "==> firewall"
if systemctl is-active -q firewalld; then
  firewall-cmd -q --permanent --add-service=http --add-service=https
  firewall-cmd -q --reload
fi

echo "==> service"
install -m 644 "$SRC/deploy/bgpcanary.service" /etc/systemd/system/bgpcanary.service
systemctl daemon-reload
systemctl enable -q bgpcanary
systemctl restart bgpcanary
sleep 2
systemctl --no-pager --lines=0 status bgpcanary | head -3

echo "==> default admin account"
CREDS=$(/usr/bin/bgpcanary-admin ensure-admin)
if [ -n "$CREDS" ]; then
  umask 077
  { echo "bgpcanary initial admin login (created $(date -Is)). You must change the password at first login."
    echo "$CREDS"
    echo
    echo "This file is only a record of the generated password: editing it does NOT change the password."
    echo "To set a new admin password: sudo bgpcanary-admin useradd admin"; } > "$ETC/initial-admin-password"
  chmod 600 "$ETC/initial-admin-password"
  echo
  echo "  Web login created:"
  echo "$CREDS" | sed 's/^/    /'
  echo "  (also saved to $ETC/initial-admin-password; you must change the password at first login)"
else
  echo "    users already exist; no default account created"
fi
if [ "$ATLAS_PROBE" = 1 ]; then
  echo "==> RIPE Atlas software probe"
  bash "$SRC/deploy/atlas-probe.sh" || echo "    probe install failed; bgpcanary itself is fine. Retry: sudo $APP/deploy/install.sh --atlas-probe-only"
fi
echo
echo "Done: bgpcanary $VERSION. Open https://$FQDN/"
echo "(The certificate is self-signed; see docs/INSTALL.md for using a real one.)"
if ! grep -q '^asn = [1-9]' "$ETC/config.toml"; then
  echo "Sign in as admin and you'll be taken to setup to enter your ASN and prefixes."
fi
