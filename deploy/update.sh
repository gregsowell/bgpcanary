#!/usr/bin/env bash
# bgpcanary-update: install a newer release of bgpcanary (installed as /usr/bin/bgpcanary-update).
#
#   sudo bgpcanary-update                  update to the latest release on GitHub
#   sudo bgpcanary-update --check          only say whether a newer release exists
#   sudo bgpcanary-update --version 1.2.0  install a specific release (also to go back to an older one)
#   sudo bgpcanary-update --from DIR|FILE  install from a local source directory or .tar.gz (no download)
#   sudo bgpcanary-update --rollback       undo the last update: previous code + the database from before it
#
# Every update first saves the database and configuration to /var/backups/bgpcanary/, then runs the new
# release's installer (which keeps your settings, data, users and certificate) and checks the service is healthy.
set -euo pipefail
# The installer replaces this very file; run from a private copy so bash never reads a half-replaced script.
if [ -z "${BGPCANARY_UPDATE_COPY:-}" ]; then
  T=$(mktemp /tmp/bgpcanary-update.XXXXXX)
  cp "$(readlink -f "$0")" "$T"
  BGPCANARY_UPDATE_COPY="$T" exec bash "$T" "$@"
fi
trap 'rm -f "$BGPCANARY_UPDATE_COPY"' EXIT
APP=/opt/bgpcanary
DATA=/var/lib/bgpcanary
ETC=/etc/bgpcanary
current() { sed -n 's/^__version__ = "\(.*\)"/\1/p' "$APP/bgpcanary/__init__.py" 2>/dev/null; }
REPO=$(sed -n 's/^REPO = "\([^"]*\)".*/\1/p' "$APP/bgpcanary/__init__.py" 2>/dev/null)
REPO=${REPO:-gregsowell/bgpcanary}
MODE=latest; WANT=""; FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check; shift ;;
    --version) MODE=version; WANT="${2#v}"; shift 2 ;;
    --from) MODE=from; FROM="$2"; shift 2 ;;
    --rollback) MODE=rollback; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)"; exit 1 ;;
  esac
done
[ "$(id -u)" = 0 ] || [ "$MODE" = check ] || { echo "Run as root: sudo bgpcanary-update"; exit 1; }
CUR=$(current)

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

