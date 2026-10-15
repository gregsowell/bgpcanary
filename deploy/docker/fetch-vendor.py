#!/usr/bin/env python3
"""Download the front-end libraries that deploy/install.sh vendors, checked against the same SHA-256 sums, so the
container image and a server install always get exactly the same files. Used by the Dockerfile:

    python fetch-vendor.py deploy/install.sh bgpcanary/static/vendor
"""
import hashlib
import os
import re
import sys
import urllib.request

script, dest = sys.argv[1], sys.argv[2]
with open(script, encoding="utf-8") as f:
    text = f.read()
urls = dict(re.findall(r'^([A-Z_]+)="(https://[^"]+)"$', text, re.M))   # e.g. CHARTJS_URL="https://..."
items = re.findall(r"^vendor (\S+) (\S+) ([0-9a-f]{64})$", text, re.M)   # vendor FILE URL SHA256
if len(items) < 3:
    sys.exit(f"expected the 'vendor FILE URL SHA256' lines in {script}, found {len(items)}")
os.makedirs(dest, exist_ok=True)
for name, url, sha in items:
    m = re.fullmatch(r'"?\$\{?([A-Z_]+)\}?"?', url)
    if m:
        url = urls.get(m.group(1), "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or not url.startswith("https://"):
        sys.exit(f"unexpected vendor line: {name} {url}")
    with urllib.request.urlopen(url, timeout=60) as r:
        body = r.read()
    if hashlib.sha256(body).hexdigest() != sha:
        sys.exit(f"{url} doesn't match its expected checksum; refusing to use it")
    with open(os.path.join(dest, name), "wb") as f:
        f.write(body)
    print(f"vendored {name} ({len(body)} bytes, sha256 ok)")
