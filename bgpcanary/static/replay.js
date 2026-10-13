// Path-tree replay: your AS at the root, branching out along the AS paths seen by RIS peers.
// A slider scrubs through a time window (an event, or the dashboard's selected range);
// branches are colored against a comparison point (the healthiest moment before an event,
// or the start of the window).
(function () {
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const pad2 = n => String(n).padStart(2, "0");
  const tzName = (() => {
    try { return new Date().toLocaleTimeString("en-US", { timeZoneName: "short" }).split(" ").pop(); } catch (e) { return ""; }
  })();
  const fmt = ts => {
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())} ${tzName}`;
  };
  const fmtAxis = (ts, span) => {
    const d = new Date(ts * 1000), hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    return span > 2 * 86400 ? `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hm}` : hm;
  };
  const ago = s => {
    s = Math.max(0, Math.round(s));
    if (s < 90) return "now";
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return (d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`) + " ago";
  };

  // one color per prefix when several are compared in one tree
  const PCOL = ["#2463eb", "#e8710a", "#12a36b", "#9b4dca", "#d23f8a", "#0f9bb5"];

  function theme() {
    return {
      bg: css("--panel") || "#fff", text: css("--text") || "#111", muted: css("--muted") || "#666",
      border: css("--border") || "#ddd", accent: css("--accent") || "#2463eb", crit: css("--crit") || "#c8262b",
      ok: css("--ok") || "#1a8a4a", warn: css("--warn") || "#c77700", codebg: css("--code-bg") || "#f0f2f5",
    };
  }

  class Replay {
    constructor(data, baseMode) {
      this.D = data;
      this.ev = data.event || null;
      this.events = data.events || (data.event ? [data.event] : []);
      this.paths = data.paths.map(s => s.split(" ").map(Number));
      this.rev = this.paths.map(p => p.slice().reverse()); // origin first
      this.U = data.updates;
      this.cur = Int32Array.from(data.init);
      this.ptr = 0;
      this.t = data.start;
      // several prefixes in one tree: each peer entry belongs to one prefix
      this.pp = data.peer_prefix ? Int32Array.from(data.peer_prefix) : null;
      this.np = data.prefixes ? data.prefixes.length : 1;
      if (this.pp) {
        this.pvis = this._prefixSeries();
        this.ptot = this.pvis.map(s => Math.max(1, ...s.map(v => v[1])));
      }
      this.visSeries = this._visibilitySeries();
      this.total0 = Math.max(1, ...this.visSeries.map(v => v[1]));
      this.setBase(baseMode);
    }
    originOk(j) { return j >= 0 && this.paths[j][this.paths[j].length - 1] === this.D.asn; }
    _visibleNow(state) { let n = 0; for (const j of state) if (this.originOk(j)) n++; return n; }
    _visibilitySeries() {
      const st = Int32Array.from(this.D.init);
      let n = this._visibleNow(st);
      const out = [[this.D.start, n]];
      for (const [ts, i, j] of this.U) {
        const was = this.originOk(st[i]), now = this.originOk(j);
        st[i] = j;
        if (was !== now) { n += now ? 1 : -1; out.push([ts, n]); }
      }
      out.push([this.D.end, n]);
      return out;
    }
    _prefixSeries() {
      const st = Int32Array.from(this.D.init), n = new Array(this.np).fill(0);
      st.forEach((j, i) => { if (this.originOk(j)) n[this.pp[i]]++; });
      const out = n.map(v => [[this.D.start, v]]);
      for (const [ts, i, j] of this.U) {
        const was = this.originOk(st[i]), now = this.originOk(j);
        st[i] = j;
        if (was !== now) { const p = this.pp[i]; n[p] += now ? 1 : -1; out[p].push([ts, n[p]]); }
      }
      out.forEach((s, p) => s.push([this.D.end, n[p]]));
      return out;
    }
    visibleP(p) { let n = 0; this.cur.forEach((j, i) => { if (this.pp[i] === p && this.originOk(j)) n++; }); return n; }
    // "best": healthiest moment before the event (or in the window); "start": window start
    setBase(mode) {
      this.baseMode = mode;
      const keep = this.t;
      if (mode === "start") {
        this.baseT = this.D.start;
      } else {
        const limit = this.ev ? this.ev.start : this.D.end;
        let best = this.visSeries[0];
        for (const v of this.visSeries) if (v[0] <= limit && v[1] >= best[1]) best = v;
        this.baseT = best[0];
      }
      this.seek(this.D.start); this.seek(this.baseT);
      this.baseState = Int32Array.from(this.cur);
      this.seek(this.D.start); this.seek(keep);
      if (this.dchains) this.base = this.countsFor(this.baseState, this.dchains);
    }
    seek(t) {
      if (t < this.t) { this.cur = Int32Array.from(this.D.init); this.ptr = 0; }
      while (this.ptr < this.U.length && this.U[this.ptr][0] <= t) {
        const [, i, j] = this.U[this.ptr++];
        this.cur[i] = j;
      }
      this.t = t;
    }
    visible() { return this._visibleNow(this.cur); }

    // ---- tree structure -------------------------------------------------------------
    build(depth, topN) {
      this.depth = depth;
      const key = (r, l) => r.slice(0, l + 1).join(">");
      this.chains = this.rev.map(r => {
        const c = [];
        for (let l = 0; l <= Math.min(depth, r.length - 1); l++) c.push(key(r, l));
        return c;
      });
      // rank nodes by the max number of peers they carried at any point in the window
      const maxC = new Map();
      const keep = this.t;
      const S = 160;
      this.seek(this.D.start);
      for (let k = 0; k <= S; k++) {
        this.seek(this.D.start + (this.D.end - this.D.start) * k / S);
        for (const [n, c] of this.countsFor(this.cur, this.chains)) if (c > (maxC.get(n) || 0)) maxC.set(n, c);
      }
      for (const [n, c] of this.countsFor(this.baseState, this.chains)) if (c > (maxC.get(n) || 0)) maxC.set(n, c);
      this.seek(this.D.start); this.seek(keep);
      const kids = new Map(), all = new Set();
      const addEdge = (p, c) => { all.add(p); all.add(c); if (!kids.has(p)) kids.set(p, new Set()); kids.get(p).add(c); };
      for (const c of this.chains) for (let l = 0; l < c.length; l++) {
        all.add(c[l]);
        if (l > 0) addEdge(c[l - 1], c[l]);
      }
      // Always show every direct upstream, and every neighbour (transit/peer) of each upstream from the
      // global table - including the ones that carry none of our paths, so gaps are visible.
      const pinned = new Map();
      const rootKey = String(this.D.asn);
      if (depth >= 1) {
        for (const u of this.D.upstreams || []) {
          const uk = `${rootKey}>${u}`;
          addEdge(rootKey, uk);
          pinned.set(uk, { upstream: true });
          if (depth < 2) continue;
          for (const n of (this.D.upstream_neighbors || {})[u] || []) {
            const nk = `${uk}>${n.asn}`;
            addEdge(uk, nk);
            pinned.set(nk, { power: n.power, of: u });
          }
        }
      }
      this.pinned = pinned;
      this.maxC = maxC;
      const pw = k => (pinned.get(k) || {}).power || 0;
      // collapse: keep topN children (pinned ones always), fold the rest into "others"
      const disp = new Map(), shown = new Map();
      const roots = [...all].filter(k => !k.includes(">"));
      const visit = (k, level, parent) => {
        shown.set(k, { key: k, level, asn: Number(k.split(">").pop()), parent, children: [], max: maxC.get(k) || 0 });
        disp.set(k, k);
        if (parent) shown.get(parent).children.push(k);
        const ch = [...(kids.get(k) || [])].sort((a, b) => ((maxC.get(b) || 0) - (maxC.get(a) || 0)) || (pw(b) - pw(a)));
        const top = [], rest = [];
        let free = 0;
        for (const c of ch) {
          if (pinned.has(c)) top.push(c);
          else if (free++ < topN) top.push(c);
          else rest.push(c);
        }
        for (const c of top) visit(c, level + 1, k);
        if (rest.length) {
          const ok = k + ">*";
          shown.set(ok, { key: ok, level: level + 1, asn: null, parent: k, children: [], others: rest.length,
                          max: rest.reduce((s, c) => s + (maxC.get(c) || 0), 0) });
          shown.get(k).children.push(ok);
          const mark = n => { disp.set(n, ok); for (const c of kids.get(n) || []) mark(c); };
          rest.forEach(mark);
        }
      };
      roots.sort((a, b) => (maxC.get(b) || 0) - (maxC.get(a) || 0)).forEach(r => visit(r, 0, null));
      this.shown = shown;
      this.roots = roots;
      this.dchains = this.chains.map(c => {
        const out = [];
        for (const k of c) { const d = disp.get(k); if (!out.length || out[out.length - 1] !== d) out.push(d); if (d.endsWith("*")) break; }
        return out;
      });
      this.base = this.countsFor(this.baseState, this.dchains);
      this.layout();
    }
    countsFor(state, chains) {
      // per node: total peers, plus "node|prefix" counts when several prefixes share the tree
      const m = new Map();
      for (let i = 0; i < state.length; i++) {
        const j = state[i];
        if (j < 0) continue;
        for (const k of chains[j]) {
          m.set(k, (m.get(k) || 0) + 1);
          if (this.pp) { const kp = k + "|" + this.pp[i]; m.set(kp, (m.get(kp) || 0) + 1); }
        }
      }
      return m;
    }
    layout() {
      this.rows = 0;
      const place = k => {
        const n = this.shown.get(k);
        if (!n.children.length) { n.row = this.rows++; return; }
        n.children.forEach(place);
        n.row = (this.shown.get(n.children[0]).row + this.shown.get(n.children[n.children.length - 1]).row) / 2;
      };
      this.roots.forEach(place);
    }

    // ---- drawing ---------------------------------------------------------------------
    size(W) {
      const rowH = this.rowH || 20;   // "Fit to screen" packs rows tighter
      return { W, H: 86 + Math.max(this.rows, 3) * rowH + 24 + 70, rowH };
    }
    phase() {
      const ev = this.ev;
      if (ev) {
        if (this.t < ev.start) return "before event";
        if (!ev.end || this.t <= ev.end) return "during event";
        return "after recovery";
      }
      return ago(Date.now() / 1000 - this.t);
    }
    draw(ctx, W, opts = {}) {
      const th = theme();
      const { H, rowH } = this.size(W);
      const counts = this.countsFor(this.cur, this.dchains);
      const total = Math.max(this.total0, 1);
      const vis = this.visible();
      ctx.save();
      ctx.fillStyle = th.bg; ctx.fillRect(0, 0, W, H);
      ctx.textBaseline = "middle";
      // header
      ctx.fillStyle = th.text; ctx.font = "600 15px system-ui, sans-serif";
      ctx.fillText(this.pp ? `AS${this.D.asn} · ${this.np} prefixes` : `AS${this.D.asn} · ${this.D.prefix}`, 16, 20);
      ctx.font = "13px system-ui, sans-serif"; ctx.fillStyle = th.muted;
      ctx.fillText(fmt(this.t), 16, 42);
      if (this.pp) {
        // one chip per prefix: color, prefix, how many RIS peers see it at the selected time
        let x = 230;
        this.D.prefixes.forEach((p, i) => {
          const v = this.visibleP(i), pc = Math.round(100 * v / this.ptot[i]);
          ctx.fillStyle = PCOL[i % PCOL.length]; ctx.fillRect(x, 36, 12, 12);
          ctx.font = "600 13px system-ui, sans-serif"; ctx.fillStyle = th.text;
          const a = `${p}`; ctx.fillText(a, x + 17, 42); x += 17 + ctx.measureText(a).width + 6;
          ctx.font = "13px system-ui, sans-serif"; ctx.fillStyle = pc < 50 ? th.crit : pc < 80 ? th.warn : th.muted;
          const b = `${v}/${this.ptot[i]}`; ctx.fillText(b, x, 42); x += ctx.measureText(b).width + 22;
        });
      }
      const pct = Math.round(100 * vis / total);
      if (!this.pp) {
        ctx.fillStyle = pct < 50 ? th.crit : pct < 80 ? th.warn : th.ok;
        ctx.font = "600 14px system-ui, sans-serif";
        const vt = `${vis}/${this.total0} RIS peers (${pct}%)`;
        ctx.fillText(vt, 230, 42);
        ctx.fillStyle = th.muted; ctx.font = "13px system-ui, sans-serif";
        const cmp = this.baseMode === "start" ? `colors vs ${fmtAxis(this.baseT, this.D.end - this.D.start)}` : "colors vs healthiest point";
        ctx.fillText(`${this.phase()} · ${cmp}`, 230 + ctx.measureText(vt).width + 40, 42);
      }
      // legend
      const lg = this.pp
        ? [["one strand per prefix", th.accent, []], ["prefix missing on this branch", th.warn, [4, 3]], ["not carrying you", th.crit, [2, 3]], ["idle", th.border, []]]
        : [["carrying routes", th.accent, []], ["lost >50%", th.warn, []], ["dropped", th.crit, [5, 4]], ["new", th.ok, []], ["not carrying you", th.crit, [2, 3]], ["idle", th.border, []]];
      let lx = W - 16;
      ctx.font = "12px system-ui, sans-serif";
      for (let i = lg.length - 1; i >= 0; i--) {
        const [label, col, dash] = lg[i];
        const w = ctx.measureText(label).width;
        lx -= w; ctx.fillStyle = th.muted; ctx.fillText(label, lx, 20);
        lx -= 26; ctx.strokeStyle = col; ctx.lineWidth = 3; ctx.setLineDash(dash);
        ctx.beginPath(); ctx.moveTo(lx, 20); ctx.lineTo(lx + 20, 20); ctx.stroke(); ctx.setLineDash([]);
        lx -= 16;
      }
      // tree geometry
      const top = 86, labelW = 240, left = 24;
      const colW = (W - left - labelW) / Math.max(this.depth, 1);
      const pos = k => { const n = this.shown.get(k); return [left + n.level * colW, top + n.row * rowH + rowH / 2]; };
      // column headings, so it's clear which layer is which (e.g. vs bgp.tools' upstream list)
      const COLS = ["You", "Your upstreams", "Their transits & peers", "Next hop out", "Further out", "Further out", "Further out"];
      ctx.font = "600 11px system-ui, sans-serif"; ctx.fillStyle = th.muted;
      for (let l = 0; l <= this.depth; l++) ctx.fillText(COLS[l].toUpperCase(), left + l * colW - (l ? 4 : 0), 68);
      ctx.strokeStyle = th.border; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(16, 77); ctx.lineTo(W - 16, 77); ctx.stroke();
      const stateOf = k => {
        const c = counts.get(k) || 0, b = this.base.get(k) || 0;
        if (c > 0 && b > 0) return c < b * 0.5 ? ["degraded", th.warn] : ["live", th.accent];
        if (c === 0 && b > 0) return ["lost", th.crit];
        if (c > 0 && b === 0) return ["new", th.ok];
        // an upstream / upstream-neighbour carrying none of our paths now (and none at the comparison point)
        if (this.pinned && this.pinned.has(k)) return ["never", th.crit];
        return ["idle", th.border];
      };
      const wOf = c => 1 + 8 * Math.sqrt(c / total);
      // compare mode: a branch is "partial" when it carries some of the prefixes but not all of them
      const pc = (k, p) => counts.get(k + "|" + p) || 0;
      const partial = k => { const c = counts.get(k) || 0; if (!this.pp || !c) return false;
                             for (let p = 0; p < this.np; p++) if (!pc(k, p)) return true; return false; };
      for (const [k, n] of this.shown) {
        if (!n.parent) continue;
        if (this.pp && (counts.get(k) || 0) > 0) {
          // one strand per prefix, side by side; a dashed strand = that prefix is missing on this branch
          const [x0, y0] = pos(n.parent), [x1, y1] = pos(k), mx = (x0 + x1) / 2;
          for (let p = 0; p < this.np; p++) {
            const c = pc(k, p), o = (p - (this.np - 1) / 2) * 3;
            ctx.strokeStyle = PCOL[p % PCOL.length];
            ctx.globalAlpha = c ? 0.9 : 0.75;
            ctx.lineWidth = c ? 1 + 5 * Math.sqrt(c / this.ptot[p]) : 1.3;
            ctx.setLineDash(c ? [] : [4, 3]);
            ctx.beginPath(); ctx.moveTo(x0, y0 + o); ctx.bezierCurveTo(mx, y0 + o, mx, y1 + o, x1, y1 + o); ctx.stroke();
          }
          ctx.setLineDash([]); ctx.globalAlpha = 1;
          continue;
        }
        const [x0, y0] = pos(n.parent), [x1, y1] = pos(k);
        const [st, col] = stateOf(k);
        const c = counts.get(k) || 0, b = this.base.get(k) || 0;
        ctx.strokeStyle = col;
        ctx.globalAlpha = st === "idle" ? 0.7 : st === "never" ? 0.6 : 1;
        ctx.lineWidth = st === "lost" ? Math.max(1.5, wOf(b) * 0.5) : wOf(c);
        if (st === "never") ctx.lineWidth = 1.4;
        ctx.setLineDash(st === "lost" ? [5, 4] : st === "never" ? [2, 3] : []);
        ctx.beginPath(); ctx.moveTo(x0, y0);
        const mx = (x0 + x1) / 2;
        ctx.bezierCurveTo(mx, y0, mx, y1, x1, y1); ctx.stroke();
      }
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      this.hit = [];
      for (const [k, n] of this.shown) {
        const [x, y] = pos(k);
        let [st, col] = stateOf(k);
        const c = counts.get(k) || 0, b = this.base.get(k) || 0;
        if (this.pp && c > 0) [st, col] = partial(k) ? ["partial", th.warn] : ["live", th.accent];
        const r = n.level === 0 ? 7 : 3 + 4 * Math.sqrt(Math.max(c, st === "lost" ? b * 0.3 : 0) / total);
        ctx.fillStyle = st === "idle" || st === "never" ? th.bg : col;
        ctx.strokeStyle = col; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        let label;
        if (n.others) label = `+${n.others} more`;
        else {
          const nm = (this.D.names[n.asn] || "").split(/[ ,]/)[0];
          label = `AS${n.asn}${n.level === 0 && n.asn === this.D.asn ? " (you)" : nm ? " " + nm.slice(0, 14) : ""}`;
        }
        ctx.font = (n.level <= 1 ? "600 " : "") + "12px system-ui, sans-serif";
        ctx.fillStyle = st === "idle" || st === "lost" || st === "never" ? th.muted : th.text;
        const lxp = x + r + 5;
        ctx.fillText(label, lxp, y - (n.children.length ? 7 : 0));
        const lw = ctx.measureText(label).width;
        ctx.font = "11px system-ui, sans-serif";
        if (this.pp && c > 0 && n.level > 0) {
          // per-prefix counts in each prefix's color, e.g. "120 · 0"
          let cx = n.children.length ? lxp : lxp + lw + 6;
          const cy = n.children.length ? y + 7 : y;
          for (let p = 0; p < this.np; p++) {
            const v = pc(k, p), s = `${v}`;
            ctx.fillStyle = v ? PCOL[p % PCOL.length] : th.crit;
            ctx.fillText(s, cx, cy); cx += ctx.measureText(s).width;
            if (p < this.np - 1) { ctx.fillStyle = th.muted; ctx.fillText(" · ", cx, cy); cx += ctx.measureText(" · ").width; }
          }
          this.hit.push({ x, y, r: Math.max(r, 6), k, n, c, b, per: [...Array(this.np).keys()].map(p => pc(k, p)) });
          continue;
        }
        ctx.fillStyle = st === "lost" || st === "never" ? th.crit : st === "degraded" ? th.warn : st === "new" ? th.ok : th.muted;
        const cnt = n.level === 0 ? "" : st === "never" ? "not carrying you" : st === "lost" ? `0 (was ${b})` : st === "degraded" ? `${c} of ${b}` :
          st === "new" ? `${c} (new)` : `${c}`;
        if (cnt) ctx.fillText(cnt, n.children.length ? lxp : lxp + lw + 6, n.children.length ? y + 7 : y);
        this.hit.push({ x, y, r: Math.max(r, 6), k, n, c, b });
      }
      // timeline strip with event shading and visibility
      const ty = H - 70, th2 = 44, tx0 = 16, tx1 = W - 16, span = this.D.end - this.D.start;
      const X = t => tx0 + (t - this.D.start) / span * (tx1 - tx0);
      ctx.fillStyle = th.codebg; ctx.fillRect(tx0, ty, tx1 - tx0, th2);
      for (const e of this.events) {
        const s = Math.max(e.start, this.D.start), en = Math.min(e.end || this.D.end, this.D.end);
        if (en < this.D.start || s > this.D.end) continue;
        ctx.fillStyle = e.severity === "critical" ? "rgba(200,38,43,.18)" : "rgba(199,119,0,.18)";
        ctx.fillRect(X(s), ty, Math.max(2, X(en) - X(s)), th2);
      }
      if (this.gifRange && !opts.footer) {
        // selected GIF / playback range
        const [a, b] = this.gifRange;
        const xa = X(Math.max(a, this.D.start)), xb = X(Math.min(b, this.D.end));
        ctx.fillStyle = "rgba(36,99,235,.16)"; ctx.fillRect(xa, ty, Math.max(2, xb - xa), th2);
        ctx.strokeStyle = th.accent; ctx.lineWidth = 2;
        for (const x of [xa, xb]) { ctx.beginPath(); ctx.moveTo(x, ty - 6); ctx.lineTo(x, ty + th2 + 6); ctx.stroke(); }
      }
      if (this.baseMode === "best" || this.baseT > this.D.start) {
        ctx.strokeStyle = th.ok; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(X(this.baseT), ty); ctx.lineTo(X(this.baseT), ty + th2); ctx.stroke(); ctx.setLineDash([]);
      }
      const series = this.pp ? this.pvis.map((s, p) => [s, this.ptot[p], PCOL[p % PCOL.length]])
                             : [[this.visSeries, Math.max(this.total0, 1), th.accent]];
      for (const [ser, maxV, colr] of series) {   // each line scaled to its own peak, so drops compare
        ctx.strokeStyle = colr; ctx.lineWidth = 1.5; ctx.beginPath();
        const Y = v => ty + th2 - 3 - (v / maxV) * (th2 - 6);
        ser.forEach(([t, v], i) => {
          if (i === 0) ctx.moveTo(X(t), Y(v));
          else { ctx.lineTo(X(t), Y(ser[i - 1][1])); ctx.lineTo(X(t), Y(v)); }
        });
        ctx.stroke();
      }
      ctx.strokeStyle = th.text; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(X(this.t), ty - 4); ctx.lineTo(X(this.t), ty + th2 + 4); ctx.stroke();
      ctx.fillStyle = th.muted; ctx.font = "11px system-ui, sans-serif";
      ctx.fillText(fmtAxis(this.D.start, span), tx0, ty + th2 + 14);
      const et = fmtAxis(this.D.end, span); ctx.fillText(et, tx1 - ctx.measureText(et).width, ty + th2 + 14);
      if (opts.footer) { const w = ctx.measureText(opts.footer).width; ctx.fillText(opts.footer, (W - w) / 2, ty + th2 + 14); }
      this.timeline = { ty, th2, tx0, tx1 };
      ctx.restore();
    }
    frameTimes(n, T0 = this.D.start, T1 = this.D.end) {
      // half uniform in time, half following update activity, so frames linger where things change
      const ts = this.U.map(u => u[0]).filter(t => t >= T0 && t <= T1);
      const out = [];
      for (let i = 0; i < n; i++) {
        const f = n === 1 ? 0 : i / (n - 1);
        const uni = T0 + f * (T1 - T0);
        const q = ts.length ? ts[Math.min(ts.length - 1, Math.floor(f * (ts.length - 1)))] : uni;
        out.push(Math.min(T1, Math.max(T0, 0.5 * uni + 0.5 * q)));
      }
      out[0] = T0; out[n - 1] = T1;
      return out;
    }
  }

  function loadScript(src) {
    return new Promise((res, rej) => {
      if (document.querySelector(`script[src="${src}"]`)) return res();
      const s = document.createElement("script"); s.src = src; s.onload = res; s.onerror = rej;
      document.head.appendChild(s);
    });
  }

  // Wires a replay panel. `root` contains elements marked data-rp="...":
  // canvas, wrap, slider, tip, status, depth, top, base, play, jump-start, jump-drop, jump-end,
  // frames, delay, gif, gifstatus. Returns a controller with load(url).
  window.initTree = function (root, opts = {}) {
    const q = n => root.querySelector(`[data-rp="${n}"]`);
    const canvas = q("canvas"), wrap = q("wrap"), slider = q("slider"), tip = q("tip"), status = q("status");
    let rp = null, data = null, timer = null, frames = null, fi = 0, following = !!opts.followNow;
    let rangeCustom = false;
    const toInput = ts => new Date(ts * 1000 - new Date(ts * 1000).getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    const fromInput = v => (v ? new Date(v).getTime() / 1000 : NaN);
    const dur = s => { s = Math.round(s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
                       m = Math.floor(s % 3600 / 60), x = s % 60;
                       return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m ${x}s` : `${x}s`; };
    // selected GIF / playback range, clamped to the loaded window
    const range = () => {
      let a = fromInput(q("gif-from").value), b = fromInput(q("gif-to").value);
      if (!isFinite(a)) a = data.start;
      if (!isFinite(b)) b = data.end;
      return [Math.max(data.start, Math.min(a, b)), Math.min(data.end, Math.max(a, b))];
    };
    const syncRange = () => {
      if (!rp) return;
      const [a, b] = range(), whole = a <= data.start + 1 && b >= data.end - 1;
      rp.gifRange = whole ? null : [a, b];
      q("gif-span").textContent = b > a ? `${dur(b - a)} selected${whole ? " (whole window)" : ""}` : "From must be before To";
      render();
    };
    const setRangeInputs = (a, b) => { q("gif-from").value = toInput(a); q("gif-to").value = toInput(b); };

    // Size: draw on a wider logical canvas and show it scaled down by k, so the whole tree fits
    const sizeSel = q("size");
    if (sizeSel) {
      try { sizeSel.value = localStorage.getItem("bgpcanary.treeSize") || "fit"; } catch (e) { }
      sizeSel.onchange = () => { try { localStorage.setItem("bgpcanary.treeSize", sizeSel.value); } catch (e) { } render(); };
    }
    let k = 1;
    const render = () => {
      if (!rp) return;
      const W = wrap.clientWidth;
      if (!W) return;
      const mode = sizeSel ? sizeSel.value : "full";
      const avail = window.innerHeight - 140;
      rp.rowH = 20;
      k = mode === "full" ? 1 : mode === "compact" ? 0.75 : Math.min(1, avail / rp.size(W).H);   // height doesn't depend on width
      if (mode === "fit" && k < 0.6) {
        // too tall to fit by scaling alone without unreadable text: pack the rows tighter, keep the text >= 60%
        const fixed = rp.size(W).H - rp.rows * 20;
        rp.rowH = Math.max(12, (avail / 0.6 - fixed) / Math.max(rp.rows, 3));
        k = Math.max(0.6, Math.min(1, avail / rp.size(W).H));
      }
      const LW = W / k, { H } = rp.size(LW);
      // browsers draw nothing on a canvas over ~32k px a side or ~16M px in all (Safari): use a lower resolution instead
      const dpr = Math.min(window.devicePixelRatio || 1, 32000 / H, 32000 / LW, Math.sqrt(16e6 / (LW * H)));
      canvas.width = Math.round(LW * dpr); canvas.height = Math.round(H * dpr);
      canvas.style.width = W + "px"; canvas.style.height = Math.round(H * k) + "px";
      const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      rp.draw(ctx, LW);
      // slider labels: selected moment, and the window's start/end under the track
      const ro = q("slider-time");
      if (ro) ro.textContent = `${fmt(rp.t)} · ${rp.phase()}`;
      const span = data.end - data.start;
      const dt = ts => { const d = new Date(ts * 1000); return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
      if (q("slider-start")) q("slider-start").textContent = dt(data.start);
      if (q("slider-end")) q("slider-end").textContent = dt(data.end) + (opts.followNow ? " (now)" : "");
    };
    const rebuild = () => { if (rp) { rp.build(parseInt(q("depth").value), parseInt(q("top").value)); render(); } };
    const setT = t => {
      t = Math.min(data.end, Math.max(data.start, t));
      rp.seek(t);
      slider.value = Math.round(1000 * (t - data.start) / (data.end - data.start));
      following = !!opts.followNow && data.end - t < 1;
      render();
    };
    const stop = () => { if (timer) clearInterval(timer); timer = null; if (q("play")) q("play").textContent = "▶ Play"; };

    async function load(url, keepPosition) {
      status.textContent = "Loading path history…";
      const r = await fetch(url);
      if (r.status === 401) { location.href = "/login"; return; }
      if (!r.ok) { status.textContent = "Could not load path history."; return; }
      const prevT = rp && !following ? rp.t : null;
      data = await r.json();
      if (!data.peers.length) { status.textContent = "No stored updates cover this window."; rp = null; return; }
      rp = new Replay(data, q("base") ? q("base").value : (opts.base || "best"));
      status.textContent = `${data.peers.length} RIS peers · ${data.updates.length.toLocaleString()} updates in window`;
      rebuild();
      if (!keepPosition || !rangeCustom) { setRangeInputs(data.start, data.end); rangeCustom = false; }
      syncRange();
      if (keepPosition && prevT !== null && prevT >= data.start) setT(prevT);
      else setT(opts.startAt ? opts.startAt(data) : data.end);
    }

    q("gif-from").addEventListener("change", () => { rangeCustom = true; syncRange(); });
    q("gif-to").addEventListener("change", () => { rangeCustom = true; syncRange(); });
    q("gif-from-slider").onclick = () => { if (rp) { q("gif-from").value = toInput(Math.floor(rp.t)); rangeCustom = true; syncRange(); } };
    q("gif-to-slider").onclick = () => { if (rp) { q("gif-to").value = toInput(Math.ceil(rp.t)); rangeCustom = true; syncRange(); } };
    q("gif-reset").onclick = () => { if (rp) { setRangeInputs(data.start, data.end); rangeCustom = false; syncRange(); } };

    slider.addEventListener("input", () => {
      if (!rp) return; stop();
      rp.seek(data.start + (data.end - data.start) * slider.value / 1000);
      following = !!opts.followNow && slider.value >= 1000;
      render();
    });
    q("depth").onchange = rebuild; q("top").onchange = rebuild;
    if (q("base")) q("base").onchange = () => { if (rp) { rp.setBase(q("base").value); render(); } };
    window.addEventListener("resize", render);
    canvas.addEventListener("click", e => {
      if (!rp) return;
      const b = canvas.getBoundingClientRect(), x = (e.clientX - b.left) / k, y = (e.clientY - b.top) / k, tl = rp.timeline;
      if (tl && y >= tl.ty - 6 && y <= tl.ty + tl.th2 + 6) { stop(); setT(data.start + (x - tl.tx0) / (tl.tx1 - tl.tx0) * (data.end - data.start)); }
    });
    canvas.addEventListener("mousemove", e => {
      if (!rp) return;
      const b = canvas.getBoundingClientRect(), x = (e.clientX - b.left) / k, y = (e.clientY - b.top) / k;
      const h = (rp.hit || []).find(h => (h.x - x) ** 2 + (h.y - y) ** 2 <= (h.r + 3 / k) ** 2);
      if (!h) { tip.style.display = "none"; return; }
      const nm = h.n.others ? `${h.n.others} smaller branches` : `AS${h.n.asn} ${data.names[h.n.asn] || ""}`;
      const ref = rp.baseMode === "start" ? "at window start" : "at comparison point";
      const pin = rp.pinned && rp.pinned.get(h.k);
      const role = pin && pin.of ? `\nNeighbour of AS${pin.of} in the global table${pin.power ? ` (${pin.power} RIS adjacencies)` : ""}` :
        pin && pin.upstream ? "\nYour direct upstream" : "";
      const peak = rp.maxC.get(h.k) || 0;
      const never = pin && !h.c && !h.b ? (peak ? `\nNot carrying you now (carried up to ${peak} peers earlier in this window)`
                                                : "\nCarried none of your paths in this window") : "";
      tip.textContent = h.per
        ? `${nm}${role}\n` + h.per.map((v, p) => `${data.prefixes[p]}: ${v ? v + " peers" : "MISSING on this branch"}`).join("\n")
        : `${nm}${role}\nAt selected time: ${h.c} peers · ${ref}: ${h.b}${never}`;
      tip.style.left = Math.min(x * k + 12, wrap.clientWidth - 260) + "px"; tip.style.top = (y * k + 12) + "px"; tip.style.display = "block";
    });
    canvas.addEventListener("mouseleave", () => tip.style.display = "none");
    if (q("play")) q("play").onclick = () => {
      if (!rp) return;
      if (timer) return stop();
      const [a, b] = range();
      frames = rp.frameTimes(240, a, b);   // plays the selected GIF range (whole window by default)
      fi = frames.findIndex(t => t >= rp.t + 1); if (fi < 0 || fi >= frames.length - 1) fi = 0;
      q("play").textContent = "❚❚ Pause";
      timer = setInterval(() => { setT(frames[fi++]); if (fi >= frames.length) stop(); }, 70);
    };
    const jumpStart = () => (rp.ev ? Math.max(data.start, rp.ev.start - 1) : data.start);
    const jumpEnd = () => (rp.ev && rp.ev.end ? Math.min(data.end, rp.ev.end + 1) : data.end);
    if (q("jump-start")) q("jump-start").onclick = () => { if (rp) { stop(); setT(jumpStart()); } };
    if (q("jump-end")) q("jump-end").onclick = () => { if (rp) { stop(); setT(jumpEnd()); } };
    if (q("jump-drop")) q("jump-drop").onclick = () => {
      if (!rp) return; stop();
      const from = rp.ev ? rp.ev.start : data.start;
      const lo = rp.visSeries.reduce((m, v) => (v[0] >= from && v[1] < m[1] ? v : m), [from, Infinity]);
      setT(lo[0]);
    };
    if (q("gif")) q("gif").onclick = async () => {
      if (!rp) return;
      const btn = q("gif"), gs = q("gifstatus"); btn.disabled = true; stop();
      try {
        gs.textContent = "Loading encoder…";
        await loadScript("/static/vendor/gif.js");
        const nFrames = parseInt(q("frames").value), delay = parseInt(q("delay").value);
        const [a, b] = range();
        if (!(b > a)) throw "the From time must be before the To time";
        // A sub-range gets its own replay: the GIF's tree layout, timeline and colors are scoped to it.
        let g = rp;
        if (a > data.start + 1 || b < data.end - 1) {
          gs.textContent = "Loading range…";
          const qs = new URLSearchParams(data.prefixes ? { prefixes: data.prefixes.join(","), start: a, end: b }
                                                        : { prefix: data.prefix, start: a, end: b });
          const r = await fetch((data.prefixes ? "/api/replay/compare?" : "/api/replay?") + qs);
          if (!r.ok) throw "could not load the selected range";
          const d2 = await r.json();
          if (rp.ev) { d2.event = rp.ev; d2.events = [rp.ev]; }
          g = new Replay(d2, rp.ev && a < rp.ev.start ? rp.baseMode : (q("base") ? q("base").value : "start"));
          g.build(parseInt(q("depth").value), parseInt(q("top").value));
        }
        const keepRowH = rp.rowH;
        g.rowH = 20;   // GIFs always use full spacing, whatever the on-screen Size setting
        const W = 1200, { H } = g.size(W);
        const off = document.createElement("canvas"); off.width = W; off.height = H;
        const ctx = off.getContext("2d", { willReadFrequently: true });
        const gif = new GIF({ workers: 2, quality: 10, width: W, height: H, workerScript: "/static/vendor/gif.worker.js" });
        const keepT = rp.t, label = rp.ev ? `event ${rp.ev.id}` : `${data.prefix}`;
        g.frameTimes(nFrames, a, b).forEach((t, i, ts) => {
          g.seek(t);
          g.draw(ctx, W, { footer: `bgpcanary · ${label}` });
          gif.addFrame(ctx, { copy: true, delay: i === 0 || i === ts.length - 1 ? 1500 : delay });
          if (i % 10 === 0) gs.textContent = `Rendering frames ${i}/${nFrames}…`;
        });
        rp.seek(data.start); rp.seek(keepT);
        rp.rowH = keepRowH;
        gif.on("progress", p => gs.textContent = `Encoding GIF ${Math.round(p * 100)}%…`);
        const blob = await new Promise(res => { gif.on("finished", res); gif.render(); });
        const link = document.createElement("a");
        const d = new Date(a * 1000), stamp = `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}-${pad2(d.getHours())}${pad2(d.getMinutes())}`;
        link.href = URL.createObjectURL(blob);
        link.download = rp.ev ? `bgpcanary-event-${rp.ev.id}-${stamp}.gif` : data.prefixes ? `bgpcanary-compare-${stamp}.gif` : `bgpcanary-paths-${stamp}.gif`;
        document.body.appendChild(link); link.click(); link.remove();
        gs.textContent = `Saved ${(blob.size / 1048576).toFixed(1)} MB GIF`;
      } catch (e) {
        gs.textContent = "GIF export failed: " + e;
      } finally { btn.disabled = false; }
    };
    return { load, isFollowing: () => following && !timer };
  };

  // Event page convenience wrapper
  window.initReplay = function (eventId) {
    const root = document.getElementById("replay");
    const ctl = initTree(root, { base: "best", startAt: d => Math.max(d.start, d.event.start - 1) });
    ctl.load(`/api/events/${eventId}/replay`);
  };
})();
