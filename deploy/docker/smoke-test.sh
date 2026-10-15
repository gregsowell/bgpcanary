#!/usr/bin/env bash
# Smoke test for a bgpcanary container image (CI and the release workflow run it; it also works locally):
#
#   deploy/docker/smoke-test.sh IMAGE [PLATFORM]       e.g.  deploy/docker/smoke-test.sh bgpcanary:test linux/arm64
#
# Runs the image the way docs/DOCKER.md does (read-only, no capabilities, a data volume) and checks the first start,
# the admin account, headers, signing in, the setup wizard's in-place restart, clean stops and a second start.
# Run it from the repository root. DOCKER=podman uses podman; PORT (18080) and WAIT (180 s) can be changed, and
# RUN_OPTS adds options to `docker run` (e.g. --add-host entries that keep the test off real collectors).
set -euo pipefail
IMG="$1"; PLAT="${2:-}"
D="${DOCKER:-docker}"
N="bgpcanary-smoke-$$"; V="$N-data"; P="${PORT:-18080}"; U="http://127.0.0.1:$P"
WAIT="${WAIT:-180}"
J=$(mktemp -d)
trap 'rm -rf "$J"; $D rm -f "$N" >/dev/null 2>&1; $D volume rm -f "$V" >/dev/null 2>&1' EXIT
pass=0
ok() { echo "PASS  $1"; pass=$((pass + 1)); }
fail() { echo "FAIL  $1"; echo "---- container log:"; $D logs "$N" 2>&1 | tail -n 60; exit 1; }
check() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }
healthz() { curl -fsS "$U/healthz" | python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
run_has() {   # run_has PATTERN COMMAND... : COMMAND's whole output (kept in $J/out) contains PATTERN
  local pat="$1"; shift   # (no "| grep -q": with pipefail, grep stopping early can fail or falsely pass a check)
  "$@" > "$J/out" 2>&1 || true
  grep -q -- "$pat" "$J/out"
}
post() {   # post PATH NAME=VALUE... : a browser-like form POST with this test's cookies; prints "STATUS REDIRECT"
  local path="$1"; shift
  local args=()
  for kv in "$@"; do args+=(--data-urlencode "$kv"); done
  curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' -c "$J/jar" -b "$J/jar" -H "Origin: $U" "${args[@]}" "$U$path"
}
start() {   # start [extra run options]
  # shellcheck disable=SC2086   # (RUN_OPTS: several options)
  $D run -d --name "$N" ${PLAT:+--platform "$PLAT"} --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges -v "$V:/var/lib/bgpcanary" -p "127.0.0.1:$P:8080" ${RUN_OPTS:-} "$@" "$IMG" >/dev/null
}
up() {   # wait for /healthz; with an argument, for a "started" time other than that one (= a restart happened)
  local _
  for _ in $(seq "$WAIT"); do
    if s=$(healthz started 2>/dev/null) && [ "$s" != "${1:-}" ]; then return 0; fi
    sleep 1
  done
  return 1
}
stopped_cleanly() {
  $D stop -t 30 "$N" >/dev/null && [ "$($D inspect -f '{{.State.ExitCode}}' "$N")" = 0 ]
}

echo "== first start: $IMG ${PLAT}"
start
check "answers /healthz" up
check "PID 1 is tini, the app runs as uid 10001" '[ "$($D exec "$N" cat /proc/1/comm)" = tini ] && [ "$($D exec "$N" id -u)" = 10001 ]'
$D logs "$N" > "$J/log" 2>&1
PW=$(sed -n '/^   password: /{s///p;q}' "$J/log")
check "the initial admin password is in the log" '[ -n "$PW" ]'
check "...and in the data volume, for its owner only" '[ "$($D exec "$N" stat -c "%a %u" /var/lib/bgpcanary/initial-admin-password)" = "600 10001" ]'
check "the database is readable by its owner only" '[ "$($D exec "$N" stat -c "%a" /var/lib/bgpcanary/bgpcanary.db)" = 600 ]'
check "config created with a random secret key, owner only" '[ "$($D exec "$N" stat -c "%a %u" /var/lib/bgpcanary/config.toml)" = "600 10001" ] && $D exec "$N" grep -Eq "^secret_key = \"[0-9a-f]{64}\"$" /var/lib/bgpcanary/config.toml'
check "bgpcanary-admin: admin must change the password" 'run_has "^admin .*must change password" $D exec "$N" bgpcanary-admin users'
check "bgpcanary-admin refuses to run as root" '! $D exec -u 0 "$N" bgpcanary-admin users >/dev/null 2>&1'
check "the healthcheck command passes" '$D exec "$N" python -m bgpcanary.container healthcheck >/dev/null'
curl -sS -D "$J/headers" -o "$J/login.html" -H "Host: bgp.example.com" "$U/login"
check "security headers are sent" 'grep -qi "^content-security-policy: default-src .self." "$J/headers" && grep -qi "^x-frame-options: DENY" "$J/headers" && grep -qi "^x-content-type-options: nosniff" "$J/headers"'
check "no server banner" '! grep -qi "^server:" "$J/headers"'
check "over plain HTTP the login page explains why signing in can't work" 'grep -q "came over plain HTTP" "$J/login.html"'
check "...but not on localhost, where browsers keep the cookie" 'run_has "action=\"/login\"" curl -sS "$U/login" && ! grep -q "came over plain HTTP" "$J/out"'
SUM=$(sed -n 's/^vendor chart\.umd\.min\.js .* \([0-9a-f]\{64\}\)$/\1/p' deploy/install.sh)
check "Chart.js is served and matches the installer's checksum" '[ -n "$SUM" ] && [ "$(curl -fsS "$U/static/vendor/chart.umd.min.js" | sha256sum | cut -d" " -f1)" = "$SUM" ]'
check "stops cleanly (exit code 0)" stopped_cleanly
$D rm "$N" >/dev/null

echo "== second start on the same volume: sign in, set up, restart in place"
start -e BGPCANARY_INSECURE_COOKIES=1
check "comes back" up
check "doesn't make another admin account" 'run_has "Application startup complete" $D logs "$N" && ! grep -q "password: " "$J/out"'
check "signs in with the initial password, which must then be changed" '[ "$(post /login username=admin "password=$PW" next=/)" = "303 $U/account/password" ]'
NEWPW="Smoke-test-$$-$RANDOM-pass"
check "changes the password" '[ "$(post /settings/password "current=$PW" "new=$NEWPW" "confirm=$NEWPW" back=/account/password)" = "303 $U/" ]'
check "a POST from another site is refused" '[ "$(curl -sS -o /dev/null -w "%{http_code}" -b "$J/jar" -H "Origin: https://other.example" --data asn=1 "$U/settings/monitor")" = 403 ]'
T1=$(healthz started)
check "the setup wizard saves" '[ "$(post /settings/monitor asn=64500 prefixes=192.0.2.0/24 | cut -d" " -f1)" = 303 ]'
check "...and the app restarts with the new settings" 'up "$T1" && [ "$(healthz configured)" = True ]'
check "...in place: the container itself didn't restart" '[ "$($D inspect -f "{{.RestartCount}}" "$N")" = 0 ] && [ "$($D inspect -f "{{.State.Running}}" "$N")" = true ] && run_has "restarting with the new settings" $D logs "$N"'
check "the initial password's file is gone now that it's changed" '! $D exec "$N" test -e /var/lib/bgpcanary/initial-admin-password'
check "still signed in after the restart" '[ "$(curl -sS -o /dev/null -w "%{http_code}" -b "$J/jar" "$U/settings/about")" = 200 ]'
check "the About page shows the container's update steps" 'run_has "docker compose pull" curl -sS -b "$J/jar" "$U/settings/about"'
check "stops cleanly again" stopped_cleanly
echo "$pass checks passed"
