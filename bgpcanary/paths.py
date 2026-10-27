"""AS path helpers."""


def normalize(path) -> list:
    """Return a flat list of ints with prepends and AS_SETs collapsed."""
    out = []
    for hop in path or []:
        if isinstance(hop, (list, tuple)):  # AS_SET from RIS Live
            hop = min(hop) if hop else None
        elif isinstance(hop, str):
            hop = hop.strip("{}").split(",")[0]
            if not hop.isdigit():
                continue
        if hop is None:
            continue
        hop = int(hop)
        if not out or out[-1] != hop:
            out.append(hop)
    return out


def parse_str(s: str) -> list:
    return normalize(s.split())


def origin(p):
    return p[-1] if p else None


def upstream(p):
    return p[-2] if len(p) >= 2 else None


def chain(p, depth=3):
    """Last hops before the origin, e.g. '64510 64502 64501' (transit ... upstream)."""
    if len(p) < 2:
        return ""
    return " ".join(str(x) for x in p[-(depth + 1):-1])


def hops(c):
    """A chain string (see chain()) as ints: transit first, your upstream last."""
    return [int(x) for x in c.split()] if c else []


def movers(before, after, floor=3):
    """Who changed how they reach you between two chain mixes ({chain: RIS peers}, as the buckets store them): each
    network on the chains with its next hop toward you before and after. Your upstreams themselves are left out
    (their counts are what the alert reports anyway). [{"asn", "from", "to", "before", "after", "peers"}], biggest
    move first: from != to is a switch of next hop (peers: how many it moved to the new one); from == to a change
    of reach at that hop (before/after: its peers there; peers: the network's whole change). Only moves of at least
    `floor` peers."""
    nxt = {}   # asn -> {next hop toward you: [peers before, peers after]}
    for side, mix in ((0, before), (1, after)):
        for c, n in (mix or {}).items():
            h = hops(c)
            for i, a in enumerate(h[:-1]):   # (the last hop is your upstream: its next hop is you)
                nxt.setdefault(a, {}).setdefault(h[i + 1], [0, 0])[side] += n
    switches, changes = [], []
    for a, d in nxt.items():
        tb, ta = sum(v[0] for v in d.values()), sum(v[1] for v in d.values())
        nb = max((k for k in d if d[k][0]), key=lambda k: d[k][0], default=None)
        na = max((k for k in d if d[k][1]), key=lambda k: d[k][1], default=None)
        if nb is not None and na is not None and nb != na and d[na][1] - d[na][0] >= floor:
            switches.append({"asn": a, "from": nb, "to": na, "before": tb, "after": ta, "peers": d[na][1] - d[na][0]})
        elif abs(ta - tb) >= floor:
            # a change of reach, told at the hop where most of it happened: its busiest hop can differ before and
            # after without anything having moved there (what it lost at one hop simply went away)
            k = max(d, key=lambda k: abs(d[k][1] - d[k][0]))
            changes.append({"asn": a, "from": k, "to": k, "before": d[k][0], "after": d[k][1], "peers": abs(ta - tb)})
    # the switches explain the move; a change of reach of a network they switched from or to is their consequence
    ends = {m["from"] for m in switches} | {m["to"] for m in switches}
    return (sorted(switches, key=lambda m: -m["peers"])
            + sorted((m for m in changes if m["asn"] not in ends), key=lambda m: -m["peers"]))


def mover_text(m, label):
    """One mover (see movers()) in words, `label(asn)` naming networks: 'AS64510 (A) switched from via AS64502 to
    via AS64501 (40 peers)' / 'AS64510 (A) stopped carrying you via AS64502 (94 → 0 peers)'."""
    a, f, t = label(m["asn"]), m["from"], m["to"]
    if f is not None and t is not None and f != t:
        return f"{a} switched from via {label(f)} to via {label(t)} ({m['peers']} peers)"
    if not m["after"]:
        return f"{a} stopped carrying you via {label(f)} ({m['before']} → 0 peers)"
    if not m["before"]:
        return f"{a} started carrying you via {label(t)} (0 → {m['after']} peers)"
    return f"{a} via {label(t)}: {m['before']} → {m['after']} peers"


def collector_name(s: str) -> str:
    """'rrc01.ripe.net' / 'RRC01' / '01' -> 'rrc01'."""
    s = s.lower().split(".")[0]
    if s.isdigit():
        return "rrc%02d" % int(s)
    return s
