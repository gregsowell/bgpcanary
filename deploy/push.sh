#!/usr/bin/env bash
# Dev helper: push the working tree to a test server and run the installer there (as root, or through sudo).
set -euo pipefail
HOST="${1:?usage: deploy/push.sh [user@]host   (copies the tree to host and runs install.sh)}"
cd "$(dirname "$0")/.."
tar --force-local --exclude=__pycache__ -czf "${TEMP:-/tmp}/bgpcanary.tgz" bgpcanary deploy requirements.txt
scp -q "${TEMP:-/tmp}/bgpcanary.tgz" "$HOST:/tmp/bgpcanary.tgz"
ssh "$HOST" 'rm -rf ~/bgpcanary-src && mkdir ~/bgpcanary-src && tar -xzf /tmp/bgpcanary.tgz -C ~/bgpcanary-src && cd ~/bgpcanary-src && if [ "$(id -u)" = 0 ]; then bash deploy/install.sh; else sudo bash deploy/install.sh; fi 2>&1 | tail -4'
