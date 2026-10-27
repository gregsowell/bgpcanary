"""Sanity checks on RIPE's data, so a problem with RIPE RIS isn't reported as a problem with your network.

- An outage seen in RIS is checked against Route Views (an independent set of route collectors) and RIPE Atlas
  before it's alerted as critical: judge() gives the verdict, Service._confirm_outage applies it.
- Control prefixes (cfg.control_prefixes, see Engine.control_status): other networks' prefixes, watched the same
  way. RIS losing most of them at once means RIPE's data is at fault.
- SourceHealth sums up how RIS Live, RIPE's looking glass, the control prefixes, Route Views and Atlas look, for
  the data-sources panel and the banner, and decides when the looking glass is trusted to withdraw routes.
"""
import ipaddress
import json
import sqlite3
import time
from collections import deque

RV_SEES, RV_LOST = 0.6, 0.4   # Route Views still has this share of its usual paths: not an outage / it lost them too
RV_MIN_USUAL = 5              # with fewer paths than this normally, Route Views can't tell either way
RV_USUAL_DAYS = 7             # a collector's "usual" view: its last good snapshot within this long before the outage
LG_STALE_S = 90 * 60          # RIPE's looking glass this far behind: unreliable
LG_DOUBT_S = 30 * 60          # after an incomplete looking-glass answer, it withdraws nothing for this long
RIS_FLAPS = 6                 # RIS Live disconnects in an hour that count as unstable


def _origin_paths(paths_json, asn):
    return sum(1 for p in json.loads(paths_json or "[]") if p.split() and p.split()[-1] == str(asn))


def route_views_view(conn, prefix, asn, eid, start):
    """What Route Views showed for `prefix` in the snapshots taken for event `eid`, per collector, next to that
    collector's usual view (its last good snapshot before `start`). Counts only paths from your ASN. None if no
    snapshot was taken."""
    rows = conn.execute("SELECT server, ts, paths, error, raw FROM rv_snapshots WHERE event_id=? AND prefix=? "
                        "ORDER BY ts", (eid, prefix)).fetchall()
    if not rows:
        return None
    latest = {r["server"]: r for r in rows}   # (the newest per collector)
    servers = []
    for srv, r in sorted(latest.items()):
        if r["error"]:
            servers.append({"server": srv, "error": r["error"][:120]})
            continue
        # (a routine snapshot if there is one: those taken for events show unusual moments)
        ref = conn.execute("SELECT paths FROM rv_snapshots WHERE server=? AND prefix=? AND ts<? AND ts>? AND "
                           "(error IS NULL OR error='') AND raw!='' ORDER BY event_id IS NULL DESC, ts DESC LIMIT 1",
                           (srv, prefix, start, start - RV_USUAL_DAYS * 86400)).fetchone()
        servers.append({"server": srv, "paths": _origin_paths(r["paths"], asn),
                        "usual": _origin_paths(ref["paths"], asn) if ref else None})
    answered = [s for s in servers if "error" not in s]
    with_usual = [s for s in answered if s["usual"]]
    now_paths, usual = sum(s["paths"] for s in with_usual), sum(s["usual"] for s in with_usual)
    if with_usual:
        share = now_paths / usual
    elif answered:   # no usual view to compare with: the share of collectors that see it at all
        share = sum(1 for s in answered if s["paths"]) / len(answered)
    else:
        share = None
    verdict = None if share is None else "sees" if share >= RV_SEES else "lost" if share <= RV_LOST else "partial"
    return {"verdict": verdict, "share": share, "asked": len(servers), "answered": len(answered),
            "paths": sum(s["paths"] for s in answered), "usual": usual if with_usual else None, "servers": servers}


