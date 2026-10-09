"""Minimal async telnet client for Route Views route servers (login rviews)."""
import asyncio
import logging
import re

from . import paths

log = logging.getLogger("bgpcanary.routeviews")

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
PROMPT_RE = re.compile(rb"(?:^|\n)[\w.\-]+[>#] ?$")
LOGIN_RE = re.compile(rb"(?i)(username|login)\s*:\s*$")
PASS_RE = re.compile(rb"(?i)password\s*:\s*$")
PATH_RE = re.compile(r"^  ((?:\d+|\{[\d,]+\})(?: +(?:\d+|\{[\d,]+\}))*)(?:,.*|\s+\(.*)?\s*$")
PING_RE = re.compile(r"Success rate is (\d+) percent \((\d+)/(\d+)\)(?:, round-trip min/avg/max = (\d+)/(\d+)/(\d+) ms)?")
IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
AS_RE = re.compile(r"\[AS (\d+)\]")

_locks: dict[str, asyncio.Lock] = {}
# Route Views rate-limits telnet per source IP (per collector, per day: 100 connections, 500 KB down,
# 100 KB up). Every session reports its usage here so the service can budget it.
on_usage = None   # callable(host, bytes_in, bytes_out)


class RateLimited(Exception):
    """The collector's rate limiter answered instead of the router (HTTP page instead of a login prompt)."""


class Session:
    def __init__(self, host, user="rviews", password="rviews", timeout=30):
        self.host, self.user, self.password, self.timeout = host, user, password, timeout
        self.buf = b""
        self.bytes_in = self.bytes_out = 0

    async def __aenter__(self):
        self.r, self.w = await asyncio.wait_for(asyncio.open_connection(self.host, 23), self.timeout)
        await self._login()
        await self.cmd("terminal length 0")
        return self

    async def __aexit__(self, *a):
        try:
            self.w.write(b"exit\r\n")
            await self.w.drain()
            self.w.close()
        except Exception:
            pass
        if on_usage:
            try:
                on_usage(self.host, self.bytes_in, self.bytes_out + 6)
            except Exception:
                log.exception("usage callback")

    def _negotiate(self, data: bytes) -> bytes:
        out, i, reply = bytearray(), 0, bytearray()
        while i < len(data):
            b = data[i]
            if b == IAC and i + 1 < len(data):
                c = data[i + 1]
                if c in (DO, DONT, WILL, WONT) and i + 2 < len(data):
                    opt = data[i + 2]
                    if c == DO:
                        reply += bytes([IAC, WONT, opt])
                    elif c == WILL:
                        reply += bytes([IAC, DO if opt in (1, 3) else DONT, opt])
                    i += 3
                    continue
                if c == SB:
                    j = data.find(bytes([IAC, SE]), i)
                    i = j + 2 if j >= 0 else len(data)
                    continue
                if c == IAC:
                    out.append(IAC)
                i += 2
                continue
            out.append(b)
            i += 1
        if reply:
            self.w.write(bytes(reply))
        return bytes(out)

    async def _read_until(self, patterns, timeout, on_chunk=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for p in patterns:
                if p.search(self.buf[-200:]):
                    out, self.buf = self.buf, b""
                    return out
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"timeout waiting for prompt from {self.host}")
            chunk = await asyncio.wait_for(self.r.read(4096), remaining)
            if not chunk:
                if b"HTTP/1." in self.buf or b"Rate Limiter" in self.buf or b"request in time" in self.buf:
                    raise RateLimited(f"{self.host}: rate-limited by Route Views (daily telnet limit for this IP)")
                raise ConnectionError(f"{self.host} closed connection")
            self.bytes_in += len(chunk)
            self.buf += self._negotiate(chunk)
            if b"HTTP/1." in self.buf[:64] or b"Rate Limiter" in self.buf:
                raise RateLimited(f"{self.host}: rate-limited by Route Views (daily telnet limit for this IP)")
            if on_chunk:
                await on_chunk(self)

    async def _login(self):
        out = await self._read_until([LOGIN_RE, PASS_RE, PROMPT_RE], self.timeout)
        if LOGIN_RE.search(out[-200:]):
            self.w.write(self.user.encode() + b"\r\n")
            out = await self._read_until([PASS_RE, PROMPT_RE], self.timeout)
        if PASS_RE.search(out[-200:]):
            self.w.write(self.password.encode() + b"\r\n")
            await self._read_until([PROMPT_RE], self.timeout)

    async def cmd(self, command, timeout=60, on_chunk=None):
        self.bytes_out += len(command) + 2
        self.w.write(command.encode() + b"\r\n")
        await self.w.drain()
        out = await self._read_until([PROMPT_RE], timeout, on_chunk)
        text = out.decode(errors="replace").replace("\r", "")
        lines = text.split("\n")
        if lines and command in lines[0]:
            lines = lines[1:]
        if lines and PROMPT_RE.search(lines[-1].encode()):
            lines = lines[:-1]
        return "\n".join(lines).strip("\n")


