#!/usr/bin/env bash
# bgpcanary installer for Rocky Linux / AlmaLinux / RHEL 9 and 10, and Alpine Linux 3.22+. Run as root from the source directory:
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
. /etc/os-release
EL="${PLATFORM_ID:-}"; EL="${EL#platform:}"   # el9 / el10 on Enterprise Linux (Alpine has no PLATFORM_ID)
case "${ID:-}/$EL" in
  */el9|*/el10) OS=el ;;
  alpine/*) OS=alpine ;;
  *) OS="" ;;
esac
if [ "$OS" = alpine ]; then   # 3.22 is the oldest release Alpine still supports; all of them ship Python 3.12+
  V="${VERSION_ID:-0}"; MINOR="${V#3.}"; MINOR="${MINOR%%[!0-9]*}"
  { [ "$V" != "${V#3.}" ] && [ "${MINOR:-0}" -ge 22 ]; } || OS=""
fi
[ -n "$OS" ] || { echo "bgpcanary supports Rocky Linux, AlmaLinux and RHEL 9 or 10, and Alpine Linux 3.22 or newer (this is ${PRETTY_NAME:-unknown})."; exit 1; }
SUDO=sudo; if [ "$OS" = alpine ] && ! command -v sudo >/dev/null; then SUDO=doas; fi   # (Alpine's default)
[ "$(id -u)" = 0 ] || { echo "Run as root: $SUDO $0 $*"; exit 1; }

# What differs between the two: where nginx reads its sites, its web root (Let's Encrypt challenges), where the
# certificate goes, and the init system (systemd, or OpenRC's supervise-daemon on Alpine).
if [ "$OS" = el ]; then
  NGINX_D=/etc/nginx/conf.d; WEBROOT=/usr/share/nginx/html
  CERT=/etc/pki/tls/certs/bgpcanary.crt; KEY=/etc/pki/tls/private/bgpcanary.key
else
  NGINX_D=/etc/nginx/http.d; WEBROOT=/var/lib/nginx/html
  CERT=/etc/ssl/certs/bgpcanary.crt; KEY=/etc/ssl/private/bgpcanary.key
fi
svc() {   # svc start|stop|restart|reload NAME
  if [ "$OS" = el ]; then systemctl "$1" "$2"; else rc-service "$2" "$1"; fi
}
svc_enable() {   # start NAME at boot
  if [ "$OS" = el ]; then systemctl enable -q "$1"; else rc-update -q add "$1" default; fi
}

# Backups live in a directory only root can write. The service account owns $DATA, so anything root did in
# there (writing, chown, pruning) could be pointed at other files by a compromised service. Here the database
# is read as the service user (so it can only ever yield what the service could read anyway) and root only
# writes into $BK.
BK=/var/backups/bgpcanary
backup_db() {   # backup_db NAME.db : a consistent copy of the database into $BK/NAME.db
  local tmp out="$BK/$1"
  install -d -o root -g root -m 700 "$BK"
  tmp=$(runuser -u bgpcanary -- mktemp -p "$DATA" .backup.XXXXXX) || return 1
  if runuser -u bgpcanary -- sqlite3 -init /dev/null "$DATA/bgpcanary.db" ".backup '$tmp'" >/dev/null &&
     (umask 077; runuser -u bgpcanary -- cat "$tmp" > "$out.part") && mv -f "$out.part" "$out"; then
    runuser -u bgpcanary -- rm -f "$tmp"; echo "    $out"
  else
    runuser -u bgpcanary -- rm -f "$tmp"; rm -f "$out.part"; return 1
  fi
}
keep_newest() {   # keep_newest N PATTERN : delete all but the newest N files in $BK matching PATTERN
  find "$BK" -maxdepth 1 -type f -name "$2" -printf '%T@ %p\n' 2>/dev/null | sort -rn | tail -n +"$(($1 + 1))" \
    | cut -d' ' -f2- | while IFS= read -r f; do rm -f -- "$f"; done
}
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/bgpcanary/__init__.py")
for a in "$ASN" "${UPSTREAMS[@]}"; do
  [[ "$a" =~ ^[0-9]+$ ]] || { echo "Not an AS number: '$a' (use digits, e.g. --asn 64500 --upstream 64501)"; exit 1; }
done
echo "bgpcanary $VERSION installer on $PRETTY_NAME"

# ---------------------------------------------------------------- upgrade from the pre-1.0 name (bgpmon)
if [ "$OS" = el ] && [ -d /opt/bgpmon ] && [ ! -d "$APP" ]; then   # (bgpmon only ever ran on EL)
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

if [ -z "$FQDN" ] && [ -f "$NGINX_D/bgpcanary.conf" ]; then   # upgrade: keep the existing name
  FQDN=$(awk '$1 == "server_name" {gsub(";", "", $2); print $2; exit}' "$NGINX_D/bgpcanary.conf")
fi
[ -n "$FQDN" ] || FQDN=$(hostname -f 2>/dev/null || cat /proc/sys/kernel/hostname)   # (minimal systems lack `hostname`)
toml_list() { local out="" x; for x in "$@"; do out+="${out:+, }$x"; done; echo "[$out]"; }
toml_strs() { local out="" x; for x in "$@"; do out+="${out:+, }\"$x\""; done; echo "[$out]"; }
CHARTJS_URL="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"

echo "==> packages"
PY=python3
if [ "$OS" = el ]; then
  # Python 3.11+ is required: EL10's python3 is 3.12; on EL9 (python3 = 3.9) the python3.12 package is used
  [ "$EL" = el9 ] && PY=python3.12
  PKGS=("$PY" nginx policycoreutils-python-utils openssl sqlite dejavu-sans-fonts tar)
  command -v curl >/dev/null || PKGS+=(curl)   # EL9 minimal ships curl-minimal, which conflicts with the curl package
  dnf -y -q install "${PKGS[@]}" >/dev/null
else
  # (Alpine 3.22+'s python3 is 3.12 or newer.) coreutils, findutils and runuser: the backup code above and some GNU
  # options used below (install -o, sort -V, find -printf, sha256sum -c) aren't in busybox. logrotate: for the log.
  apk add -q --no-progress --no-cache python3 nginx openssl sqlite font-dejavu tar curl ca-certificates \
      coreutils findutils runuser logrotate
fi

# Check prefixes / probe addresses, and an existing config file, before changing anything (stdlib only)
"$PY" - "${PREFIXES[@]}" -- "${PROBES[@]}" <<'PYCHECK' || exit 1
import ipaddress, sys
args = sys.argv[1:]
cut = args.index("--")
for p in args[:cut]:
    try:
        if getattr(ipaddress.ip_network(p, strict=False).network_address, "scope_id", None):
            raise ValueError(p)
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
if [ "$OS" = el ]; then
  id bgpcanary &>/dev/null || useradd --system --home-dir "$DATA" --shell /sbin/nologin bgpcanary
else   # (busybox adduser)
  getent group bgpcanary >/dev/null || addgroup -S bgpcanary
  id bgpcanary &>/dev/null || adduser -S -D -H -h "$DATA" -s /sbin/nologin -G bgpcanary bgpcanary
fi
install -d -o bgpcanary -g bgpcanary -m 750 "$DATA"
install -d -o root -g bgpcanary -m 750 "$ETC"
install -d -o root -g root -m 700 "$BK"
install -d -m 755 "$APP"
if [ -d "$DATA/backups" ] && [ ! -L "$DATA/backups" ]; then
  # before 1.2.2 backups were kept in $DATA/backups: move them to $BK (read as the service user, as above)
  echo "==> moving backups to $BK"
  # NUL-separated "mtime name" pairs; only the names bgpcanary itself used are taken, and each copy keeps its
  # original time, so pruning and --rollback still pick backups by age
  (cd / && runuser -u bgpcanary -- find "$DATA/backups" -maxdepth 1 -type f \
      \( -name 'before-*.db' -o -name 'rolled-back-*.db' -o -name 'config-*.tar.gz' \) -printf '%T@ %f\0') |
    while IFS= read -r -d '' line; do
      t="${line%% *}"; f="${line#* }"; t="${t%.*}"
      [[ "$t" =~ ^[0-9]+$ && "$f" =~ ^(before|rolled-back|config)-[A-Za-z0-9._-]+\.(db|tar\.gz)$ ]] || continue
      if (umask 077; runuser -u bgpcanary -- cat "$DATA/backups/$f" > "$BK/$f"); then
        touch -d "@$t" "$BK/$f"
      else
        rm -f "$BK/$f"
      fi
    done
  runuser -u bgpcanary -- rm -rf "$DATA/backups"
fi

if [ -s "$DATA/bgpcanary.db" ]; then
  echo "==> backing up the database before upgrading"
  backup_db "before-$VERSION-$(date +%Y%m%d-%H%M%S).db" || echo "    (backup failed; continuing)"
  keep_newest 3 'before-*.db'
fi

echo "==> code"
rm -rf "$APP/bgpcanary.new"
cp -r "$SRC/bgpcanary" "$APP/bgpcanary.new"
find "$APP/bgpcanary.new" -name '__pycache__' -prune -exec rm -rf {} +
if [ -d "$APP/bgpcanary/static/vendor" ]; then cp -r "$APP/bgpcanary/static/vendor" "$APP/bgpcanary.new/static/"; fi
rm -rf "$APP/bgpcanary.old"; [ -d "$APP/bgpcanary" ] && mv "$APP/bgpcanary" "$APP/bgpcanary.old"
mv "$APP/bgpcanary.new" "$APP/bgpcanary"
[ "$SRC" = "$APP" ] || install -m 644 "$SRC/requirements.txt" "$APP/requirements.txt"   # (re-running the installed copy: same file)
install -d -m 755 "$APP/deploy"   # kept so the update / uninstall / probe commands work later
if [ "$SRC" != "$APP" ]; then
  for f in install.sh update.sh uninstall.sh atlas-probe.sh; do install -m 755 "$SRC/deploy/$f" "$APP/deploy/"; done
  for f in config.example.toml nginx-bgpcanary.conf bgpcanary.service bgpcanary.initd bgpcanary.logrotate; do install -m 644 "$SRC/deploy/$f" "$APP/deploy/"; done
fi

mkdir -p "$APP/bgpcanary/static/vendor"
vendor() {   # vendor FILE URL SHA256 : download a front-end library once and check it's exactly the expected file
  local dst="$APP/bgpcanary/static/vendor/$1"
  if [ -s "$dst" ] && echo "$3  $dst" | sha256sum -c --status; then return 0; fi
  curl -fsSL "$2" -o "$dst.part" || { rm -f "$dst.part"; echo "Couldn't download $2"; exit 1; }
  if ! echo "$3  $dst.part" | sha256sum -c --status; then
    rm -f "$dst.part"; echo "$2 doesn't match its expected checksum; refusing to install it."; exit 1
  fi
  mv -f "$dst.part" "$dst"
}
vendor chart.umd.min.js "$CHARTJS_URL" 81ffafe13c37e1b25793b020d446f4d9739b949dadb7f9f79d709a0cad781c2f
# GIF encoder for the path-tree replay export (the worker must be same-origin)
vendor gif.js https://cdnjs.cloudflare.com/ajax/libs/gif.js/0.2.0/gif.js a8b111071bb3b123c302e6182c01d6b3550f93a4b627398b07c46875d84090bb
vendor gif.worker.js https://cdnjs.cloudflare.com/ajax/libs/gif.js/0.2.0/gif.worker.js ca9e3048557ec05d619e18b83403cd3669c88939e5fa2d6034ce7625d445970d

echo "==> python environment"
# (re)create the venv if missing or built with an older Python than the one required
if [ ! -x "$APP/venv/bin/python" ] || ! "$APP/venv/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  rm -rf "$APP/venv"
  "$PY" -m venv "$APP/venv"
fi
# exact versions, only files whose hashes are in requirements.txt, and only pre-built wheels (no build step that
# could fetch unpinned build tools)
"$APP/venv/bin/pip" install -q --disable-pip-version-check --require-hashes --only-binary :all: -r "$APP/requirements.txt"

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

cat > /usr/bin/bgpcanary-admin <<EOF
#!/bin/sh
# runs the bgpcanary command line as the service user (runuser is on every EL system and a small Alpine package;
# sudo may not be)
[ "\$(id -u)" = 0 ] || { echo "Run as root: $SUDO bgpcanary-admin \$*" >&2; exit 1; }
cd /opt/bgpcanary && exec runuser -u bgpcanary -- env BGPCANARY_CONFIG=/etc/bgpcanary/config.toml /opt/bgpcanary/venv/bin/python -m bgpcanary.cli "\$@"
EOF
chmod 755 /usr/bin/bgpcanary-admin
ln -sf "$APP/deploy/update.sh" /usr/bin/bgpcanary-update
ln -sf "$APP/deploy/uninstall.sh" /usr/sbin/bgpcanary-uninstall

echo "==> TLS (self-signed unless a certificate already exists)"
[ -d "$(dirname "$KEY")" ] || install -d -m 700 "$(dirname "$KEY")"
if [ ! -f "$CERT" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout "$KEY" -out "$CERT" \
    -subj "/CN=$FQDN" -addext "subjectAltName=DNS:$FQDN" 2>/dev/null
  chmod 600 "$KEY"
fi

echo "==> nginx"
# HTTP/2 syntax: "http2 on;" from nginx 1.25.1 (EL10, Alpine); "listen ... http2" before that (EL9)
NGV=$(nginx -v 2>&1 | sed -n 's|.*nginx/\([0-9.]*\).*|\1|p')
if [ "$(printf '%s\n1.25.1\n' "$NGV" | sort -V | head -1)" = 1.25.1 ]; then H2_LISTEN=""; H2_ON="http2 on;"; else H2_LISTEN=" http2"; H2_ON=""; fi
sed -e "s/__FQDN__/$FQDN/g" -e "s/__H2_LISTEN__/$H2_LISTEN/g" -e "s/__H2_ON__/$H2_ON/g" \
    -e "s|__CERT__|$CERT|" -e "s|__KEY__|$KEY|" -e "s|__WEBROOT__|$WEBROOT|" \
    "$SRC/deploy/nginx-bgpcanary.conf" > "$NGINX_D/bgpcanary.conf"
chmod 644 "$NGINX_D/bgpcanary.conf"
if [ "$OS" = alpine ] && ! grep -qs '^# bgpcanary' "$NGINX_D/default.conf"; then
  # Alpine's stock site claims default_server on port 80 too. It's replaced by a stub rather than deleted: apk
  # would put a deleted file back with the next nginx upgrade, and nginx would then refuse to reload or start.
  [ ! -f "$NGINX_D/default.conf" ] || mv "$NGINX_D/default.conf" "$NGINX_D/default.conf.orig"
  echo "# bgpcanary disabled the stock site here (kept as default.conf.orig); its own server is in bgpcanary.conf" > "$NGINX_D/default.conf"
fi
[ "$OS" != el ] || setsebool -P httpd_can_network_connect 1 2>/dev/null || true
nginx -t -q
svc_enable nginx
svc start nginx
svc reload nginx

if [ "$OS" = el ]; then   # (Alpine has no firewall unless you add one; then open 80 and 443 yourself)
  echo "==> firewall"
  if systemctl is-active -q firewalld; then
    firewall-cmd -q --permanent --add-service=http --add-service=https
    firewall-cmd -q --reload
  fi
fi

echo "==> service"
if [ "$OS" = el ]; then
  install -m 644 "$SRC/deploy/bgpcanary.service" /etc/systemd/system/bgpcanary.service
  systemctl daemon-reload
  svc_enable bgpcanary
  systemctl restart bgpcanary
  sleep 2
  systemctl --no-pager --lines=0 status bgpcanary | head -3
else
  # OpenRC: supervise-daemon keeps it running (deploy/bgpcanary.initd). There's no journal, so it logs to a file of
  # its own, which logrotate turns over weekly from Alpine's daily cron.
  install -d -o bgpcanary -g bgpcanary -m 750 /var/log/bgpcanary
  install -m 755 "$SRC/deploy/bgpcanary.initd" /etc/init.d/bgpcanary
  install -m 644 "$SRC/deploy/bgpcanary.logrotate" /etc/logrotate.d/bgpcanary
  svc_enable bgpcanary
  rc-service bgpcanary restart || { rc-service bgpcanary zap; rc-service bgpcanary start; }   # (zap: forget a crash)
  sleep 2
  rc-service bgpcanary status || true
fi

echo "==> default admin account"
CREDS=$(/usr/bin/bgpcanary-admin ensure-admin)
if [ -n "$CREDS" ]; then
  umask 077
  { echo "bgpcanary initial admin login (created $(date -Is)). You must change the password at first login."
    echo "$CREDS"
    echo
    echo "This file is only a record of the generated password: editing it does NOT change the password."
    echo "To set a new admin password: $SUDO bgpcanary-admin useradd admin"; } > "$ETC/initial-admin-password"
  chmod 600 "$ETC/initial-admin-password"
  echo
  echo "  Web login created:"
  echo "$CREDS" | sed 's/^/    /'
  echo "  (also saved to $ETC/initial-admin-password; you must change the password at first login)"
else
  echo "    users already exist; no default account created"
  # the generated password stops working once it's changed: don't leave it lying around after that
  # (only when the user list could be read: if that fails, keep the file)
  if [ -f "$ETC/initial-admin-password" ] && USERS=$(/usr/bin/bgpcanary-admin users) && [ -n "$USERS" ] \
     && ! grep -q "(must change password)" <<<"$USERS"; then
    rm -f "$ETC/initial-admin-password"
    echo "    removed $ETC/initial-admin-password (its temporary password has been changed)"
  fi
fi
if [ "$ATLAS_PROBE" = 1 ]; then
  echo "==> RIPE Atlas software probe"
  if [ "$OS" = alpine ]; then
    echo "    RIPE packages the software probe for Enterprise Linux and Debian only; not installed"
  else
    bash "$SRC/deploy/atlas-probe.sh" || echo "    probe install failed; bgpcanary itself is fine. Retry: $SUDO $APP/deploy/install.sh --atlas-probe-only"
  fi
fi
echo
echo "Done: bgpcanary $VERSION. Open https://$FQDN/"
echo "(The certificate is self-signed; see docs/INSTALL.md for using a real one.)"
if ! grep -q '^asn = [1-9]' "$ETC/config.toml"; then
  echo "Sign in as admin and you'll be taken to setup to enter your ASN and prefixes."
fi