def atlas_view(conn, targets, since):
    """The newest complete round of RIPE Atlas pings (baseline or check) to one of `targets` since `since`:
    {"target", "probes", "replied", "share", "ts"}, or None if there's none yet (or no Atlas)."""
    if not targets:
        return None
    try:
        rows = conn.execute(
            f"SELECT r.msm_id, r.ts, r.sent, r.rcvd, m.target FROM atlas_result r JOIN atlas_msm m ON m.id=r.msm_id "
            f"WHERE r.kind='ping' AND m.purpose IN ('baseline','check') AND r.ts>=? "
            f"AND m.target IN ({','.join('?' * len(targets))}) ORDER BY r.msm_id, r.ts", (since, *targets)).fetchall()
    except sqlite3.OperationalError:
        return None
    rounds, cur = [], None
    for r in rows:   # a round = one measurement's results with no gap longer than 2 minutes (as atlas.check_loss)
        if not cur or cur[-1]["msm_id"] != r["msm_id"] or r["ts"] - cur[-1]["ts"] > 120:
            cur = []
            rounds.append(cur)
        cur.append(r)
    done = [rs for rs in rounds if len(rs) >= 10 and rs[-1]["ts"] < time.time() - 120]
    if not done:
        return None
    rs = max(done, key=lambda x: x[-1]["ts"])
    replied = sum(1 for r in rs if r["rcvd"])
    return {"target": rs[0]["target"], "probes": len(rs), "replied": replied, "share": replied / len(rs),
            "ts": rs[-1]["ts"]}


def targets_in(prefix, targets):
    net = ipaddress.ip_network(prefix, strict=False)
    out = []
    for t in targets:
        try:
            if ipaddress.ip_address(t) in net:
                out.append(t)
        except ValueError:
            pass
    return out


def judge(trouble, rv, atlas):
    """(verdict, why) for an outage seen in RIS. `trouble`: Engine.ris_trouble(); `rv`: route_views_view();
    `atlas`: atlas_view(). Route Views is asked first: it shows routes, as RIS does, from collectors of its own.
    Atlas (whether packets get through) counts when Route Views couldn't say."""
    if trouble:
        return "contradicted", ("RIS lost sight of the control prefixes too ("
                                + ", ".join(c["prefix"] for c in trouble) + ")")
    # a handful of paths normally: a few collectors' view, which can keep (or lose) your route for reasons of
    # their own, so it decides nothing either way (Atlas may)
    weak = bool(rv) and rv.get("usual") is not None and rv["usual"] < RV_MIN_USUAL
    if rv and not weak and rv["verdict"] == "sees":
        return "contradicted", (f"Route Views still sees it ({rv['paths']} paths at {rv['answered']} collectors"
                                + (f", usually {rv['usual']}" if rv["usual"] else "") + ")")
    if rv and not weak and rv["verdict"] == "lost":
        return "confirmed", (f"Route Views lost it too ({rv['paths']} paths at {rv['answered']} collectors"
                             + (f", usually {rv['usual']}" if rv["usual"] else "") + ")")
    if atlas and atlas["share"] >= 0.5:
        return "contradicted", (f"{atlas['replied']} of {atlas['probes']} RIPE Atlas probes still reach "
                                f"{atlas['target']}")
    if atlas:
        return "confirmed", f"only {atlas['replied']} of {atlas['probes']} RIPE Atlas probes reach {atlas['target']}"
    if weak:
        return "unknown", (f"Route Views normally holds only {rv['usual']} path{'s' if rv['usual'] != 1 else ''} for "
                           f"it ({rv['paths']} now), too few to tell")
    if rv and rv["verdict"] == "partial":
        return "unknown", (f"Route Views sees it partly ({rv['paths']} paths at {rv['answered']} collectors"
                           + (f", usually {rv['usual']}" if rv["usual"] else "") + ")")
    if rv and rv["asked"]:
        return "unknown", f"none of the {rv['asked']} Route Views collectors answered"
    return "unknown", "Route Views couldn't be asked (none configured, or their daily limit is used up)"


