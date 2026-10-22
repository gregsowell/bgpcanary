"""Traceroute from this server, without root or a traceroute program (the Path finder page).

Each probe goes out from its own socket with a rising TTL: an ICMP echo request where the system lets ordinary users
open ICMP sockets (net.ipv4.ping_group_range, open to everyone on current distributions), else a UDP datagram to a
high port. The router where the TTL runs out answers "time exceeded", and the kernel puts that on the probe socket's
error queue (IP_RECVERR) with the router's address, as tracepath does. No raw sockets, so no capabilities: it works
in the systemd unit's sandbox and in the container image. Linux only.
"""
import ipaddress
import select
import socket
import struct
import sys
import time

IP_RECVERR = getattr(socket, "IP_RECVERR", 11)
IPV6_RECVERR = getattr(socket, "IPV6_RECVERR", 25)
MSG_ERRQUEUE = getattr(socket, "MSG_ERRQUEUE", 0x2000)
SO_TIMESTAMP = getattr(socket, "SO_TIMESTAMP", 29)   # the kernel's receive time on each answer (also SCM_TIMESTAMP)
TIMEVAL = struct.Struct("@ll")
ORIGIN_ICMP, ORIGIN_ICMP6 = 2, 3     # sock_extended_err.ee_origin
PORT = 33434                         # traceroute's first UDP port
# unreachable codes as traceroute prints them: !N network, !H host, !P protocol, !X administratively prohibited
UNREACH4 = {0: "!N", 1: "!H", 2: "!P", 9: "!X", 10: "!X", 13: "!X"}
UNREACH6 = {0: "!N", 1: "!X", 3: "!H", 5: "!X", 6: "!X"}


def supported():
    return sys.platform.startswith("linux")


def icmp_allowed(v6=False):
    """True if this process may open an unprivileged ICMP socket."""
    try:
        socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_DGRAM,
                      socket.IPPROTO_ICMPV6 if v6 else socket.IPPROTO_ICMP).close()
        return True
    except OSError:
        return False


