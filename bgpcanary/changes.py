"""What changed in the stored paths toward a prefix: each RIS peer's changes of path, the bursts they come in
and the words for them. The BGP data page's Changes tab and an event's "around this event" lines share these."""
from . import db, paths

LIVE_SOURCES = ("ris-live", "bgplay")   # updates that are news; every other source only corrects the stored state


def chain_key(p):
    """The way in of a path toward you: (transit, upstream); (None, upstream) when the RIS peer is your upstream
    itself; None for a path without one."""
    if len(p) >= 3:
        return (p[-3], p[-2])
    return (None, p[-2]) if len(p) == 2 else None


def chain_text(c, nm):
    """'AS64510 TRANSIT → AS64501 UPSTREAM' for a chain_key, with the first word of each name."""
    if not c:
        return "no route"
    lbl = lambda a: f"AS{a}" + (f" {nm[a].split(' ')[0]}" if nm.get(a) else "")   # noqa: E731
    return (f"{lbl(c[0])} → " if c[0] else "") + lbl(c[1])


def path_changes(conn, prefix, start, end, bookkeeping=False, state=None):
    """Every change of a RIS peer's path toward `prefix` between `start` and `end`, oldest first, judged against the
    stored state at `start`: [{"ts", "collector", "peer", "peer_asn", "old", "new", "source", "live"}], old / new the
    paths as ints (None: no route). A re-announcement of the same path is no change. `bookkeeping`: also the rows
    that only correct the stored state (the looking glass's resync, seed rows after a restart, stale withdrawals);
    they count toward the state either way. `state`: db.state_at's rows for `start`, when the caller has them
    (reading them goes through the prefix's whole history)."""
    last = {}
    for r in (db.state_at(conn, prefix, start) if state is None else state):
        last[(r["collector"], r["peer"])] = (paths.parse_str(r["path"]) if r["kind"] == "A" and r["path"] else None,
                                             r["peer_asn"])
    out = []
    for r in conn.execute("SELECT ts, collector, peer, peer_asn, kind, path, source FROM updates WHERE prefix=? AND ts>? "
                          "AND ts<=? ORDER BY ts, id", (prefix, start, end)):
        k = (r["collector"], r["peer"])
        new = paths.parse_str(r["path"]) if r["kind"] == "A" and r["path"] else None
        old, pa = last.get(k, (None, None))
        last[k] = (new, r["peer_asn"] or pa)
        if old == new:
            continue
        live = r["source"] in LIVE_SOURCES
        if live or bookkeeping:
            out.append({"ts": r["ts"], "collector": r["collector"], "peer": r["peer"], "peer_asn": r["peer_asn"] or pa,
                        "old": old, "new": new, "source": r["source"], "live": live})
    return out


def bursts_of(changes, gap=120):
    """Changes grouped into bursts (changes within `gap` seconds of each other), newest first: [{"start", "end",
    "peers", "changes", "moves": [((old chain, new chain), n)], biggest first}]."""
    out, cur = [], None
    for c in changes:
        if cur is None or c["ts"] - cur["end"] > gap:
            cur = {"start": c["ts"], "end": c["ts"], "changes": []}
            out.append(cur)
        cur["end"] = c["ts"]
        cur["changes"].append(c)
    for b in out:
        b["peers"] = len({(c["collector"], c["peer"]) for c in b["changes"]})
        moves = {}
        for c in b["changes"]:
            k = (chain_key(c["old"]) if c["old"] else None, chain_key(c["new"]) if c["new"] else None)
            moves[k] = moves.get(k, 0) + 1
        b["moves"] = sorted(moves.items(), key=lambda x: -x[1])
    out.reverse()
    return out


def burst_text(b, nm):
    """'152 moved from AS64510 A → AS64501 B to AS64520 C → AS64502 D; 34 withdrawn (were ...)'."""
    parts = []
    for (oc, nc), n in b["moves"][:3]:
        if oc == nc:
            parts.append(f"{n} changed further out (same way in)")
        elif nc is None:
            parts.append(f"{n} withdrawn (were {chain_text(oc, nm)})")
        elif oc is None:
            parts.append(f"{n} announced via {chain_text(nc, nm)}")
        else:
            parts.append(f"{n} moved from {chain_text(oc, nm)} to {chain_text(nc, nm)}")
    rest = sum(n for _, n in b["moves"][3:])
    return "; ".join(parts) + (f"; {rest} more" if rest else "")
