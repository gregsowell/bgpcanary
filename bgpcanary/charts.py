"""The event page's charts as pictures, for the evidence PDF (evidence_pdf.py): visibility, the upstream chains and the
update churn around an event. They're drawn from what /api/series gives the page (web.series_payload), as the page
draws them (static/app.js renderCharts), with the events in the window shaded behind them. Times are UTC."""
import io
import math
import time

from PIL import Image, ImageDraw

from .tree import C, font

PALETTE = [(36, 99, 235), (232, 113, 10), (18, 163, 107), (155, 77, 202), (210, 63, 138), (15, 155, 181), (138, 143, 152)]
BANDS = (("info", (59, 111, 214, 26)), ("warning", (199, 119, 0, 41)), ("critical", (200, 38, 43, 41)))   # app.js
S = 2       # drawn at twice the size and scaled down: smooth lines
W = 880     # (a PDF page is 528 points wide inside its margins: 13 px text reads as 7.8 points)
TIME_STEPS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800)


def _step(top, n=5):
    """A round step for an axis from 0 to `top` with about `n` ticks (at least 1: these are counts)."""
    raw = max(top / n, 1)
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag)


class _Plot:
    def __init__(self, d, title, ymax, legend, H=250):
        """`legend`: [(label, colour, dashed)], in rows under the title."""
        self.H, self.t0, self.t1 = H, d["start"], max(d["end"], d["start"] + 1)
        self.img = Image.new("RGB", (W * S, H * S), C["bg"])
        self.dr = ImageDraw.Draw(self.img, "RGBA")
        self.text((6, 6), title, 14, True, C["text"])
        x, y = 6, 28
        for label, col, dash in legend:
            w = 22 + self.tw(label, 11) + 16
            if x + w > W - 6 and x > 6:
                x, y = 6, y + 17
            self.line([(x, y + 7), (x + 16, y + 7)], col, 1.5 if dash else 2.5, (4, 3) if dash else None)
            self.text((x + 22, y), label, 11, color=C["text"])
            x += w
        self.l, self.r, self.t, self.b = 50, W - 14, y + 24, H - 26   # the plot area
        step = _step(ymax or 1)
        self.top = max(step, math.ceil((ymax or 1) / step) * step)
        # the events in the window, shaded by severity (one shade per stretch, as the page does; info events only
        # when they're brief dips)
        spans = {}
        for e in d.get("events") or []:
            if e["severity"] == "info" and e["kind"] != "blip":
                continue
            a, b = max(self.t0, e["start_ts"]), min(self.t1, e["end_ts"] or time.time())
            if b >= self.t0 and a <= self.t1:
                spans.setdefault(e["severity"], []).append((self.x(a), max(self.x(b), self.x(a) + 1.5)))
        for sev, col in BANDS:
            for x0, x1 in spans.get(sev, []):
                self.dr.rectangle([x0 * S, self.t * S, x1 * S, self.b * S], fill=col)
        # the axes and the grid
        v = 0
        while v <= self.top + 1e-9:
            y = self.y(v)
            self.dr.line([(self.l * S, y * S), (self.r * S, y * S)], fill=C["border"], width=1)
            label = f"{v:g}"
            self.text((self.l - 6 - self.tw(label, 11), y - 7), label, 11)
            v += step
        span = self.t1 - self.t0
        ts_step = next((s for s in TIME_STEPS if span / s <= 8), TIME_STEPS[-1])
        fmt = "%m-%d" if span > 3 * 86400 else "%m-%d %H:%M" if span > 86400 else "%H:%M"
        t = math.ceil(self.t0 / ts_step) * ts_step
        while t <= self.t1:
            x = self.x(t)
            self.dr.line([(x * S, self.t * S), (x * S, self.b * S)], fill=C["border"] + (120,), width=1)
            label = time.strftime(fmt, time.gmtime(t))
            self.text((x - self.tw(label, 11) / 2, self.b + 5), label, 11)
            t += ts_step
        self.text((6, self.b + 5), "UTC", 11, color=C["muted"])
        self.dr.line([(self.l * S, self.b * S), (self.r * S, self.b * S)], fill=C["muted"], width=S)

    def x(self, ts):
        return self.l + (ts - self.t0) / (self.t1 - self.t0) * (self.r - self.l)

    def y(self, v):
        return self.b - min(v, self.top) / self.top * (self.b - self.t)

    def tw(self, s, size, bold=False):
        return self.dr.textlength(s, font=font(size * S, bold)) / S

    def text(self, xy, s, size, bold=False, color=None):
        self.dr.text((xy[0] * S, xy[1] * S), s, font=font(size * S, bold), fill=color or C["muted"])

    def line(self, pts, col, w=1.5, dash=None):
        """A line through `pts` (1x coordinates); `dash`: (on, off) lengths."""
        if not dash:
            self.dr.line([(a * S, b * S) for a, b in pts], fill=col, width=max(1, int(w * S)), joint="curve")
            return
        on, off = dash
        for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
            seg = math.hypot(xb - xa, yb - ya)
            p = 0.0
            while p < seg:
                q = min(p + on, seg)
                f0, f1 = p / seg, q / seg
                self.dr.line([((xa + (xb - xa) * f0) * S, (ya + (yb - ya) * f0) * S),
                              ((xa + (xb - xa) * f1) * S, (ya + (yb - ya) * f1) * S)], fill=col, width=max(1, int(w * S)))
                p = q + off

    def steps(self, ts, vals, end):
        """The runs of a stepped line (each value held until the next point), split where a value is missing:
        [[(x, y)...]] in 1x coordinates."""
        runs, cur = [], []
        for i, v in enumerate(vals):
            if v is None:
                if cur:
                    runs.append(cur)
                cur = []
                continue
            x1 = self.x(ts[i + 1] if i + 1 < len(ts) else end)
            cur += [(self.x(ts[i]), self.y(v)), (x1, self.y(v))]
        if cur:
            runs.append(cur)
        return runs

    def png(self):
        im = self.img.resize((W, self.H), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue()


def _end(d):
    return d["ts"][-1] + d.get("step", 30) if d["ts"] else d["end"]


def visibility(d):
    """The RIS peers seeing the prefix, the lowest in each bucket, the baseline and the alert threshold."""
    vals = [v for k in ("visible", "min", "baseline") for v in d[k] if v is not None]
    p = _Plot(d, f"Visibility of {d['prefix']}: RIS peers seeing it", max(vals or [1]) * 1.05,
              [("Peers seeing prefix", PALETTE[0], False), ("Lowest in bucket", C["crit"], True),
               ("Baseline", C["muted"], True), (f"Alert threshold ({d['threshold_pct']:g}%)", C["warn"], True)])
    end = _end(d)
    th = [None if b is None else b * d["threshold_pct"] / 100 for b in d["baseline"]]
    for run in p.steps(d["ts"], d["visible"], end):   # the area under the main line, faintly
        p.dr.polygon([(run[0][0] * S, p.b * S)] + [(a * S, b * S) for a, b in run] + [(run[-1][0] * S, p.b * S)],
                     fill=PALETTE[0] + (30,))
    for key, col, w, dash in (("baseline", C["muted"], 1.2, (6, 4)), ("threshold", C["warn"], 1.2, (3, 3)),
                              ("min", C["crit"], 1.2, (2, 2)), ("visible", PALETTE[0], 1.8, None)):
        for run in p.steps(d["ts"], th if key == "threshold" else d[key], end):
            p.line(run, col, w, dash)
    return p.png()


def chains(d):
    """The RIS peers per upstream chain (stacked), as the page's "Upstream chains"."""
    keys = [k for k in d["chains"] if k != "other" or any(d["chains"][k])]
    n = len(d["ts"])
    totals = [sum((d["chains"][k][i] or 0) for k in keys) for i in range(n)]
    p = _Plot(d, "Upstream chains: RIS peers per chain (stacked)", max(totals or [1]) * 1.05,
              [((d["chain_labels"].get(k) or k)[:40] if k != "other" else "other", PALETTE[j % len(PALETTE)], False)
               for j, k in enumerate(keys)], H=240)
    end, below = _end(d), [0] * n
    for j, k in enumerate(keys):
        col = PALETTE[j % len(PALETTE)]
        top = [None if d["chains"][k][i] is None else below[i] + d["chains"][k][i] for i in range(n)]
        for run_top, run_low in zip(p.steps(d["ts"], top, end), p.steps(d["ts"], [None if t is None else below[i]
                                                                                for i, t in enumerate(top)], end)):
            p.dr.polygon([(a * S, b * S) for a, b in run_top] + [(a * S, b * S) for a, b in reversed(run_low)],
                         fill=col + (90,))
            p.line(run_top, col, 1)
        below = [below[i] + (d["chains"][k][i] or 0) for i in range(n)]
    return p.png()


def churn(d):
    """Announcements and withdrawals per bucket (stacked bars), as the page's "Update churn"."""
    n = len(d["ts"])
    ups = [v or 0 for v in d["updates"]]
    wds = [v or 0 for v in d["withdrawals"]]
    p = _Plot(d, "Update churn: BGP updates per interval", max([u + w for u, w in zip(ups, wds)] or [1]) * 1.05,
              [("Announcements", PALETTE[0], False), ("Withdrawals", C["crit"], False)], H=200)
    end = _end(d)
    for i in range(n):
        x0 = p.x(d["ts"][i])
        x1 = p.x(d["ts"][i + 1] if i + 1 < n else end)
        bw = max(0.8, (x1 - x0) * 0.8)
        for lo, hi, col in ((0, ups[i], PALETTE[0]), (ups[i], ups[i] + wds[i], C["crit"])):
            if hi > lo:
                p.dr.rectangle([x0 * S, p.y(hi) * S, (x0 + bw) * S, p.y(lo) * S], fill=col)
    return p.png()


def all_charts(d):
    """[(title, png)] of the three, for the PDF; [] when there's nothing stored in the window."""
    if not d["ts"]:
        return []
    return [("Visibility around the event", visibility(d)), ("Upstream chains", chains(d)),
            ("Update churn", churn(d))]