def _probe(target, v6, icmp, ttl):
    """One probe on its own socket; returns (socket, time sent)."""
    fam = socket.AF_INET6 if v6 else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_DGRAM, (socket.IPPROTO_ICMPV6 if v6 else socket.IPPROTO_ICMP) if icmp else 0)
    try:
        # (answers wait in the queue while the other probes go out: their RTT comes from the kernel's timestamp)
        s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMP, 1)
        if v6:
            s.setsockopt(socket.IPPROTO_IPV6, IPV6_RECVERR, 1)
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, ttl)
        else:
            s.setsockopt(socket.IPPROTO_IP, IP_RECVERR, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        s.setblocking(False)
        if icmp:
            # echo request; the kernel fills in the identifier and checksum on these sockets
            s.connect((target, 0))
            s.send(struct.pack("!BBHHH", 128 if v6 else 8, 0, 0, 0, ttl) + b"bgpcanary path finder")
        else:
            s.connect((target, PORT + ttl - 1))
            s.send(b"bgpcanary path finder")
        return s, time.time()
    except OSError:
        s.close()
        raise


def _stamp(anc):
    """The kernel's receive time from a message's control data, or now."""
    for level, kind, data in anc:
        if level == socket.SOL_SOCKET and kind == SO_TIMESTAMP and len(data) >= TIMEVAL.size:
            sec, usec = TIMEVAL.unpack_from(data)
            return sec + usec / 1e6
    return time.time()


def _answer(s, v6, icmp, target):
    """What came back on a probe's socket: (address, note, reached, when), or None if nothing usable."""
    try:
        _, anc, _, _ = s.recvmsg(512, 1024, MSG_ERRQUEUE)
    except (BlockingIOError, InterruptedError):
        anc = None
    except OSError:
        return None
    if anc is None:   # not an error: the target's echo reply (or, for UDP, an open port answering)
        try:
            _, anc, _, _ = s.recvmsg(512, 1024)
        except OSError:
            return None
        return target, "", True, _stamp(anc)
    when = _stamp(anc)
    for level, kind, data in anc:
        if (level, kind) not in ((socket.IPPROTO_IP, IP_RECVERR), (socket.IPPROTO_IPV6, IPV6_RECVERR)):
            continue
        if len(data) < 16:
            continue
        _, origin, etype, code, _, _, _ = struct.unpack_from("=IBBBBII", data)
        if origin not in (ORIGIN_ICMP, ORIGIN_ICMP6):
            return None   # a local error (no route, message too long): nothing came back from the network
        fam = struct.unpack_from("=H", data, 16)[0] if len(data) >= 18 else 0
        if fam == socket.AF_INET and len(data) >= 24:
            addr = socket.inet_ntop(socket.AF_INET, data[20:24])
        elif fam == socket.AF_INET6 and len(data) >= 40:
            addr = socket.inet_ntop(socket.AF_INET6, data[24:40])
        else:
            addr = None
        if (etype, v6) in ((11, False), (3, True)):   # time exceeded: a router on the way
            return addr, "", False, when
        if (etype, v6) in ((3, False), (1, True)):    # unreachable: the end of the road
            if (code, v6) in ((3, False), (4, True)):   # port unreachable: the target itself (UDP probes)
                return addr, "", True, when
            return addr, (UNREACH6 if v6 else UNREACH4).get(code, f"!<{code}>"), True, when
        return addr, "", False, when
    return None


def _round(target, v6, icmp, ttls, wait):
    """Send one probe per TTL at once; {ttl: (address, rtt ms, note, reached)} for those that were answered."""
    socks, got = {}, {}
    try:
        for ttl in ttls:
            s, sent = _probe(target, v6, icmp, ttl)
            socks[s.fileno()] = (s, ttl, sent)
            time.sleep(0.002)   # (not all in the same millisecond, for routers that rate-limit their answers)
        poll, pending = select.poll(), set(socks)
        for fd in socks:
            poll.register(fd, select.POLLIN | select.POLLERR)
        deadline = time.monotonic() + wait
        while pending:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            for fd, _ in poll.poll(left * 1000):
                s, ttl, sent = socks[fd]
                a = _answer(s, v6, icmp, target)
                if a:
                    got[ttl] = (a[0], round(max(a[3] - sent, 0) * 1000, 1), a[1], a[2])
                poll.unregister(fd)
                pending.discard(fd)
    finally:
        for s, _, _ in socks.values():
            s.close()
    return got


def run(target, max_hops=30, wait=2.0, rounds=2, icmp=None):
    """Trace the route to `target` (an IP address): {"mode": "icmp" | "udp", "reached": bool, "direct": bool,
    "hops": [{"ttl", "ip", "rtt", "note"}]}, a silent hop with ip None. "direct": the first probe already reached
    the target, so the TTL didn't take effect on the way out (networking that sends packets on itself instead of
    forwarding them, as some container and VPN setups do). Raises OSError when no probe can be sent at all, e.g. no
    route for that address family."""
    v6 = ipaddress.ip_address(target).version == 6
    # ICMP where allowed; UDP if it isn't, or if ICMP got no answer at all (some firewalls drop it but pass UDP)
    for mode in [icmp] if icmp is not None else [True, False] if icmp_allowed(v6) else [False]:
        hops, reached = {}, None
        for r in range(rounds):
            if r == 0:
                todo = list(range(1, max_hops + 1))
            else:   # again for the silent ones, up to the target or just past the last router that answered
                top = reached - 1 if reached else min(max_hops, max(hops, default=0) + 2)
                todo = [t for t in range(1, top + 1) if t not in hops]
            if not todo:
                break
            for ttl, ans in _round(target, v6, mode, todo, wait).items():
                hops[ttl] = ans
                if ans[3]:
                    reached = min(reached or ttl, ttl)
        if hops:
            break
    last = reached or min(max_hops, max(hops, default=0) + 2)
    out = []
    for t in range(1, last + 1):
        ip, rtt, note, _ = hops.get(t, (None, None, "", False))
        out.append({"ttl": t, "ip": ip, "rtt": rtt, "note": note})
    at_target = bool(reached) and hops[reached][0] == target
    return {"mode": "icmp" if mode else "udp", "reached": at_target, "direct": at_target and reached == 1,
            "hops": out}
