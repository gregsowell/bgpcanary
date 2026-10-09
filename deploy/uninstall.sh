#!/usr/bin/env bash
# Remove bgpcanary (installed as /usr/sbin/bgpcanary-uninstall).
#
#   sudo bgpcanary-uninstall          remove the program, service and web server config; KEEP the data
#                                     (/var/lib/bgpcanary), settings (/etc/bgpcanary) and certificate
#   sudo bgpcanary-uninstall --purge  also delete the database, backups, settings, certificate and the user
#
# Packages installed alongside it (nginx, python, sqlite) and the optional RIPE Atlas probe are left alone.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo $0 $*"; exit 1; }
PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1
echo "This removes bgpcanary from this server."
[ $PURGE = 1 ] && echo "--purge: the database, all history, backups, settings and certificate will be DELETED permanently."
read -r -p "Continue? [y/N] " a; [ "$a" = y ] || [ "$a" = Y ] || exit 1
systemctl disable -q --now bgpcanary 2>/dev/null || true
rm -f /etc/systemd/system/bgpcanary.service /etc/nginx/conf.d/bgpcanary.conf
systemctl daemon-reload
systemctl reload nginx 2>/dev/null || true
rm -f /usr/bin/bgpcanary-admin /usr/bin/bgpcanary-update /usr/sbin/bgpcanary-uninstall
rm -rf /opt/bgpcanary
if [ $PURGE = 1 ]; then
  rm -rf /var/lib/bgpcanary /etc/bgpcanary /etc/pki/tls/certs/bgpcanary.crt /etc/pki/tls/private/bgpcanary.key
  userdel bgpcanary 2>/dev/null || true
  echo "bgpcanary and all its data were removed."
else
  echo "bgpcanary was removed. Your data and settings are still in /var/lib/bgpcanary and /etc/bgpcanary;"
  echo "reinstalling picks them up again. To delete them as well: sudo rm -rf /var/lib/bgpcanary /etc/bgpcanary"
fi