class SourceHealth:
    """How the data sources look right now: state() for the panel and banner (cached for 20 s), lg_trusted() for
    the looking-glass resync."""

    def __init__(self, svc):
        self.svc = svc
        self.disconnects = deque()   # RIS Live disconnects in the last hour
        self._cache = (0, None)

    def ris_disconnected(self):
        self.disconnects.append(time.time())
        self._cache = (0, None)

    def lg_trusted(self):
        """Whether the looking-glass resync may withdraw routes: not while RIS has lost the control prefixes, nor
        for a while after the looking glass gave an incomplete answer."""
        eng, now = self.svc.engine, time.time()
        recent = any(s.get("last_incomplete") and now - s["last_incomplete"] < LG_DOUBT_S
                     for s in eng.lg_stats.values())
        return not recent and not eng.ris_trouble()

    def state(self):
        now = time.time()
        if now - self._cache[0] < 20 and self._cache[1] is not None:
            return self._cache[1]
        svc, eng = self.svc, self.svc.engine
        while self.disconnects and self.disconnects[0] < now - 3600:
            self.disconnects.popleft()
        # reasons: RIS-based alerts can't be trusted (the banner; alerts say so). notes: worth knowing, no more
        reasons, notes = [], []
        # RIS Live
        connected = bool(eng.status.get("ws_connected"))
        down_since = getattr(svc, "ws_down_since", None)
        ris = {"connected": connected, "drops": len(self.disconnects), "last_msg": eng.status.get("last_msg"),
               "down_for": (now - down_since) if not connected and down_since else None}
        if ris["down_for"] and ris["down_for"] > 180:
            reasons.append(f"RIS Live has been disconnected for {ris['down_for'] / 60:.0f} minutes")
        elif len(self.disconnects) >= RIS_FLAPS:
            reasons.append(f"RIS Live dropped {len(self.disconnects)} times in the last hour")
        # RIPE's looking glass
        lg = []
        for p, st in sorted(eng.lg_stats.items()):
            lg.append({"prefix": p, "behind": max(0, st["asked"] - st["latest"]), "listed": st["listed"],
                       "held": st["held"], "at": st["at"], "last_incomplete": st.get("last_incomplete"),
                       "control": p in eng.controls})
        behind = max((x["behind"] for x in lg), default=0)
        if behind > LG_STALE_S:   # (it can't withdraw anything newer than its data, so it does no harm)
            notes.append(f"RIPE's looking glass is {behind / 3600:.1f} hours behind, so its cross-check of the "
                         f"live data catches less until it catches up")
        bad = [x for x in lg if x["last_incomplete"] and now - x["last_incomplete"] < LG_DOUBT_S]
        if bad:
            reasons.append("RIPE's looking glass gave incomplete answers (ignored) in the last "
                           f"{LG_DOUBT_S // 60} minutes")
        # control prefixes
        controls = eng.control_status()
        trouble = eng.ris_trouble()
        if trouble:
            reasons.append("RIS lost sight of the control prefixes too: "
                           + ", ".join(f"{c['prefix']} ({c['visible']} of ~{c['baseline']:.0f} peers)"
                                       for c in trouble))
        # Route Views and Atlas: the latest answer from each collector in the last day
        rv = []
        try:
            for r in svc.aconn.execute(
                    "SELECT s.server, s.ts, s.error FROM rv_snapshots s JOIN (SELECT server, MAX(ts) ts FROM "
                    "rv_snapshots WHERE ts>? GROUP BY server) l ON l.server=s.server AND l.ts=s.ts ORDER BY s.server",
                    (now - 86400,)):
                rv.append({"server": r["server"], "ts": r["ts"], "ok": not r["error"], "error": (r["error"] or "")[:100]})
        except sqlite3.OperationalError:
            pass
        blocked = [s for s in svc.cfg.rv_servers if not svc.rv_allowed(s, "event")[0]]
        state = {"ok": not reasons, "reasons": reasons, "notes": notes, "ris_live": ris, "lg": lg, "controls": controls,
                 "route_views": {"servers": rv, "configured": len(svc.cfg.rv_servers), "blocked": blocked},
                 "confirm_outages": svc.cfg.confirm_outages, "at": now}
        self._cache = (now, state)
        return state
