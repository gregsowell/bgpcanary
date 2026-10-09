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


def collector_name(s: str) -> str:
    """'rrc01.ripe.net' / 'RRC01' / '01' -> 'rrc01'."""
    s = s.lower().split(".")[0]
    if s.isdigit():
        return "rrc%02d" % int(s)
    return s
