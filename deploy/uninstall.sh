#!/usr/bin/env bash
# Remove bgpcanary (installed as /usr/sbin/bgpcanary-uninstall).
#
#   sudo bgpcanary-uninstall          remove the program, service and web server config; KEEP the data
#                                     (/var/lib/bgpcanary), settings (/etc/bgpcanary) and certificate
#   sudo bgpcanary-uninstall --purge  also delete the database, backups, settings, certificate and the user
#
# Packages installed alongside it (nginx, python, sqlite) and the optional RIPE Atlas probe are left alone.
set -euo pipefail
SUDO=sudo; if ! command -v sudo >/dev/null && command -v doas >/dev/null; then SUDO=doas; fi   # (Alpine's default)
[ "$(id -u)" = 0 ] || { echo "Run as root: $SUDO $0 $*"; exit 1; }
PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1
echo "This removes bgpcanary from this server."
[ $PURGE = 1 ] && echo "--purge: the database, all history, backups, settings and certificate will be DELETED permanently."
read -r -p "Continue? [y/N] " a; [ "$a" = y ] || [ "$a" = Y ] || exit 1
if [ -d /run/systemd/system ]; then
  systemctl disable -q --now bgpcanary 2>/dev/null || true
  rm -f /etc/systemd/system/bgpcanary.service
  systemctl daemon-reload
else   # OpenRC (Alpine)
  rc-service bgpcanary stop 2>/dev/null || true
  rc-update -q del bgpcanary default 2>/dev/null || true
  rm -f /etc/init.d/bgpcanary /etc/logrotate.d/bgpcanary
fi
rm -f /etc/nginx/conf.d/bgpcanary.conf /etc/nginx/http.d/bgpcanary.conf
if grep -qs '^# bgpcanary' /etc/nginx/http.d/default.conf; then   # put Alpine's stock site back
  if [ -f /etc/nginx/http.d/default.conf.orig ]; then mv -f /etc/nginx/http.d/default.conf.orig /etc/nginx/http.d/default.conf
  else rm -f /etc/nginx/http.d/default.conf; fi
fi
if [ -d /run/systemd/system ]; then systemctl reload nginx; else rc-service nginx reload; fi 2>/dev/null || true
rm -f /usr/bin/bgpcanary-admin /usr/bin/bgpcanary-update /usr/sbin/bgpcanary-uninstall
rm -rf /opt/bgpcanary
if [ $PURGE = 1 ]; then
  rm -rf /var/lib/bgpcanary /var/backups/bgpcanary /etc/bgpcanary /var/log/bgpcanary \
    /etc/pki/tls/certs/bgpcanary.crt /etc/pki/tls/private/bgpcanary.key /etc/ssl/certs/bgpcanary.crt /etc/ssl/private/bgpcanary.key
  if command -v userdel >/dev/null; then userdel bgpcanary 2>/dev/null || true
  else deluser bgpcanary 2>/dev/null || true; delgroup bgpcanary 2>/dev/null || true; fi   # (busybox)
  echo "bgpcanary and all its data were removed."
else
  echo "bgpcanary was removed. Your data, backups and settings are still in /var/lib/bgpcanary,"
  echo "/var/backups/bgpcanary and /etc/bgpcanary; reinstalling picks them up again."
  echo "To delete them as well: $SUDO rm -rf /var/lib/bgpcanary /var/backups/bgpcanary /etc/bgpcanary"
fi
