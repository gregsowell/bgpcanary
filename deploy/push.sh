#!/usr/bin/env bash
# Dev helper: push the working tree to the VM and run the installer.
set -euo pipefail
HOST="${1:?usage: deploy/push.sh [user@]host   (copies the tree to host and runs install.sh)}"
cd "$(dirname "$0")/.."
tar --force-local --exclude=__pycache__ -czf "${TEMP:-/tmp}/bgpcanary.tgz" bgpcanary deploy requirements.txt
scp -q "${TEMP:-/tmp}/bgpcanary.tgz" "$HOST:/tmp/bgpcanary.tgz"
ssh "$HOST" 'rm -rf ~/bgpcanary-src && mkdir ~/bgpcanary-src && tar -xzf /tmp/bgpcanary.tgz -C ~/bgpcanary-src && cd ~/bgpcanary-src && sudo bash deploy/install.sh 2>&1 | tail -4'
