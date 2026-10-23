"""Watched Path finder targets: each one is looked up and traced again on its own schedule, and, if asked, traced back
from their side with RIPE Atlas (at most every BACK_MINUTES: it costs credits). A change in the path your traffic
takes, or theirs, counts once CONFIRM checks in a row show it, so one odd traceroute doesn't; it goes into the
target's history either way, and out as an alert (event kind path_change) if the watch asks for that."""
import asyncio
import json
import logging
import time

from . import pathfinder

log = logging.getLogger("bgpcanary.watch")
MINUTES = (15, 30, 60, 180, 360, 720, 1440)
BACK_MINUTES = 360   # traces back from them at most this often (~60 credits per probe each)
CONFIRM = 2          # checks in a row that must show a new path before it counts as changed
_pruned = {"at": 0}


def watches(conn):
    out = []
    for r in conn.execute("SELECT * FROM pf_watch ORDER BY COALESCE(NULLIF(label, ''), query)"):
        w = dict(r)
        w["state"] = json.loads(w["state"] or "{}")
        out.append(w)
    return out


def get(conn, wid):
    return next((w for w in watches(conn) if w["id"] == wid), None)


def find(conn, query):
    """The watch for a query, however it was typed (as13335 = AS13335), or None."""
    try:
        q = pathfinder.key_of(query)
    except ValueError:
        return None
    return next((w for w in watches(conn) if w["query"] == q), None)


def save(conn, query, label="", minutes=60, alert=True, back=False, user=""):
    """Watch `query` (an address, prefix, AS number or host name), or change how it's watched. ValueError if it
    isn't one of those."""
    conn.execute("INSERT INTO pf_watch(query,label,minutes,alert,back,created,created_by,state) VALUES(?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(query) DO UPDATE SET label=excluded.label, minutes=excluded.minutes, "
                 "alert=excluded.alert, back=excluded.back",
                 (pathfinder.key_of(query), (label or "").strip()[:60], minutes if minutes in MINUTES else 60,
                  1 if alert else 0, 1 if back else 0, time.time(), user, "{}"))
    conn.commit()


def remove(conn, wid):
    conn.execute("DELETE FROM pf_watch WHERE id=?", (wid,))
    conn.commit()


def path_text(path, names):
    return " → ".join(f"AS{a}" + (f" {names[str(a)].split()[0][:14]}" if names.get(str(a)) else "") for a in path)


def _confirm(svc, w, kind, new, res, now):
    """Fold one check's view of a path (kind "active": your traffic's, "back": theirs) into the watch's state; open
    a path_change event when a different path has been seen CONFIRM times in a row and the watch alerts."""
    st = w["state"]
    cur = st.get(kind)
    if new is None:
        return
    if cur is None:
        st[kind], st[kind + "_since"] = new, now
        st.pop(kind + "_pending", None)
        return
    if pathfinder.same(cur, new):
        if new["complete"] and len(new["path"]) >= len(cur["path"]):
            st[kind] = new   # (a fuller view of the same path)
        st.pop(kind + "_pending", None)
        return
    pend = st.get(kind + "_pending")
    if pend and pathfinder.same(pend["value"], new):
        pend["n"] += 1
        if new["complete"]:
            pend["value"] = new
    else:
        pend = {"value": new, "n": 1, "first": now}
    st[kind + "_pending"] = pend
    if pend["n"] < CONFIRM:
        return
    st[kind], st[kind + "_since"] = pend["value"], pend["first"]
    st.pop(kind + "_pending")
    if not w["alert"]:
        return
    t, names = res["target"], res["names"]
    what = w["label"] or w["query"]
    after, before = path_text(pend["value"]["path"], names), path_text(cur["path"], names)
    title = (f"Your traffic to {what} (AS{t['asn']}) now goes {after}" if kind == "active" else
             f"Traffic from {what} (AS{t['asn']}) back to you now comes {after}")
    note = (f"Before, it went {before}. Seen in {CONFIRM} checks in a row (every {w['minutes']} minutes)"
            + (", by a traceroute from this server." if kind == "active" else
               f", by RIPE Atlas probes in AS{pend['value'].get('probe_asn') or t['asn']}."))
    svc.engine._new_event("path_change", "info", None, pend["first"], title,
                          {"pathfinder": w["query"], "direction": "to them" if kind == "active" else "back to you",
                           "was": cur["path"], "now": pend["value"]["path"], "note": note}, end_ts=now)


async def check(svc, w):
    """Look the target up, trace it, collect and start the traces back from them: one check of a watch."""
    conn, now, st = svc.aconn, time.time(), w["state"]
    try:
        res = await pathfinder.lookup(svc, w["query"])
    except pathfinder.PathError as ex:
        st.update(error=str(ex), error_at=now)
        _store(conn, w, now)
        return
    key, them = res["key"], res["target"]["asn"]
    known = pathfinder.known_value(res)
    pathfinder.record(conn, key, "known", known, now)
    st.update(error=None, them=them, paths=len(known["paths"]), traced=res["trace_to"],
              shortest=[int(a) for a in known["shortest"].split()] if known["shortest"] else None)
    if res["can_trace"]:
        try:
            tr = await pathfinder.trace(svc, res["trace_to"], res["paths"], them, limit=False)
            act = pathfinder.active_value(tr)
            st["trace_error"] = (None if act else "this server's traffic doesn't leave through your network"
                                 if not tr["from_you"] else "the traceroute's TTL didn't take effect" if tr["direct"]
                                 else "the traceroute saw too little")
            if act:
                pathfinder.record(conn, key, "active", act, now)
                _confirm(svc, w, "active", act, res, now)
        except pathfinder.PathError as ex:
            st["trace_error"] = str(ex)
    if w["back"]:
        await _back(svc, w, res, now)
    _store(conn, w, now)


async def _back(svc, w, res, now):
    st = w["state"]
    msm = st.get("back_msm")
    if msm and now - st.get("back_started", now) > 240:   # (most results are in by then)
        try:
            r = await pathfinder.back_result(svc, msm, res["target"]["asn"], res["paths"])
            val = pathfinder.back_value(r["results"])
            if val:
                pathfinder.record(svc.aconn, res["key"], "back", val, now)
                _confirm(svc, w, "back", val, res, now)
            if r["done"] or val:
                st.pop("back_msm", None)
        except pathfinder.PathError:
            st.pop("back_msm", None)
    if not st.get("back_msm") and now - st.get("back_started", 0) >= max(BACK_MINUTES, w["minutes"]) * 60:
        try:
            info = await pathfinder.back_start(svc, res["target"]["asn"], res["main"], user="watch")
            st.update(back_msm=info["msm"], back_started=now, back_error=None, back_from=info["from_asn"])
        except pathfinder.PathError as ex:
            st.update(back_error=str(ex), back_started=now)


def _store(conn, w, now):
    conn.execute("UPDATE pf_watch SET last_check=?, state=? WHERE id=?", (now, json.dumps(w["state"]), w["id"]))
    conn.commit()


async def loop(svc):
    await asyncio.sleep(120)   # (after the busy first minutes)
    while True:
        try:
            for w in watches(svc.aconn):
                if time.time() - (w["last_check"] or 0) >= w["minutes"] * 60:
                    try:
                        await check(svc, w)
                    except Exception:
                        log.exception("checking watched %s", w["query"])
            if time.time() - _pruned["at"] > 86400:
                pathfinder.prune(svc.aconn)
                _pruned["at"] = time.time()
        except Exception:
            log.exception("watch loop")
        await asyncio.sleep(60)
