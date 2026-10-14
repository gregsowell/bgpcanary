#!/usr/bin/env bash
# Install (or upgrade) the RIPE Atlas software probe on this machine. Run as root; called by
# `install.sh --atlas-probe` / `--atlas-probe-only`, or directly. Safe to re-run.
#
# Hosting a probe earns RIPE Atlas credits (~21,600/day), which pay for bgpcanary's optional Atlas measurements.
# After installing, register the printed public key at https://atlas.ripe.net/register/swprobe
set -euo pipefail
CHECKSUMS_URL=https://github.com/RIPE-NCC/ripe-atlas-software-probe/releases/latest/download/CHECKSUMS
REPO_BASE=https://ftp.ripe.net/ripe/atlas/software-probe
CONF=/etc/ripe-atlas/config.txt
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

. /etc/os-release
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
curl -fsSL -o "$TMP/CHECKSUMS" "$CHECKSUMS_URL"
if command -v dnf >/dev/null; then
  EL=$(echo "${PLATFORM_ID:-}" | cut -d: -f2)
  PKG=$(grep -o "ripe-atlas-repo-[0-9.-]*\.$EL\.noarch\.rpm" "$TMP/CHECKSUMS" | sort -uV | tail -1)
  [ -n "$PKG" ] || { echo "no RIPE Atlas repository package for '$EL'"; exit 1; }
  curl -fsSL -o "$TMP/$PKG" "$REPO_BASE/$EL/noarch/$PKG"
  grep -q "$(cd "$TMP" && sha256sum "$PKG")" "$TMP/CHECKSUMS" || { echo "checksum mismatch for $PKG"; exit 1; }
  echo "    verified $PKG"
  rpm -q ripe-atlas-repo >/dev/null 2>&1 || rpm -Uvh --quiet "$TMP/$PKG"
  if rpm -q ripe-atlas-probe >/dev/null 2>&1; then dnf -y -q upgrade ripe-atlas-probe; else dnf -y -q install ripe-atlas-probe; fi
elif command -v apt-get >/dev/null; then
  ARCH=$(dpkg --print-architecture)
  PKG=$(grep -o "ripe-atlas-repo_[0-9.-]*_all\.deb" "$TMP/CHECKSUMS" | sort -uV | tail -1)
  curl -fsSL -o "$TMP/$PKG" "$REPO_BASE/debian/dists/$VERSION_CODENAME/main/binary-$ARCH/$PKG"
  grep -q "$(cd "$TMP" && sha256sum "$PKG")" "$TMP/CHECKSUMS" || { echo "checksum mismatch for $PKG"; exit 1; }
  echo "    verified $PKG"
  dpkg -i "$TMP/$PKG" >/dev/null && apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q ripe-atlas-probe
else
  echo "unsupported platform; see https://github.com/RIPE-NCC/ripe-atlas-software-probe"; exit 1
fi

port_taken() { ss -Hltn "sport = :$1" | grep -q .; }

# The probe uses TCP 8080 (httppost) and 2023 (telnetd) on localhost. bgpcanary itself listens on 8080, so
# move the probe to free ports. Done after the package is installed (it creates /etc/ripe-atlas and the key
# itself) with the probe stopped; an existing config.txt is kept and only missing port settings are added.
systemctl stop ripe-atlas.service 2>/dev/null || true
touch "$CONF"
pick() {   # pick <setting> <default> <first alternative>
  local key=$1 def=$2 p=$3
  grep -q "^$key=" "$CONF" && return
  port_taken "$def" || return 0
  while port_taken "$p"; do p=$((p + 1)); done
  echo "$key=$p" >> "$CONF"
  echo "    port $def is in use here; probe $key set to $p"
}
pick HTTP_POST_PORT 8080 8081
pick TELNETD_PORT 2023 2024
chown ripe-atlas:ripe-atlas "$CONF"

systemctl enable -q ripe-atlas.service
systemctl restart ripe-atlas.service
sleep 3
systemctl is-active -q ripe-atlas.service && echo "    ripe-atlas probe is running" || echo "    WARNING: ripe-atlas probe is not running (journalctl -u ripe-atlas)"
# /etc/ripe-atlas is private to the probe; give bgpcanary a copy of the public key to show in Settings
# (written by the service user: root doesn't write into /var/lib/bgpcanary, which that account controls)
if [ -d /var/lib/bgpcanary ] && id bgpcanary &>/dev/null; then
  runuser -u bgpcanary -- sh -c 'umask 022; cat > "$1.tmp" && mv -f "$1.tmp" "$1"' sh /var/lib/bgpcanary/atlas-probe-key.pub \
    < /etc/ripe-atlas/probe_key.pub || echo "    (couldn't copy the probe's public key for the Settings page)"
fi
echo
echo "  Probe public key (register it at https://atlas.ripe.net/register/swprobe):"
sed 's/^/    /' /etc/ripe-atlas/probe_key.pub
