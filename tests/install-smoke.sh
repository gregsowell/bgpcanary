#!/usr/bin/env bash
# End-to-end test of the server installer. Run as root INSIDE A THROWAWAY machine or container, never on a real
# server: it installs bgpcanary, re-installs it, runs bgpcanary-update and ends with bgpcanary-uninstall --purge.
#
#   bash tests/install-smoke.sh [SRC]       SRC: the source tree to install (default: the one this script is in)
#
# CI runs it on Alpine in a container (see .github/workflows/ci.yml for the OpenRC incantation). On Rocky /
# AlmaLinux it needs systemd, so use a VM or a systemd container such as rockylinux/rockylinux:10-ubi-init.
# Prints PASS/FAIL lines and stops at the first failure.
set -euo pipefail
SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
[ "$(id -u)" = 0 ] || { echo "run as root, inside a throwaway system"; exit 1; }
. /etc/os-release
J=$(mktemp -d); trap 'rm -rf "$J"' EXIT
pass=0
ok() { echo "PASS  $1"; pass=$((pass + 1)); }
fail() { echo "FAIL  $1"; echo "---- service log:"; logs; exit 1; }
check() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }
out_has() {   # out_has PATTERN COMMAND... : COMMAND's whole output contains PATTERN (no "| grep -q" under pipefail)
  local pat="$1"; shift
  "$@" > "$J/out" 2>&1 || true
  grep -qi -- "$pat" "$J/out"
}
if [ -d /run/systemd/system ]; then
  INIT=systemd; CERT=/etc/pki/tls/certs/bgpcanary.crt; KEY=/etc/pki/tls/private/bgpcanary.key
  logs() { journalctl -u bgpcanary -n 30 --no-pager 2>/dev/null || true; }
  running() { systemctl is-active -q bgpcanary; }
  enabled() { systemctl is-enabled -q bgpcanary; }
else
  INIT=openrc; CERT=/etc/ssl/certs/bgpcanary.crt; KEY=/etc/ssl/private/bgpcanary.key
  logs() { tail -n 30 /var/log/bgpcanary/bgpcanary.log 2>/dev/null || true; }
  running() { rc-service bgpcanary status >/dev/null 2>&1; }
  enabled() { out_has '^ *bgpcanary |' rc-update show default; }