def lock_for(host):
    return _locks.setdefault(host, asyncio.Lock())


def parse_bgp(text):
    out = []
    for line in text.split("\n"):
        m = PATH_RE.match(line)
        if m:
            out.append(" ".join(str(x) for x in paths.parse_str(m.group(1))))
    return out


def parse_ping(text):
    m = PING_RE.search(text)
    if not m:
        return None, None
    return float(m.group(1)), (float(m.group(5)) if m.group(5) else None)


def parse_trace(text, target):
    last_ip, last_asn, reached = None, None, False
    for line in text.split("\n"):
        if line.startswith("Tracing") or "escape sequence" in line or "VRF info" in line:
            continue
        ips = IP_RE.findall(line)
        if ips:
            paren = re.findall(r"\((\d{1,3}(?:\.\d{1,3}){3})\)", line)
            last_ip = paren[0] if paren else ips[0]
            a = AS_RE.findall(line)
            last_asn = int(a[-1]) if a else last_asn
            if target in ips:
                reached = True
    return last_ip, last_asn, reached


async def show_bgp(host, prefix):
    async with lock_for(host):
        async with Session(host) as s:
            v6 = ":" in prefix
            # only the AS-path lines (two-space indent + digits): ~1 KB instead of ~5-15 KB of per-path detail
            base = f"show bgp ipv6 unicast {prefix}" if v6 else f"show ip bgp {prefix}"
            raw = await s.cmd(f"{base} | include ^  [0-9]", timeout=90)
    return raw, parse_bgp(raw)


async def show_bgp_many(host, prefixes):
    """One session, one compact `show ... | include` per prefix: {prefix: (raw, [paths])}."""
    out = {}
    async with lock_for(host):
        async with Session(host) as s:
            for prefix in prefixes:
                base = f"show bgp ipv6 unicast {prefix}" if ":" in prefix else f"show ip bgp {prefix}"
                raw = await s.cmd(f"{base} | include ^  [0-9]", timeout=90)
                out[prefix] = (raw, parse_bgp(raw))
    return out


async def ping(host, target):
    async with lock_for(host):
        async with Session(host) as s:
            raw = await s.cmd(f"ping {target}", timeout=60)
    return raw, *parse_ping(raw)


async def traceroute(host, target, max_dark_hops=4):
    """Runs traceroute; aborts (Ctrl-^) after `max_dark_hops` consecutive all-'*' hops."""
    state = {"aborted": False}

    async def watch(sess):
        if state["aborted"]:
            return
        text = sess.buf.decode(errors="replace")
        hop_lines = [l for l in text.replace("\r", "").split("\n") if re.match(r"^\s*\d+\s", l)]
        dark = 0
        for l in reversed(hop_lines):
            if re.match(r"^\s*\d+\s+(\*\s*)+$", l):
                dark += 1
            else:
                break
        if dark >= max_dark_hops:
            state["aborted"] = True
            sess.w.write(b"\x1e")
            await sess.w.drain()

    async with lock_for(host):
        async with Session(host) as s:
            raw = await s.cmd(f"traceroute {target}", timeout=300, on_chunk=watch)
    if state["aborted"]:
        raw += f"\n[bgpcanary: aborted after {max_dark_hops} unresponsive hops]"
    return raw, *parse_trace(raw, target)
