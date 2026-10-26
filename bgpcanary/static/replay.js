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
  const durText = s => {
    s = Math.round(s);
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
  };
  // Events replayed together (data.stitch, see web.stitched_payload): the quiet time between them is cut out, so the
  // replay's clock isn't the real one. realT: the real time at a moment of the replay; stitchedT: back again (to the
  // nearest stretch); inCut: the cut a moment falls in, if any.
  const realT = (D, t) => {
    const S = D && D.stitch;
    if (!S) return t;
    for (const [a, b, r] of S.segments) if (t <= b) return r + Math.max(0, t - a);
    const [a, b, r] = S.segments[S.segments.length - 1];
    return r + (b - a);
  };
  const stitchedT = (D, real) => {
    const S = D && D.stitch;
    if (!S) return real;
    for (const [a, b, r] of S.segments) if (real <= r + (b - a)) return a + Math.max(0, real - r);
    return S.segments[S.segments.length - 1][1];
  };
  const inCut = (D, t) => {
    const S = D && D.stitch;
    if (!S) return null;
    for (let i = 1; i < S.segments.length; i++) if (t > S.segments[i - 1][1] && t < S.segments[i][0]) return S.cuts[i - 1];
    return null;
  };

  // one color per prefix when several share one tree (up to 12 strands; more are combined into one)
  const PCOL = ["#2463eb", "#e8710a", "#12a36b", "#9b4dca", "#d23f8a", "#0f9bb5",
                "#b8860b", "#5c6bc0", "#6d4c41", "#00796b", "#7cb342", "#455a64"];
  const PRIVATE = "Private peering";   // the group for listed peers that aren't at one of your exchanges (peering.py)

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
      // origin first. Peers (data.exchanges: peer -> exchange) hang off one "Exchanges" branch, split per exchange:
      // origin > "x" > "x:<n>" > peer > ... (two virtual hops; transit branches are unchanged)
      const ex = data.exchanges || {};
      this.xnames = [...new Set(Object.values(ex))].sort((a, b) => (a === PRIVATE) - (b === PRIVATE) || a.localeCompare(b));
      this.rev = this.paths.map(p => {
        const r = p.slice().reverse(), g = r.length > 1 && r[0] === data.asn ? ex[r[1]] : undefined;
        return g === undefined ? r : [r[0], "x", "x:" + this.xnames.indexOf(g), ...r.slice(1)];
      });
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
    build(depth, topN, nbrs = "all") {
      this.depth = depth; this.nbrs = nbrs;
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
      // What each network does with your routes over the whole window: the ones right in front of your AS (and
      // your configured upstreams and peers) reach you directly; the rest of those in any path reach you some other
      // way. A neighbour of an upstream in neither set is one RIS never sees carrying you: that's the one worth a
      // red "not carrying you". The others don't carry you through that upstream because they have a path they
      // prefer (your own peer, say), which is no problem: shown faint blue, or folded into one line (nbrs = "fold").
      const direct = new Set([...(this.D.known_neighbours || []), ...(this.D.upstreams || []),
                              ...Object.keys(this.D.exchanges || {}).map(Number)]);
      const anywhere = new Set();
      for (const p of this.paths) {
        if (p.length < 2 || p[p.length - 1] !== this.D.asn) continue;
        direct.add(p[p.length - 2]);
        for (let i = 0; i < p.length - 1; i++) anywhere.add(p[i]);
      }
      // Always show every direct upstream, and every neighbour (transit/peer) of each upstream from the
      // global table - including the ones that carry none of our paths, so gaps are visible.
      const pinned = new Map(), folds = new Map();
      const rootKey = String(this.D.asn);
      if (depth >= 1) {
        for (const u of this.D.upstreams || []) {
          const uk = `${rootKey}>${u}`;
          addEdge(rootKey, uk);
          pinned.set(uk, { upstream: true });
          if (depth < 2 || nbrs === "none") continue;
          for (const n of (this.D.upstream_neighbors || {})[u] || []) {
            const nk = `${uk}>${n.asn}`;
            const kind = maxC.get(nk) ? null : direct.has(n.asn) ? "direct" : anywhere.has(n.asn) ? "elsewhere" : "never";
            if (nbrs === "fold" && (kind === "direct" || kind === "elsewhere")) {
              if (!folds.has(uk)) folds.set(uk, []);
              folds.get(uk).push({ asn: n.asn, kind, power: n.power });
              continue;
            }
            addEdge(uk, nk);
            pinned.set(nk, { power: n.power, of: u, kind });
          }
          // neighbours it had in the last month and no longer has at all: "no longer peered"
          for (const n of (this.D.upstream_gone || {})[u] || []) {
            const nk = `${uk}>${n.asn}`;
            if (pinned.has(nk)) continue;
            addEdge(uk, nk);
            pinned.set(nk, { of: u, gone: n });
          }
        }
      }
      this.pinned = pinned;
      this.maxC = maxC;
      const pw = k => (pinned.get(k) || {}).power || 0;
      // collapse: keep topN children (pinned ones always), fold the rest into "others"
      const disp = new Map(), shown = new Map();
      const roots = [...all].filter(k => !k.includes(">"));
      const isX = k => /(^|>)x(:\d+)?$/.test(k);   // the "Exchanges" node, or one exchange: always shown
      const visit = (k, level, parent) => {
        const last = k.split(">").pop();
        const xg = last === "x" ? "all" : last.startsWith("x:") ? this.xnames[Number(last.slice(2))] : null;
        shown.set(k, { key: k, level, asn: Number(last), xg, parent, children: [], max: maxC.get(k) || 0 });
        disp.set(k, k);
        if (parent) shown.get(parent).children.push(k);
        const ch = [...(kids.get(k) || [])].sort((a, b) => ((maxC.get(b) || 0) - (maxC.get(a) || 0)) || (pw(b) - pw(a)));
        const top = [], rest = [];
        let free = 0;
        const cap = last.startsWith("x:") ? Math.max(topN, 40) : topN;   // an exchange's peers stay unfolded
        for (const c of ch) {
          if (pinned.has(c) || isX(c)) top.push(c);
          else if (free++ < cap) top.push(c);
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
        const fo = folds.get(k);   // an upstream's neighbours that reach you another way, as one line
        if (fo && fo.length) {
          const fk = k + ">~";
          shown.set(fk, { key: fk, level: level + 1, asn: null, parent: k, children: [], folded: fo, max: 0 });
          shown.get(k).children.push(fk);
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
    _reachNow() {
      // per network in a current path toward you: {c: RIS peers whose path has it, via: its most common next hop
      // toward you (your AS itself when it's right in front of you)}
      const cnt = new Map(), nxt = new Map();
      for (let i = 0; i < this.cur.length; i++) {
        const j = this.cur[i];
        if (!this.originOk(j)) continue;
        const p = this.paths[j];
        for (let x = 0; x < p.length - 1; x++) {
          const a = p[x], h = p[x + 1];
          cnt.set(a, (cnt.get(a) || 0) + 1);
          if (!nxt.has(a)) nxt.set(a, new Map());
          nxt.get(a).set(h, (nxt.get(a).get(h) || 0) + 1);
        }
      }
      const out = new Map();
      for (const [a, c] of cnt) {
        let via = null, best = 0;
        for (const [h, n] of nxt.get(a)) if (n > best) { via = h; best = n; }
        out.set(a, { c, via });
      }
      return out;
    }
    // how a neighbour that doesn't carry you through its upstream does reach you, for its label and tooltip
    reachText(asn, kind, long) {
      const r = this.reach && this.reach.get(asn);
      if (kind === "direct" && !(r && r.via !== this.D.asn)) return long ? "directly" : "reaches you directly";
      if (!r) return long ? "another way" : "reaches you another way";
      const how = r.via === this.D.asn ? "directly" : `${long ? "through" : "via"} AS${r.via}`;
      return long ? `${how} (${r.c} RIS peer${r.c === 1 ? "" : "s"})` : `reaches you ${how}`;
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
      const extra = (this.chips(W).lines - 1) * 20;
      return { W, H: 86 + extra + Math.max(this.rows, 3) * rowH + 24 + 70, rowH, extra };
    }
    // header chips (one per prefix), wrapped onto more lines when they don't fit across
    chips(W) {
      if (!this.pp) return { at: [], lines: 1 };
      const m = Replay.measure || (Replay.measure = document.createElement("canvas").getContext("2d"));
      let x = 230, line = 0;
      const at = this.D.prefixes.map((p, i) => {
        m.font = "600 13px system-ui, sans-serif"; const wa = m.measureText(p).width;
        m.font = "13px system-ui, sans-serif"; const wb = m.measureText(`${this.ptot[i]}/${this.ptot[i]}`).width;
        const w = 17 + wa + 6 + wb + 22;
        if (x > 230 && x + w > W - 16) { line++; x = 230; }
        const c = [x, line];
        x += w;
        return c;
      });
      return { at, lines: line + 1 };
    }
    phase() {
      const ev = this.ev;
      if (ev) {
        if (this.t < ev.start) return "before event";
        if (!ev.end || this.t <= ev.end) return "during event";
        return "after recovery";
      }
      if (this.D.stitch) {   // events replayed together: which one it's in
        const e = this.events.find(e => this.t >= e.start - 1 && this.t <= (e.end || this.D.end) + 1);
        return e ? `event ${e.id}` : inCut(this.D, this.t) ? "between events" : "around the events";
      }
      return ago(Date.now() / 1000 - this.t);
    }
    real(t) { return realT(this.D, t); }
    // the time to show for a moment of the replay (events replayed together: a cut says how much it skipped)
    when(t) { const c = inCut(this.D, t); return c ? `✂ ${durText(c.skipped)} skipped` : fmt(this.real(t)); }
    draw(ctx, W, opts = {}) {
      const th = theme();
      const { H, rowH, extra } = this.size(W);
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
      ctx.fillText(this.when(this.t), 16, 42);
      if (this.pp) {
        // one chip per prefix: color, prefix, how many RIS peers see it at the selected time
        const { at } = this.chips(W);
        this.D.prefixes.forEach((p, i) => {
          const v = this.visibleP(i), pc = Math.round(100 * v / this.ptot[i]);
          let [x, line] = at[i];
          const y = 42 + line * 20;
          ctx.fillStyle = PCOL[i % PCOL.length]; ctx.fillRect(x, y - 6, 12, 12);
          ctx.font = "600 13px system-ui, sans-serif"; ctx.fillStyle = th.text;
          const a = `${p}`; ctx.fillText(a, x + 17, y); x += 17 + ctx.measureText(a).width + 6;
          ctx.font = "13px system-ui, sans-serif"; ctx.fillStyle = pc < 50 ? th.crit : pc < 80 ? th.warn : th.muted;
          ctx.fillText(`${v}/${this.ptot[i]}`, x, y);
        });
      }
      const pct = Math.round(100 * vis / total);
      if (!this.pp) {
        ctx.fillStyle = pct < 50 ? th.crit : pct < 80 ? th.warn : th.ok;
        ctx.font = "600 14px system-ui, sans-serif";
        const vt = `${vis}/${this.total0} RIS peers (${pct}%)`;
        ctx.fillText(vt, 230, 42);
        ctx.fillStyle = th.muted; ctx.font = "13px system-ui, sans-serif";
        const cmp = this.baseMode === "start" ? `colors vs ${fmtAxis(this.real(this.baseT), this.real(this.D.end) - this.real(this.D.start))}` : "colors vs healthiest point";
        ctx.fillText(`${this.phase()} · ${cmp}`, 230 + ctx.measureText(vt).width + 40, 42);
      }
      // legend
      const lg = this.pp   // [label, colour, dash, width]: the same patterns the branches use
        ? [["one strand per prefix", th.accent, [], 3], ["prefix missing on this branch", th.warn, [4, 3], 3], ["not carrying you", th.crit, [1, 3], 2], ["reaches you another way", th.accent, [1, 3], 2], ["no longer peered", th.muted, [6, 3, 1, 3], 2], ["idle", th.border, [], 3]]
        : [["carrying routes", th.accent, [], 3], ["lost >50%", th.warn, [], 3], ["dropped", th.crit, [6, 4], 4], ["new", th.ok, [], 3], ["not carrying you", th.crit, [1, 3], 2], ["reaches you another way", th.accent, [1, 3], 2], ["no longer peered", th.muted, [6, 3, 1, 3], 2], ["idle", th.border, [], 3]];
      let lx = W - 16;
      ctx.font = "12px system-ui, sans-serif";
      for (let i = lg.length - 1; i >= 0; i--) {
        const [label, col, dash, lw] = lg[i];
        const w = ctx.measureText(label).width;
        lx -= w; ctx.fillStyle = th.muted; ctx.fillText(label, lx, 20);
        lx -= 26; ctx.strokeStyle = col; ctx.lineWidth = lw; ctx.setLineDash(dash);
        ctx.beginPath(); ctx.moveTo(lx, 20); ctx.lineTo(lx + 20, 20); ctx.stroke(); ctx.setLineDash([]);
        lx -= 16;
      }
      // tree geometry
      const top = 86 + extra, labelW = 240, left = 24;
      const colW = (W - left - labelW) / Math.max(this.depth, 1);
      const pos = k => { const n = this.shown.get(k); return [left + n.level * colW, top + n.row * rowH + rowH / 2]; };
      // column headings, so it's clear which layer is which (e.g. vs bgp.tools' upstream list)
      const COLS = this.xnames.length
        ? ["You", "Upstreams · exchanges", "Their transits & peers · exchange", "Next hop out · your peers", "Further out", "Further out", "Further out"]
        : ["You", "Your upstreams", "Their transits & peers", "Next hop out", "Further out", "Further out", "Further out"];
      ctx.font = "600 11px system-ui, sans-serif"; ctx.fillStyle = th.muted;
      for (let l = 0; l <= this.depth; l++) ctx.fillText(COLS[l].toUpperCase(), left + l * colW - (l ? 4 : 0), 68 + extra);
      ctx.strokeStyle = th.border; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(16, 77 + extra); ctx.lineTo(W - 16, 77 + extra); ctx.stroke();
      this.reach = this._reachNow();
      const stateOf = k => {
        const c = counts.get(k) || 0, b = this.base.get(k) || 0;
        if ((this.shown.get(k) || {}).folded) return ["fold", th.muted];
        if (!c && this.pinned && (this.pinned.get(k) || {}).gone) return ["gone", th.muted];
        if (c > 0 && b > 0) return c < b * 0.5 ? ["degraded", th.warn] : ["live", th.accent];
        if (c === 0 && b > 0) return ["lost", th.crit];
        if (c > 0 && b === 0) return ["new", th.ok];
        // an upstream / upstream-neighbour carrying none of our paths now (and none at the comparison point):
        // red when RIS sees it carrying you nowhere, faint blue when it reaches you another way (see build)
        if (this.pinned && this.pinned.has(k)) {
          const kind = this.pinned.get(k).kind;
          return kind === "direct" || kind === "elsewhere" ? [kind, th.accent] : ["never", th.crit];
        }
        return ["idle", th.border];
      };
      const soft = st => st === "never" || st === "gone" || st === "direct" || st === "elsewhere" || st === "fold";
      // the faint states, told apart by pattern as well as colour: dropped = red dashed, thick (it carried you at the
      // comparison point); not carrying you = red dotted; reaches you another way = blue dotted (it does carry you,
      // just not through here); no longer peered = grey dash-dot.   [dash, width (null: by peers), alpha]
      const STYLE = { lost: [[6, 4], null, 1], never: [[1, 3], 1.4, 0.8], direct: [[1, 3], 1.4, 0.55], elsewhere: [[1, 3], 1.4, 0.55],
                      fold: [[1, 3], 1.4, 0.55], gone: [[6, 3, 1, 3], 1.4, 0.6], idle: [[], null, 0.7] };
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
        const sty = STYLE[st];
        ctx.strokeStyle = col;
        ctx.globalAlpha = sty ? sty[2] : 1;
        ctx.lineWidth = st === "lost" ? Math.max(2.5, wOf(b) * 0.5) : sty && sty[1] ? sty[1] : wOf(c);
        ctx.setLineDash(sty ? sty[0] : []);
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
        ctx.fillStyle = st === "idle" || soft(st) ? th.bg : col;
        ctx.strokeStyle = col; ctx.lineWidth = 1.5;
        ctx.beginPath();
        if (n.xg) ctx.rect(x - r, y - r, 2 * r, 2 * r);   // a grouping (exchanges), not a network
        else ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill(); ctx.stroke();
        let label;
        if (n.others) label = `+${n.others} more`;
        else if (n.folded) label = `+${n.folded.length} reach you another way`;
        else if (n.xg) label = n.xg === "all" ? "Exchanges" : n.xg;
        else {
          const nm = (this.D.names[n.asn] || "").split(/[ ,]/)[0];
          label = `AS${n.asn}${n.level === 0 && n.asn === this.D.asn ? " (you)" : nm ? " " + nm.slice(0, 14) : ""}`;
        }
        ctx.font = (n.level <= 1 || n.xg ? "600 " : "") + "12px system-ui, sans-serif";
        ctx.fillStyle = st === "idle" || st === "lost" || soft(st) ? th.muted : th.text;
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
        const pin = (this.pinned && this.pinned.get(k)) || {};
        const cnt = n.level === 0 || n.folded ? "" : st === "never" ? `not carrying you${pin.of ? " via AS" + pin.of : ""}`
          : st === "direct" || st === "elsewhere" ? this.reachText(n.asn, st)
          : st === "gone" ? "no longer peered" : st === "lost" ? `0 (was ${b})` : st === "degraded" ? `${c} of ${b}` :
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
      let lastLabel = -Infinity;   // (a cut's label only where it fits: many short cuts would pile up)
      for (const c of (this.D.stitch || {}).cuts || []) {   // events replayed together: where quiet time was cut out
        const x = X(c.at), s = `✂ ${durText(c.skipped)}`;
        ctx.fillStyle = th.bg; ctx.fillRect(x - 2, ty, 4, th2);
        ctx.strokeStyle = th.muted; ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
        ctx.beginPath(); ctx.moveTo(x, ty - 3); ctx.lineTo(x, ty + th2 + 3); ctx.stroke(); ctx.setLineDash([]);
        ctx.font = "10px system-ui, sans-serif";
        const w = ctx.measureText(s).width, lx = Math.min(Math.max(x - w / 2, tx0), tx1 - w);
        if (lx < lastLabel + 10) continue;
        ctx.fillStyle = th.bg; ctx.fillRect(lx - 2, ty + 2, w + 4, 12);
        ctx.fillStyle = th.muted; ctx.fillText(s, lx, ty + 8);
        lastLabel = lx + w;
      }
      ctx.strokeStyle = th.text; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(X(this.t), ty - 4); ctx.lineTo(X(this.t), ty + th2 + 4); ctx.stroke();
      ctx.fillStyle = th.muted; ctx.font = "11px system-ui, sans-serif";
      const rs = this.real(this.D.end) - this.real(this.D.start);
      ctx.fillText(fmtAxis(this.real(this.D.start), rs), tx0, ty + th2 + 14);
      const et = fmtAxis(this.real(this.D.end), rs); ctx.fillText(et, tx1 - ctx.measureText(et).width, ty + th2 + 14);
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
    // (the inputs show real times; events replayed together run on a stitched clock, see realT)
    const toInput = ts => { const r = realT(data, ts); return new Date(r * 1000 - new Date(r * 1000).getTimezoneOffset() * 60000).toISOString().slice(0, 19); };
    const fromInput = v => (v ? stitchedT(data, new Date(v).getTime() / 1000) : NaN);
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
      q("gif-span").textContent = b > a ? `${dur(b - a)} selected${data.stitch ? ", quiet time cut out" : ""}${whole ? " (whole window)" : ""}`
                                        : "From must be before To";
      render();
    };
    const setRangeInputs = (a, b) => { q("gif-from").value = toInput(a); q("gif-to").value = toInput(b); };

    // Size: draw on a wider logical canvas and show it scaled down by k, so the whole tree fits
    const sizeSel = q("size");
    if (sizeSel) {
      try { sizeSel.value = localStorage.getItem("bgpcanary.treeSize") || "fit"; } catch (e) { }
      sizeSel.onchange = () => { try { localStorage.setItem("bgpcanary.treeSize", sizeSel.value); } catch (e) { } render(); };
    }
    // Upstream neighbours: only the ones RIS sees carrying you nowhere (the default), all of them, or none
    const nbrsSel = q("nbrs");
    if (nbrsSel) {
      try { const v = localStorage.getItem("bgpcanary.treeNbrs"); nbrsSel.value = v === "problems" ? "fold" : v || "all"; } catch (e) { }
      if (!nbrsSel.value) nbrsSel.value = "all";
      nbrsSel.onchange = () => { try { localStorage.setItem("bgpcanary.treeNbrs", nbrsSel.value); } catch (e) { } rebuild(); };
    }
    const nbrsMode = () => (nbrsSel ? nbrsSel.value : "all");
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
      if (ro) ro.textContent = `${rp.when(rp.t)} · ${rp.phase()}`;
      const dt = ts => { const d = new Date(rp.real(ts) * 1000); return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
      if (q("slider-start")) q("slider-start").textContent = dt(data.start);
      if (q("slider-end")) q("slider-end").textContent = dt(data.end) + (opts.followNow ? " (now)" : "");
    };
    const rebuild = () => { if (rp) { rp.build(parseInt(q("depth").value), parseInt(q("top").value), nbrsMode()); render(); } };
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
      // the tooltip stays inside the tree's panel (which clips it): to the left of the cursor near the right edge,
      // above it near the bottom
      const showTip = text => {
        tip.textContent = text; tip.style.display = "block";
        const tw = tip.offsetWidth, tht = tip.offsetHeight;
        tip.style.left = Math.max(0, Math.min(x * k + 12, wrap.clientWidth - tw - 8)) + "px";
        tip.style.top = (y * k + 12 + tht > wrap.clientHeight ? Math.max(0, y * k - tht - 12) : y * k + 12) + "px";
      };
      if (h.n.folded) {   // an upstream's neighbours that reach you another way: who, and how
        const of = h.n.parent.split(">").pop(), fo = h.n.folded;
        showTip(`${fo.length} neighbour${fo.length === 1 ? "" : "s"} of AS${of} that don't carry you through it because they have a path to you they prefer. No problem: a network uses its best route.\n`
          + fo.slice(0, 14).map(f => `AS${f.asn} ${(data.names[f.asn] || "").split(/[ ,]/)[0]}: ${f.kind === "direct" ? "your direct neighbour" : rp.reachText(f.asn, f.kind, true)}`).join("\n")
          + (fo.length > 14 ? `\n+${fo.length - 14} more` : ""));
        return;
      }
      const nm = h.n.others ? `${h.n.others} smaller branches`
        : h.n.xg === "all" ? "Exchanges: networks that peer with you"
        : h.n.xg ? `${h.n.xg}: peers carrying your routes (they pass them only to their own customers)`
        : `AS${h.n.asn} ${data.names[h.n.asn] || ""}`;
      const ref = rp.baseMode === "start" ? "at window start" : "at comparison point";
      const pin = rp.pinned && rp.pinned.get(h.k);
      const peerAt = !h.n.xg && h.n.level === 3 && /(^|>)x>x:\d+>\d+$/.test(h.k) ? (data.exchanges || {})[h.n.asn] : null;
      // (RIPEstat's neighbour data is by UTC day)
      const day = t => new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
      const role = pin && pin.gone ? `\nNo longer peered with AS${pin.of}: last seen next to it in the global table on ${day(pin.gone.last)}` +
          (pin.gone.peak ? ` (at most ${pin.gone.peak} RIS adjacencies, ${day(pin.gone.peak_at)})` : "") :
        pin && pin.of ? `\nNeighbour of AS${pin.of} in the global table${pin.power ? ` (${pin.power} RIS adjacencies)` : ""}` :
        pin && pin.upstream ? "\nYour direct upstream" : peerAt ? `\nPeers with you (${peerAt})` : "";
      const peak = rp.maxC.get(h.k) || 0, kind = pin && pin.kind, of = pin && pin.of;
      // why a neighbour of your upstream doesn't carry you through it: expected (it has a path it prefers), or
      // a real gap, with the transit filter check's view of that pair when it has one
      const tc = ((data.transit_check || {}).rows || {})[`${of}|${h.n.asn}`], minp = (data.transit_check || {}).min_peers || 5;
      const verdict = kind !== "never" || !tc ? "" : tc.ref >= minp
        ? ` The transit filter check: it carries AS${of}'s own prefixes to ${tc.ref} RIS peers but none of yours from it${tc.flag ? " (an open notice)" : ""}: usually a prefix filter between them.`
        : ` RIS can't tell much here: it sees it in front of AS${of} at only ${tc.ref} RIS peer${tc.ref === 1 ? "" : "s"}, even for AS${of}'s own prefixes.`;
      const never = kind === "direct" ? `\nIt's your direct neighbour too, so it prefers its own path to you over AS${of}'s: not carrying you through AS${of} is expected.`
        : kind === "elsewhere" ? `\nIt reaches you ${rp.reachText(h.n.asn, kind, true)} rather than through AS${of}, so not carrying you through AS${of} is expected.`
        : kind === "never" ? `\nRIS sees no route from it to you at all, through AS${of} or any other way.${verdict}`
        : pin && !h.c && !h.b ? (peak ? `\nNot carrying you now (carried up to ${peak} peers earlier in this window)`
                                      : "\nCarried none of your paths in this window") : "";
      showTip(h.per
        ? `${nm}${role}\n` + h.per.map((v, p) => `${data.prefixes[p]}: ${v ? v + " peers" : "MISSING on this branch"}`).join("\n")
        : `${nm}${role}\nAt selected time: ${h.c} peers · ${ref}: ${h.b}${never}`);
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
    // events replayed together: to the start of the one before / after the selected moment
    const evJump = dir => {
      if (!rp || !rp.events.length) return;
      stop();
      const starts = rp.events.map(e => Math.max(data.start, e.start - 1)).sort((x, y) => x - y);
      const t = dir > 0 ? starts.find(s => s > rp.t + 1) : starts.filter(s => s < rp.t - 1).pop();
      if (t !== undefined) setT(t);
    };
    if (q("prev-event")) q("prev-event").onclick = () => evJump(-1);
    if (q("next-event")) q("next-event").onclick = () => evJump(1);
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
        if ((a > data.start + 1 || b < data.end - 1) && !data.stitch) {   // (events together: the replay already has it)
          gs.textContent = "Loading range…";
          const many = data.prefixes || data.combined;
          const qs = new URLSearchParams(many ? { prefixes: many.join(","), start: a, end: b, combine: data.combined ? 1 : 0 }
                                              : { prefix: data.prefix, start: a, end: b });
          const r = await fetch((many ? "/api/replay/compare?" : "/api/replay?") + qs);
          if (!r.ok) throw "could not load the selected range";
          const d2 = await r.json();
          if (rp.ev) { d2.event = rp.ev; d2.events = [rp.ev]; }
          g = new Replay(d2, rp.ev && a < rp.ev.start ? rp.baseMode : (q("base") ? q("base").value : "start"));
          g.build(parseInt(q("depth").value), parseInt(q("top").value), nbrsMode());
        }
        const keepRowH = rp.rowH;
        g.rowH = 20;   // GIFs always use full spacing, whatever the on-screen Size setting
        const W = 1200, { H } = g.size(W);
        const off = document.createElement("canvas"); off.width = W; off.height = H;
        const ctx = off.getContext("2d", { willReadFrequently: true });
        const gif = new GIF({ workers: 2, quality: 10, width: W, height: H, workerScript: "/static/vendor/gif.worker.js" });
        const keepT = rp.t, label = rp.ev ? `event ${rp.ev.id}`
          : data.stitch ? `events ${rp.events.map(e => e.id).join(", ")}` : `${data.prefix}`;
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
        const d = new Date(realT(data, a) * 1000), stamp = `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}-${pad2(d.getHours())}${pad2(d.getMinutes())}`;
        link.href = URL.createObjectURL(blob);
        link.download = rp.ev ? `bgpcanary-event-${rp.ev.id}-${stamp}.gif` : data.stitch ? `bgpcanary-events-${stamp}.gif`
          : data.prefixes || data.combined ? `bgpcanary-compare-${stamp}.gif` : `bgpcanary-paths-${stamp}.gif`;
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

  // Events replayed together (Events › tick some › Replay selected together), from the first one's start
  window.initStitched = function (ids) {
    const root = document.getElementById("replay");
    const ctl = initTree(root, { base: "best", startAt: d => d.start });
    ctl.load(`/api/events/replay?ids=${ids.join(",")}`);
  };
})();