# (the config is readable only by root and the service; --check runs without sudo, so a failed read is fine)
PORT=$(sed -n 's/^listen_port *= *\([0-9]*\).*/\1/p' "$ETC/config.toml" 2>/dev/null | head -1 || true)
healthy() {   # wait up to a minute for the new version to answer
  for _ in $(seq 30); do
    v=$(curl -s --max-time 2 "http://127.0.0.1:${PORT:-8080}/healthz" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')
    [ -n "$v" ] && { echo "$v"; return 0; }
    sleep 2
  done
  return 1
}

latest() {
  curl -fsSL -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name": *"v\{0,1\}\([^"]*\)".*/\1/p' | awk 'NR == 1'
}

newer() {   # is $1 a newer version than $2? (sort -V understands 1.10.0 > 1.9.0)
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]
}

if [ "$MODE" = rollback ]; then
  [ -d "$APP/bgpcanary.old" ] || { echo "Nothing to roll back to (no previous version kept)."; exit 1; }
  DB=$(find "$BK" -maxdepth 1 -type f -name 'before-update-*.db' -printf '%T@ %p\n' 2>/dev/null | sort -rn \
       | awk 'NR == 1 { sub(/^[^ ]+ /, ""); print }')
  echo "Rolling back from $CUR to $(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$APP/bgpcanary.old/__init__.py")"
  [ -n "$DB" ] && echo "and restoring the database saved before the update: $DB"
  read -r -p "Continue? [y/N] " a; [ "$a" = y ] || [ "$a" = Y ] || exit 1
  systemctl stop bgpcanary
  mv "$APP/bgpcanary" "$APP/bgpcanary.rolledback.$(date +%s)"
  mv "$APP/bgpcanary.old" "$APP/bgpcanary"
  if [ -n "$DB" ]; then
    backup_db "rolled-back-from-$CUR-$(date +%Y%m%d-%H%M%S).db" || true
    # written by the service user: root never writes into $DATA, which that account controls
    runuser -u bgpcanary -- rm -f "$DATA/bgpcanary.db-wal" "$DATA/bgpcanary.db-shm" "$DATA/bgpcanary.db.restore"
    runuser -u bgpcanary -- sh -c 'umask 027; cat > "$1"' sh "$DATA/bgpcanary.db.restore" < "$DB"
    runuser -u bgpcanary -- mv -f "$DATA/bgpcanary.db.restore" "$DATA/bgpcanary.db"
  fi
  systemctl start bgpcanary
  v=$(healthy) && echo "Rolled back: bgpcanary $v is running." || { echo "The service didn't come back; see: journalctl -u bgpcanary -n 50"; exit 1; }
  exit 0
fi

if [ "$MODE" = check ] || [ "$MODE" = latest ]; then
  L=$(latest || true)
  [ -n "$L" ] || { echo "Couldn't read the latest release from github.com/$REPO (no internet, or no release published yet)."; exit 1; }
  if ! newer "$L" "$CUR"; then echo "bgpcanary $CUR is up to date (latest release: $L)."; exit 0; fi
  echo "bgpcanary $L is available (installed: $CUR). Release notes: https://github.com/$REPO/releases/tag/v$L"
  [ "$MODE" = check ] && exit 0
  WANT="$L"
fi

WORK=$(mktemp -d); trap 'rm -rf "$WORK" "$BGPCANARY_UPDATE_COPY"' EXIT
if [ "$MODE" = from ]; then
  if [ -d "$FROM" ]; then SRC="$(cd "$FROM" && pwd)"
  else tar -xzf "$FROM" -C "$WORK"; SRC=$(find "$WORK" -maxdepth 3 -name install.sh -path '*/deploy/*' -printf '%h\n' -quit); SRC="${SRC%/deploy}"; fi
else
  echo "==> downloading bgpcanary $WANT"
  curl -fsSL "https://github.com/$REPO/archive/refs/tags/v$WANT.tar.gz" -o "$WORK/src.tar.gz" \
    || { echo "Couldn't download release v$WANT from github.com/$REPO"; exit 1; }
  tar -xzf "$WORK/src.tar.gz" -C "$WORK"
  SRC=$(find "$WORK" -maxdepth 1 -mindepth 1 -type d -print -quit)   # (-quit, not "| head": under pipefail an early-closed pipe aborts the script)
fi
[ -f "$SRC/deploy/install.sh" ] && [ -f "$SRC/bgpcanary/__init__.py" ] || { echo "That doesn't look like a bgpcanary source tree: $SRC"; exit 1; }
NEW=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/bgpcanary/__init__.py")

echo "==> backing up the database and configuration ($CUR)"
STAMP=$(date +%Y%m%d-%H%M%S)
backup_db "before-update-$CUR-$STAMP.db" || { echo "Couldn't back up the database; nothing was changed."; exit 1; }
(umask 077; tar -czf "$BK/config-$CUR-$STAMP.tar.gz" -C / "${ETC#/}" 2>/dev/null) || true
keep_newest 3 'before-update-*.db'
keep_newest 5 'config-*.tar.gz'

echo "==> installing bgpcanary $NEW (was $CUR)"
bash "$SRC/deploy/install.sh"
if v=$(healthy) && [ "$v" = "$NEW" ]; then
  echo
  echo "Updated: bgpcanary $CUR -> $NEW is running. (If something looks wrong: sudo bgpcanary-update --rollback)"
else
  echo
  echo "The new version didn't report healthy. Check: journalctl -u bgpcanary -n 50"
  echo "To go back to $CUR: sudo bgpcanary-update --rollback"
  exit 1
fi