fi
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/bgpcanary/__init__.py")
healthz() { curl -fsS --max-time 5 http://127.0.0.1:8080/healthz 2>/dev/null | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p'; }
up() { local _; for _ in $(seq 60); do [ "$(healthz)" = "$VERSION" ] && return 0; sleep 1; done; return 1; }
http() { curl -sk --max-time 10 -o /dev/null -w '%{http_code} %{redirect_url}' "$@"; }   # "STATUS REDIRECT"

echo "== install bgpcanary $VERSION on $PRETTY_NAME ($INIT)"
bash "$SRC/deploy/install.sh" --fqdn bgp.example.com > "$J/install.log" 2>&1 || { cat "$J/install.log"; fail "install.sh"; }
ok "install.sh finished"
check "the service is running, and enabled at boot" 'running && enabled'
check "it answers /healthz with version $VERSION" up
check "nginx serves it over HTTPS (self-signed) and sends plain HTTP there" \
  '[ "$(http https://127.0.0.1/login)" = "200 " ] && [ "$(http http://127.0.0.1/)" = "301 https://127.0.0.1/" ]'
check "the security headers are on" 'out_has "^strict-transport-security:" curl -skI --max-time 10 https://127.0.0.1/login'
check "the data directory belongs to the service user only" '[ "$(stat -c %U:%G:%a /var/lib/bgpcanary)" = bgpcanary:bgpcanary:750 ]'
check "the config is readable by root and the service only" '[ "$(stat -c %U:%G:%a /etc/bgpcanary/config.toml)" = root:bgpcanary:640 ]'
check "the initial admin password is in a root-only file, and the installer printed it" \
  '[ "$(stat -c %U:%a /etc/bgpcanary/initial-admin-password)" = root:600 ] && grep -q "Web login created" "$J/install.log"'
check "bgpcanary-admin works: admin exists and must change the password" 'out_has "^admin .*must change password" bgpcanary-admin users'
check "the certificate exists and its key is root-only" '[ -s "$CERT" ] && [ "$(stat -c %U:%a "$KEY")" = root:600 ]'
check "the vendored front-end libraries passed their checksums" \
  '[ -s /opt/bgpcanary/bgpcanary/static/vendor/chart.umd.min.js ] && [ -s /opt/bgpcanary/bgpcanary/static/vendor/gif.worker.js ]'
if [ "$INIT" = systemd ]; then
  check "the unit's sandbox is in effect" 'out_has "^NoNewPrivileges=yes" systemctl show bgpcanary -p NoNewPrivileges'
else
  pid() { pgrep -f 'venv/bin/uvicorn bgpcanary.web:app' | head -n 1; }
  check "the service runs as bgpcanary with no-new-privileges" \
    'P=$(pid); [ -n "$P" ] && grep -q "^Uid:[[:space:]]*$(id -u bgpcanary)[[:space:]]" "/proc/$P/status" && grep -q "^NoNewPrivs:[[:space:]]*1" "/proc/$P/status"'
  check "the log file is the service's, readable by root and it only" '[ "$(stat -c %U:%G:%a /var/log/bgpcanary/bgpcanary.log)" = bgpcanary:bgpcanary:640 ]'
  check "...and has the app's startup lines" 'grep -q "Application startup complete" /var/log/bgpcanary/bgpcanary.log'
  check "logrotate accepts its configuration" '! out_has "error" logrotate -d /etc/logrotate.d/bgpcanary'
  check "the stock nginx site is disabled and kept as default.conf.orig" \
    '[ -f /etc/nginx/http.d/default.conf.orig ] && grep -q "^# bgpcanary" /etc/nginx/http.d/default.conf'
fi

echo "== re-run the installer (that's what an update does)"
cp /etc/bgpcanary/config.toml "$J/config.before"; sha256sum "$CERT" > "$J/cert.before"
bash "$SRC/deploy/install.sh" > "$J/install2.log" 2>&1 || { cat "$J/install2.log"; fail "install.sh (re-run)"; }
check "re-run: healthy again" up
check "re-run: config and certificate untouched, no second admin account" \
  '[ "$(sha256sum < /etc/bgpcanary/config.toml)" = "$(sha256sum < "$J/config.before")" ] && sha256sum -c --status "$J/cert.before" && grep -q "users already exist" "$J/install2.log"'
check "re-run: a database backup was taken first" 'ls /var/backups/bgpcanary/before-*.db >/dev/null 2>&1'

echo "== bgpcanary-update --from, then --rollback"
bgpcanary-update --from "$SRC" > "$J/update.log" 2>&1 || { cat "$J/update.log"; fail "bgpcanary-update --from"; }
check "update: reports the running version" 'grep -q "Updated: bgpcanary $VERSION -> $VERSION is running" "$J/update.log"'
check "update: kept a database and a config backup" 'ls /var/backups/bgpcanary/before-update-*.db /var/backups/bgpcanary/config-*.tar.gz >/dev/null 2>&1'
echo y | bgpcanary-update --rollback > "$J/rollback.log" 2>&1 || { cat "$J/rollback.log"; fail "bgpcanary-update --rollback"; }
check "rollback: the service came back" 'grep -q "Rolled back: bgpcanary $VERSION is running" "$J/rollback.log" && up'

echo "== bgpcanary-uninstall --purge"
echo y | bgpcanary-uninstall --purge > "$J/uninstall.log" 2>&1 || { cat "$J/uninstall.log"; fail "bgpcanary-uninstall --purge"; }
check "purge: program, data, settings, user and service are gone" \
  '! [ -e /opt/bgpcanary ] && ! [ -e /var/lib/bgpcanary ] && ! [ -e /etc/bgpcanary ] && ! id bgpcanary >/dev/null 2>&1 && ! running'
check "purge: nginx no longer serves it" \
  '! [ -e /etc/nginx/conf.d/bgpcanary.conf ] && ! [ -e /etc/nginx/http.d/bgpcanary.conf ] && [ "$(http http://127.0.0.1/login)" != "301 https://127.0.0.1/login" ]'
if [ "$INIT" = openrc ]; then
  check "purge: the stock site is back; the service, log and logrotate entries are gone" \
    '[ -f /etc/nginx/http.d/default.conf ] && ! grep -q "^# bgpcanary" /etc/nginx/http.d/default.conf && ! [ -e /etc/init.d/bgpcanary ] && ! [ -e /etc/logrotate.d/bgpcanary ] && ! [ -e /var/log/bgpcanary ]'
fi
echo "ALL $pass CHECKS PASSED on $PRETTY_NAME"
