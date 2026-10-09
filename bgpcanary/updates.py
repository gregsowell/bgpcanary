"""Daily check for a newer bgpcanary release on GitHub (can be switched off in Settings > About & updates).

Only the public GitHub API is contacted (no data about this install is sent). The result is shown to admins;
updating itself is done on the server with `sudo bgpcanary-update`.
"""
import asyncio
import logging
import re
import time

import httpx

from . import REPO, __version__, db

log = logging.getLogger("bgpcanary.updates")
KV_ENABLED = "update_check_enabled"
KV_STATE = "update_check"


def parse(v):
    """'v1.2.3' / '1.2.3-rc1' -> comparable tuple; pre-releases sort before the release."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", (v or "").strip())
    if not m:
        return (0, 0, 0, 0, "")
    return int(m[1]), int(m[2]), int(m[3]), 0 if m[4] else 1, m[4] or ""


def enabled(conn):
    return bool(db.kv_get(conn, KV_ENABLED, True))


def state(conn):
    st = dict(db.kv_get(conn, KV_STATE, {}) or {})
    st["current"] = __version__
    st["available"] = bool(st.get("latest")) and parse(st["latest"]) > parse(__version__)
    return st


async def check(conn):
    """Ask GitHub for the latest published release. Returns the stored state."""
    st = db.kv_get(conn, KV_STATE, {}) or {}
    try:
        async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": f"bgpcanary/{__version__}"}) as c:
            r = await c.get(f"https://api.github.com/repos/{REPO}/releases/latest")
        if r.status_code == 404:
            st.update(checked=time.time(), error="no releases published yet")
        else:
            r.raise_for_status()
            d = r.json()
            st = {"checked": time.time(), "latest": d.get("tag_name", "").lstrip("v"), "url": d.get("html_url"),
                  "name": d.get("name") or d.get("tag_name"), "published": d.get("published_at"),
                  "notes": (d.get("body") or "")[:4000]}
    except Exception as ex:
        st.update(checked=time.time(), error=str(ex) or type(ex).__name__)
    db.kv_set(conn, KV_STATE, st)
    conn.commit()
    s = state(conn)
    if s["available"]:
        log.info("bgpcanary %s is available (running %s): %s", s["latest"], __version__, s.get("url"))
    return s


async def loop(conn):
    await asyncio.sleep(300)
    while True:
        if enabled(conn):
            await check(conn)
        await asyncio.sleep(86400)
