// Path finder: the known AS paths between your network and another one, as a tree from your AS out to theirs. The
// shortest known path is colored, and so are the ones in use: the traceroute from this server (your traffic) and the
// RIPE Atlas traceroutes from their network (theirs, back to you). Matching traceroutes to paths happens on the
// server (pathfinder.match_trace); this draws what it says.
(function () {
  const root = document.getElementById("pf");
  if (!root) return;
  const $ = n => root.querySelector(`[data-pf="${n}"]`);
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const theme = () => ({
    bg: css("--panel") || "#fff", text: css("--text") || "#111", muted: css("--muted") || "#666",
    border: css("--border") || "#ddd", accent: css("--accent") || "#2463eb", ok: css("--ok") || "#1a8a4a",
    warn: css("--warn") || "#c77700", crit: css("--crit") || "#c8262b", back: "#9b4dca",
  });
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const ROW = 24, TOP = 40, LEFT = 118, LABEL = 190, COL = 150;   // (LEFT: room for your AS's label, left of it)
  const query = () => {
    const u = new URLSearchParams({ q: root.dataset.q });
    if (root.dataset.prefix) u.set("prefix", root.dataset.prefix);
    if (root.dataset.origin) u.set("origin", root.dataset.origin);
    return u;
  };

  let D = null;        // /api/pathfinder
  let BASE = [];       // its paths; D.paths = these + the traceroutes' own paths when they aren't among them
  let TR = null;       // /api/pathfinder/trace
  let BK = null;       // the trace back: {info: /api/pathfinder/back, res: /api/pathfinder/back/<msm>}
  let marks = [];      // colored strands: {color, idx (into D.paths), len (networks covered; Infinity = all)}
  let tree = null, hover = -1, hits = [];

  const nameOf = a => (D && D.names[a]) || (TR && TR.names && TR.names[a]) || (BK && BK.res && BK.res.names[a]) || "";
  const short = a => { const n = nameOf(a).split(/[ ,]/)[0]; return n && n.toUpperCase() !== `AS${a}` ? n.slice(0, 14) : ""; };
  const label = a => `AS${a}` + (a === D.me ? " (you)" : short(a) ? " " + short(a) : "");
  const chip = a => `<span title="${esc(nameOf(a))}">AS${a}${short(a) ? " <span class=\"muted\">" + esc(short(a)) + "</span>" : ""}</span>`;
  const UNSEEN = "Not in the traceroute: its routers didn't answer, or answered from another network's addresses";
  // (in brackets: networks a traceroute didn't show, put in from RIS)
  const pathHtml = (asns, unseen = []) => asns.map(a => unseen.includes(a) ? `<span title="${UNSEEN}">(${chip(a)})</span>` : chip(a)).join(" → ");
  const hops = p => p.asns.length - 1;
  const key = a => a.join(" ");
  const ago = ts => { const s = Date.now() / 1000 - ts; return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`; };
  const fmt = ts => window.fmtTs ? fmtTs(ts) : new Date(ts * 1000).toLocaleString();

  async function api(url, opts) {
    let r;
    try { r = await fetch(url, opts); } catch (e) { return { error: "No answer from bgpcanary." }; }
    if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); return null; }
    return r.json().catch(() => ({ error: `Failed (HTTP ${r.status}).` }));
  }

  // ---- the tree: one branch per path from your AS; every path ends in its own leaf on a bar for them -------------
  function build() {
    const P = D.paths, them = D.target.asn, rootKey = String(D.me);
    const nodes = new Map([[rootKey, { key: rootKey, asn: D.me, depth: 0, kids: [], paths: [], parent: null }]]);
    let maxD = 0;
    P.forEach((p, i) => {
      const reaches = p.asns[p.asns.length - 1] === them;   // (a traceroute can stop short of them)
      const chain = reaches ? p.asns.slice(0, -1) : p.asns;
      let k0 = rootKey;
      nodes.get(k0).paths.push(i);
      for (let d = 1; d < chain.length; d++) {
        const k = k0 + ">" + chain[d];
        if (!nodes.has(k)) { nodes.set(k, { key: k, asn: chain[d], depth: d, kids: [], paths: [], parent: k0 }); nodes.get(k0).kids.push(k); }
        nodes.get(k).paths.push(i);
        k0 = k;
      }
      maxD = Math.max(maxD, chain.length - 1);
      if (reaches) {   // the path's own leaf on their bar
        const k = k0 + ">=" + i;
        nodes.set(k, { key: k, asn: them, depth: -1, kids: [], paths: [i], parent: k0, leaf: true });
        nodes.get(k0).kids.push(k);
      } else p.stopped = k0;
    });
    // the shortest path through a branch first (possible peerings after the known paths), then the one most RIS paths
    // point along
    const len = i => P[i].asns.length + (P[i].possible ? 100 : 0);
    const rank = k => { const n = nodes.get(k); return [Math.min(...n.paths.map(len)), -Math.max(...n.paths.map(i => P[i].seen)), Math.min(...n.paths)]; };
    for (const n of nodes.values()) n.kids.sort((a, b) => { const x = rank(a), y = rank(b); return x[0] - y[0] || x[1] - y[1] || x[2] - y[2]; });
    let rows = 0;
    const place = k => {
      const n = nodes.get(k);
      if (!n.kids.length) { n.row = rows++; return; }
      n.kids.forEach(place);
      n.row = (nodes.get(n.kids[0]).row + nodes.get(n.kids[n.kids.length - 1]).row) / 2;
    };
    place(rootKey);
    tree = { nodes, rows, maxD, rootKey };
  }

  function draw() {
    if (!tree) return;
    const canvas = $("canvas"), wrap = $("wrap"), th = theme();
    // (narrower than that and the labels run into each other: the panel scrolls sideways instead)
    const W = Math.max(wrap.clientWidth, LEFT + LABEL + (tree.maxD + 1) * COL), H = TOP + Math.max(tree.rows, 1) * ROW + 12;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = th.bg; ctx.fillRect(0, 0, W, H);
    ctx.textBaseline = "middle";
    const cols = tree.maxD + 1, colW = (W - LEFT - LABEL) / cols, xT = LEFT + cols * colW;
    const pos = n => [n.leaf ? xT : LEFT + n.depth * colW, TOP + n.row * ROW + ROW / 2];
    const HEAD = ["You", "Your upstreams", "Their transits & peers"];
    ctx.font = "600 11px system-ui, sans-serif"; ctx.fillStyle = th.muted;
    for (let d = 0; d < cols; d++) {
      ctx.textAlign = d ? "left" : "right";
      ctx.fillText((HEAD[d] || "Further on").toUpperCase(), LEFT + d * colW + (d ? -4 : 8), 14);
    }
    ctx.textAlign = "left";
    ctx.fillText(`AS${D.target.asn}`, xT - 4, 14);
    ctx.strokeStyle = th.border; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(10, 26); ctx.lineTo(W - 10, 26); ctx.stroke();
    // the colored strands an edge into node n carries (marks: shortest known path, the ones in use)
    const strands = n => marks.filter(m => n.paths.includes(m.idx) && (n.leaf ? m.len === Infinity : n.depth < m.len))
                              .map(m => ({ col: th[m.color], dash: !!(D.paths[m.idx].trace || D.paths[m.idx].back) }));
    const curve = (x0, y0, x1, y1, o) => {
      // a child one column on: a curve; a leaf further right: the curve, then straight on to their bar
      const xe = Math.min(x1, x0 + colW), mx = (x0 + xe) / 2;
      ctx.beginPath(); ctx.moveTo(x0, y0 + o); ctx.bezierCurveTo(mx, y0 + o, mx, y1 + o, xe, y1 + o);
      if (xe < x1) ctx.lineTo(x1, y1 + o);
      ctx.stroke();
    };
    const edges = [...tree.nodes.values()].filter(n => n.parent);
    for (const n of edges) {   // every known path, quietly (dashed: only traceroutes', dotted: only possible peerings)
      const [x0, y0] = pos(tree.nodes.get(n.parent)), [x1, y1] = pos(n);
      const extra = n.paths.every(i => D.paths[i].trace || D.paths[i].back), maybe = n.paths.every(i => D.paths[i].possible);
      ctx.strokeStyle = th.muted; ctx.globalAlpha = maybe ? 0.75 : 0.45; ctx.lineWidth = 1.5;
      ctx.setLineDash(extra ? [4, 3] : maybe ? [1, 4] : []); ctx.lineCap = maybe ? "round" : "butt";
      curve(x0, y0, x1, y1, 0);
    }
    ctx.globalAlpha = 1; ctx.setLineDash([]); ctx.lineCap = "butt";
    for (const n of edges) {   // the colored paths on top, side by side where they share an edge
      const s = strands(n);
      if (!s.length) continue;
      const [x0, y0] = pos(tree.nodes.get(n.parent)), [x1, y1] = pos(n);
      s.forEach((m, j) => {
        ctx.strokeStyle = m.col; ctx.lineWidth = s.length > 1 ? 2.6 : 3.5; ctx.setLineDash(m.dash ? [6, 3] : []);
        curve(x0, y0, x1, y1, s.length > 1 ? (j - (s.length - 1) / 2) * 3 : 0);
      });
      ctx.setLineDash([]);
    }
    if (hover >= 0) {
      ctx.strokeStyle = th.text; ctx.lineWidth = 2; ctx.globalAlpha = 0.8;
      for (const n of edges) if (n.paths.includes(hover)) { const [x0, y0] = pos(tree.nodes.get(n.parent)), [x1, y1] = pos(n); curve(x0, y0, x1, y1, 0); }
      ctx.globalAlpha = 1;
    }
    const leaves = edges.filter(n => n.leaf);   // their bar: every path's leaf ends on it
    if (leaves.length) {
      const ys = leaves.map(n => pos(n)[1]), y0 = Math.min(...ys) - 8, y1 = Math.max(...ys) + 8;
      ctx.fillStyle = th.border; ctx.beginPath(); ctx.roundRect ? ctx.roundRect(xT - 4, y0, 8, y1 - y0, 4) : ctx.rect(xT - 4, y0, 8, y1 - y0); ctx.fill();
      ctx.font = "600 13px system-ui, sans-serif"; ctx.fillStyle = th.text;
      ctx.fillText(`AS${D.target.asn}`, xT + 12, (y0 + y1) / 2 - 8);
      ctx.font = "12px system-ui, sans-serif"; ctx.fillStyle = th.muted;
      ctx.fillText(nameOf(D.target.asn).slice(0, 26), xT + 12, (y0 + y1) / 2 + 8);
    }
    const text = (s, x, y, col) => {   // on a backing of the panel's color, so the branches pass behind it
      const w = ctx.measureText(s).width;
      ctx.globalAlpha = 0.85; ctx.fillStyle = th.bg; ctx.fillRect(x - 2, y - 8, w + 4, 16); ctx.globalAlpha = 1;
      ctx.fillStyle = col; ctx.fillText(s, x, y);
    };
    hits = [];
    const radius = n => n.leaf ? 3 : n.depth === 0 ? 7 : 4.5;
    for (const n of tree.nodes.values()) {
      const [x, y] = pos(n), s = strands(n), r = radius(n), col = s.length ? s[s.length - 1].col : null;
      ctx.fillStyle = col || (n.leaf ? th.muted : th.bg);
      ctx.strokeStyle = col || th.muted; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      hits.push({ x, y, n });
    }
    for (const n of tree.nodes.values()) {   // (labels after every node, so no backing covers one)
      if (n.leaf) continue;
      const [x, y] = pos(n), lit = strands(n).length > 0, r = radius(n);
      ctx.font = (lit || n.depth === 0 ? "600 " : "") + "12px system-ui, sans-serif";
      const t = label(n.asn), up = n.kids.length > 0;   // (above its edges; yours to the left of it)
      if (n.depth === 0) text(t, x - r - 6 - ctx.measureText(t).width, y, th.text);
      else text(t, x + r + 5, y - (up ? 8 : 0), lit ? th.text : th.muted);
      const st = D.paths.find(p => p.stopped === n.key);   // (a trace back from their upstream starts there)
      if (st) {
        ctx.font = "11px system-ui, sans-serif";
        text(st.back ? "traced back from here" : "traceroute stopped here", x + r + 5, y + (up ? 6 : 13), st.back ? th.muted : th.warn);
      }
    }
  }

  function tip(ev) {
    const t = $("tip"), b = $("canvas").getBoundingClientRect();
    const x = ev.clientX - b.left, y = ev.clientY - b.top;
    const h = hits.find(h => Math.hypot(h.x - x, h.y - y) < 9);
    if (!h) { t.style.display = "none"; return; }
    const n = h.n, P = D.paths;
    t.textContent = n.leaf
      ? `Path ${n.paths[0] + 1}: ${hops(P[n.paths[0]])} AS hops to AS${D.target.asn}`
      : `AS${n.asn} ${nameOf(n.asn)}\non ${n.paths.length} of ${P.length} paths`;
    t.style.display = "block";
    t.style.left = Math.min(x + 14, b.width - 270) + "px"; t.style.top = (y + 14) + "px";
  }

  // ---- the list of paths --------------------------------------------------------------------------------------------
  const way = p => p.trace ? "the traceroute's path (RIS doesn't show it)" : p.back ? "a RIPE Atlas probe's path back (RIS doesn't show it)"
    : p.possible ? `possible peering: ${p.asns.length > 2 ? `AS${p.asns[1]} and they're` : "you're"} both at ${esc(p.possible.join(", "))} (PeeringDB)`
    : p.listed ? "your listed peer (Settings › Monitored network)"
    : p.own ? "their own route to you (RIS sees it)"
    : p.nbr ? "your traffic to them, through a neighbour of your upstream (RIPEstat)"
    : p.dirs.length > 1 ? "both ways" : p.dirs[0] === "out" ? "your traffic to them" : "their traffic to you";
  function table() {
    const rows = D.paths.map((p, i) => {
      const tags = marks.map(m => m.idx !== i ? "" : m.color === "accent" ? ' <span class="badge b-info">shortest</span>'
        : m.color === "ok" ? ' <span class="badge b-ok">traceroute</span>' : ' <span class="badge pf-b-back">back to you</span>').join("");
      return `<tr data-i="${i}"><td class="right">${i + 1}</td><td>${pathHtml(p.asns)}${tags}</td>`
        + `<td class="right">${hops(p)}</td><td>${way(p)}</td><td class="right">${p.trace || p.back || p.possible || p.listed ? "" : p.seen}</td></tr>`;
    });
    $("table").innerHTML = `<tr><th class="right">#</th><th>AS path (you → them)</th><th class="right">AS hops</th><th>Which way</th>`
      + `<th class="right" title="How many RIS (and Route Views) peers' paths point along it">Seen in</th></tr>` + rows.join("");
    $("table").querySelectorAll("tr[data-i]").forEach(tr => {
      tr.onmouseenter = () => { hover = +tr.dataset.i; draw(); };
      tr.onmouseleave = () => { hover = -1; draw(); };
    });
  }

  function header() {
    const t = D.target, P = BASE;
    const where = t.host ? ` <span class="muted">· ${esc(t.host)} = ${esc(t.ip)} in ${esc(t.prefix)}</span>`
      : t.ip ? ` <span class="muted">· ${esc(t.ip)} in ${esc(t.prefix)}</span>` : ` <span class="muted">· ${esc(t.prefix)}</span>`;
    $("title").innerHTML = `AS${D.me} (you) → ${chip(t.asn)}` + where;
    let pick = "";
    if (t.prefixes && t.prefixes.length > 1)
      pick += `<label class="muted">Prefix <select data-pick="prefix">${t.prefixes.map(p => `<option ${p === t.prefix ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></label>`
        + ` <span class="muted">${t.count.toLocaleString()} announced</span>`;
    if (t.origins.length > 1)
      pick += ` <label class="muted">Announced by <select data-pick="origin">${t.origins.map(a => `<option value="${a}" ${a === t.asn ? "selected" : ""}>AS${a} ${esc(nameOf(a).slice(0, 30))}</option>`).join("")}</select></label>`;
    $("pick").innerHTML = pick;
    $("pick").querySelectorAll("select").forEach(s => s.onchange = () => {
      const u = new URL(location.href);
      u.searchParams.set(s.dataset.pick, s.value);
      location.href = u.toString();
    });
    const rv = D.rv_servers && D.rv_servers.length;
    const K = P.filter(p => !p.possible), maybe = P.length - K.length;
    const parts = [K.length ? `${K.length} known path${K.length > 1 ? "s" : ""}; the shortest is ${hops(K[0])} AS hop${hops(K[0]) > 1 ? "s" : ""}` : "No path can be put together"];
    parts.push(`from ${D.mine} paths toward you and ${D.theirs} toward ${esc(t.prefix)}` + (rv ? ` (Route Views: ${D.mine_rv} and ${D.theirs_rv} of them)` : " (RIS)"));
    let inb = D.inbound === "own" ? "Their own route to you is visible in RIS."
      : D.inbound === "climb" ? `Their own route to you isn't visible in RIS, so their traffic's paths are worked out from their side${D.main.length ? `, through their main upstream${D.main.length > 1 ? "s" : ""} (${D.main.map(a => "AS" + a).join(", ")}) or a peer of theirs that has you among its customers` : ", through a peer of theirs that has you among its customers"}.`
      : D.theirs ? "Their own route to you isn't visible in RIS, and none of the networks in front of them would carry their traffic to you (as RIS shows them), so only your traffic's paths are shown." : "";
    if (maybe) inb += ` Also ${maybe} possible peering${maybe > 1 ? "s" : ""} (dotted): PeeringDB has you or one of your transits at an exchange with them, and RIS rarely sees a peering. A traceroute shows whether it's used.`;
    let rvs = "";
    if (rv) {
      const bad = D.rv_servers.filter(s => s.error);
      rvs = `<div style="margin-top:4px">Route Views: ${D.rv_servers.length - bad.length} of ${D.rv_servers.length} collectors answered`
        + (bad.length ? ` <span title="${esc(bad.map(s => s.server + ": " + s.error).join("\n"))}" style="text-decoration:underline dotted">(why not the others)</span>` : "") + ".</div>";
    }
    $("summary").innerHTML = `${parts.join(", ")}. ${inb}${rvs}` + (D.notes || []).map(n => `<div class="msg" style="margin:8px 0 0">${esc(n)}</div>`).join("");
    $("legend").innerHTML = [["shortest known path", "var(--accent)", ""], ["your traffic's path (traceroute from this server)", "var(--ok)", ""],
                            ["their path back to you (RIPE Atlas)", "#9b4dca", ""], ["other known paths", "var(--muted)", "opacity:.5"]]
      .concat(maybe ? [["possible peering (PeeringDB)", "none", "height:0;border-radius:0;border-top:3px dotted var(--muted)"]] : [])
      .map(([l, c, x]) => `<span><i style="background:${c};${x}"></i>${l}</span>`).join("");
  }

  // ---- the paths in use: put the traceroutes' paths on the tree -------------------------------------------------
  function place(m, flag) {
    // a traceroute's match -> {idx, len}: the known path it follows, or its own path added
    let idx = m.how === "new" ? -1 : D.paths.findIndex(p => !p.trace && !p.back && key(p.asns) === key(m.asns));
    if (idx < 0) idx = D.paths.findIndex(p => p[flag] && key(p.asns) === key(m.asns));
    if (idx < 0) { D.paths.push({ asns: m.asns, dirs: [flag === "trace" ? "out" : "in"], seen: 0, [flag]: true }); idx = D.paths.length - 1; }
    return { idx, len: m.how === "part" ? m.upto : Infinity };
  }
  function remark() {
    D.paths = BASE.map(p => Object.assign({}, p));
    const first = BASE.findIndex(p => !p.possible);   // (the shortest known path; possible peerings aren't known)
    marks = first >= 0 ? [{ color: "accent", idx: first, len: Infinity }] : [];
    if (TR && TR.match) marks.push({ color: "ok", ...place(TR.match, "trace") });
    if (BK && BK.res) for (const r of BK.res.results) if (r.match) marks.push({ color: "back", ...place(r.match, "back") });
    const any = D.paths.length > 0;
    $("wrap").hidden = !any; $("legend").hidden = !any; $("list").hidden = !any;
    if (any) { build(); draw(); table(); }
  }

  // RTT added by each network along a trace: what it takes to get out of it (the lowest RTT seen at or after its
  // last hop: later hops' packets went through it too, and a slow-answering router doesn't count) minus the network
  // before. Rough: one probe per hop here, three on RIPE Atlas.
  function latency(hs, mine) {
    const floor = []; let m = Infinity;
    for (let i = hs.length - 1; i >= 0; i--) { if (hs[i].rtt != null) m = Math.min(m, hs[i].rtt); floor[i] = m; }
    const groups = [];
    hs.forEach((h, i) => {
      if (!h.ip) return;
      const k = h.asn ? "AS" + h.asn : h.private ? "private" : "?";
      const g = groups[groups.length - 1];
      if (g && g.k === k) { g.last = i; g.n++; } else groups.push({ k, asn: h.asn, private: h.private, first: i, last: i, n: 1, ix: h.ix });
    });
    let prev = 0;
    const rows = groups.map(g => {
      const out = floor[g.last] === Infinity ? null : floor[g.last];
      const add = out == null ? null : Math.max(0, out - prev);
      if (out != null) prev = Math.max(prev, out);
      return { g, out, add };
    }).filter(r => r.out != null);
    if (rows.length < 2) return "";
    const total = Math.max(...rows.map(r => r.out), 0.1);
    return `<div class="pf-lat"><span class="muted" style="font-size:12px">Network</span><span class="muted" style="font-size:12px">where the delay builds up</span><span class="muted right" style="font-size:12px">adds</span>` + rows.map(r => {
      const who = r.g.asn ? chip(r.g.asn) + (r.g.asn === D.me ? " (you)" : "") : r.g.private ? `<span class="muted">${mine ? "your network (private addresses)" : "private addresses"}</span>` : '<span class="muted">unannounced</span>';
      return `<span>${who}${r.g.n > 1 ? ` <span class="muted" style="font-size:12px">${r.g.n} hops</span>` : ""}</span>`
        + `<span class="bar" title="${r.out.toFixed(1)} ms by its last hop"><span style="width:${(100 * r.add / total).toFixed(1)}%;margin-left:${(100 * (r.out - r.add) / total).toFixed(1)}%"></span></span>`
        + `<span class="right">+${r.add.toFixed(1)} ms</span>`;
    }).join("") + `</div><div class="muted" style="font-size:12px;margin-top:4px">Rough: from ${hs === (TR && TR.hops) ? "one probe" : "three probes"} per hop, each network's delay counted from the lowest round trip seen at or after its last hop.</div>`;
  }

  function hopsTable(hs) {
    return `<tr><th class="right">Hop</th><th>Address</th><th class="right">RTT</th><th>Network</th></tr>` + hs.map(h =>
      `<tr><td class="right">${h.ttl ?? ""}</td><td class="mono">${h.ip ? esc(h.ip) : "*"}${h.note ? " " + esc(h.note) : ""}`
      + `${h.name ? `<div class="muted" style="font-size:11.5px">${esc(h.name)}</div>` : ""}</td>`
      + `<td class="right">${h.rtt == null ? "" : h.rtt.toFixed(1) + " ms"}</td><td>`
      + (h.ix ? `<span class="badge b-info">${esc(h.ix)}</span> ${h.asn ? chip(h.asn) + ' <span class="muted">(its port there)</span>' : ""}`
         : h.asn ? chip(h.asn) : h.ip ? `<span class="muted">${h.private ? "private" : "unannounced"}</span>` : "") + "</td></tr>").join("");
  }

  const matchText = (m, who, n, hs) => {
    if (!m) return "";
    const p = D.paths[n - 1], top = markOf("accent");
    const hid = m.hidden.length ? ` It didn't see ${m.hidden.map(a => "AS" + a).join(", ")}: ${m.hidden.length > 1 ? "their" : "its"} routers didn't answer, or answered from another network's addresses.` : "";
    // a possible peering it went through: the exchange its hops were at, when PeeringDB knows the address
    const at = p.possible && (hs || []).map(h => h.ix).find(x => x && p.possible.includes(x));
    const what = p.possible ? `, the possible peering${at ? " at " + esc(at) : ""}: it's there, though RIS doesn't show it`
      : top && top.idx === n - 1 ? ", the shortest known path" : "";
    return m.how === "exact" || m.how === "gaps" ? `${who} takes <strong>path ${n}</strong>${what}.${hid}`
      : m.how === "part" ? `It stopped in AS${m.asns[m.upto - 1]}, following <strong>path ${n}</strong> that far.${hid}`
      : `That's not one of the paths RIS shows: it's added to the tree as <strong>path ${n}</strong> (dashed).${hid}`;
  };
  const markOf = (color, nth = 0) => marks.filter(m => m.color === color)[nth];

  function traceRender() {
    const box = $("trace-summary"), T = TR;
    if (!T) { box.innerHTML = `<div class="muted">Tracing the route from this server to ${esc(D.trace_to)}…</div>`; $("hops").innerHTML = ""; $("trace-latency").innerHTML = ""; return; }
    if (T.error) { box.innerHTML = `<div class="msg err">${esc(T.error)}</div>`; $("hops").innerHTML = ""; $("trace-latency").innerHTML = ""; return; }
    const last = [...T.hops].reverse().find(h => h.ip);
    let s = "";
    if (T.direct) s = `<div class="msg err">Every probe reached ${esc(T.target)} in one hop, so their TTL didn't take effect on the way out: something between this server and the internet sends the packets on itself instead of forwarding them (some container and VPN networking does). The traceroute works on a server install, or in a container with rootful networking or <code>--network host</code>.</div>`;
    else {
      if (T.asns.length) s += `<div>Networks on the way: ${pathHtml(T.asns, T.assumed || [])}${T.added_you ? ` <span class="muted">(your own routers answered from private or your upstream's addresses, so AS${D.me} is put first)</span>` : ""}</div>`;
      if (!T.from_you) s += `<div class="msg" style="margin-top:8px">This server reaches the internet from ${esc(T.egress || "an address")}${T.egress_asn ? " (AS" + T.egress_asn + ")" : ""}, not from your prefixes, so this traceroute shows that network's path rather than yours. It isn't marked on the tree.</div>`;
      else if (!T.own_space) s += `<div class="msg" style="margin-top:8px">This server reaches the internet from ${esc(T.egress || "an address")}${T.egress_asn ? `, an address of AS${T.egress_asn}` : ""}, not from one of your prefixes, so this is how traffic leaves through your upstream AS${T.asns[1]}. Your own prefixes' traffic goes the same way unless your routers send it elsewhere.</div>`;
      const mk = markOf("ok");
      if (T.match && mk) s += `<div style="margin-top:6px">${matchText(T.match, "Your traffic to them", mk.idx + 1, T.hops)}</div>`;
      if (!T.reached) s += `<div class="muted" style="margin-top:6px">${esc(T.target)} itself didn't answer${last ? `; the last hop that did is ${last.ttl}` : ""}. Many hosts and firewalls don't.</div>`;
    }
    box.innerHTML = s + `<div class="muted" style="font-size:12px;margin-top:6px">${T.mode === "icmp" ? "ICMP echo" : "UDP"} probes from this server${T.egress ? " (" + esc(T.egress) + ")" : ""}, <time data-ts="${Date.now() / 1000}"></time>.</div>`;
    if (window.localize) localize(box);
    $("trace-latency").innerHTML = T.direct ? "" : latency(T.hops, true);
    $("hops").innerHTML = hopsTable(T.hops);
  }

  async function trace() {
    TR = null; traceRender(); $("retrace").disabled = true;
    const u = query();
    TR = await api("/api/pathfinder/trace", { method: "POST", body: u });
    $("retrace").disabled = false; $("retrace").textContent = "Run again";
    if (!TR) return;
    remark(); traceRender(); history();
  }

  // ---- their way back (RIPE Atlas) ------------------------------------------------------------------------------
  function backRender() {
    const box = $("back-summary"), out = $("back-results");
    if (!BK) return;
    if (BK.info.error) { box.innerHTML = `<div class="msg err">${esc(BK.info.error)}</div>`; out.innerHTML = ""; return; }
    const I = BK.info, R = BK.res;
    let s = `RIPE Atlas measurement <a href="https://atlas.ripe.net/measurements/${I.msm}/" target="_blank" rel="noopener">#${I.msm}</a>: ${I.probes.length} probe${I.probes.length > 1 ? "s" : ""} `
      + (I.inside ? `inside ${chip(I.from_asn)}` : `in ${chip(I.from_asn)}, their main upstream (RIPE Atlas has no probe in AS${D.target.asn} itself), so this is the way back from there`)
      + ` tracing to ${esc(I.target)} (~${I.credits} credits).`;
    if (!R || !R.results.length) s += ` <span class="muted">Waiting for the results (usually 1–3 minutes)…</span>`;
    else if (!R.done) s += ` <span class="muted">${R.results.length} in, waiting for the rest…</span>`;
    box.innerHTML = s;
    if (!R) { out.innerHTML = ""; return; }
    let nth = 0;
    out.innerHTML = R.results.map(r => {
      const mk = r.match ? markOf("back", nth++) : null;
      const head = `Probe <a href="https://atlas.ripe.net/probes/${r.probe}/" target="_blank" rel="noopener">#${r.probe}</a> in AS${r.asn ?? "?"}${r.cc ? ` (${esc(r.cc)})` : ""}: `
        + (r.asns.length ? pathHtml(r.asns, r.assumed || []) + " (written from you)" : "no network answered") + (r.reached ? "" : ` <span class="muted">· didn't reach ${esc(I.target)}</span>`);
      const body = r.from_you ? (mk ? matchText(r.match, "Their traffic back to you", mk.idx + 1, r.hops) : "")
        : `<span class="muted">It didn't get as far as your network or your upstreams, so it isn't marked on the tree.</span>`;
      return `<details style="margin-top:10px" open><summary>${head}</summary><div style="margin:6px 0">${body}</div>${latency(r.hops, false)}<table style="margin-top:8px">${hopsTable(r.hops)}</table></details>`;
    }).join("");
  }

  async function back() {
    $("back-run").disabled = true;
    const info = await api("/api/pathfinder/back", { method: "POST", body: query() });
    if (!info) return;
    BK = { info, res: null };
    backRender();
    if (info.error) { $("back-run").disabled = false; return; }
    const t0 = Date.now();
    const poll = async () => {
      const res = await api(`/api/pathfinder/back/${info.msm}?${query()}`);
      if (res && !res.error) { BK.res = res; remark(); backRender(); }
      if (res && !res.error && res.done) { $("back-run").disabled = false; history(); return; }
      if (Date.now() - t0 > 12 * 60e3) { $("back-run").disabled = false; $("back-summary").insertAdjacentHTML("beforeend", ` <span class="muted">Stopped waiting; see the measurement on RIPE Atlas.</span>`); return; }
      setTimeout(poll, 10000);
    };
    setTimeout(poll, 20000);
  }

  // ---- history --------------------------------------------------------------------------------------------------
  async function history() {
    const H = await api(`/api/pathfinder/history?${query()}`);
    if (!H || H.error || !H.rows.length) { $("history").hidden = true; return; }
    const nm = a => { const n = (H.names[a] || "").split(/[ ,]/)[0]; return `AS${a}` + (n && n.toUpperCase() !== `AS${a}` ? ` <span class="muted">${esc(n.slice(0, 14))}</span>` : ""); };
    const path = v => v.path.map(nm).join(" → ") + (v.complete ? "" : " …");
    const KIND = { active: "Your traffic's path", back: "Their path back", known: "Known paths" };
    const dur = (a, b) => { const s = Math.max(0, b - a); return s < 120 ? "once" : s < 7200 ? `for ${Math.round(s / 60)} min` : s < 172800 ? `for ${Math.round(s / 3600)} h` : `for ${Math.round(s / 86400)} days`; };
    const rows = H.rows.map((r, i) => {
      const v = r.value, cur = !H.rows.slice(0, i).some(x => x.kind === r.kind);
      const what = r.kind === "known" ? `${v.paths.length} path${v.paths.length !== 1 ? "s" : ""}${v.shortest ? "; shortest " + v.shortest.split(" ").map(Number).map(nm).join(" → ") : ""}`
        : path(v) + (r.kind === "back" && v.probe_asn ? ` <span class="muted">(probe in AS${v.probe_asn})</span>` : "");
      return `<tr${cur ? "" : ' class="muted"'}><td style="white-space:nowrap"><time data-ts="${r.first}"></time></td><td style="white-space:nowrap">${dur(r.first, r.last)}${cur ? " (now)" : ""}</td>`
        + `<td>${KIND[r.kind] || r.kind}</td><td>${what}</td></tr>`;
    });
    $("history-body").innerHTML = `<table><tr><th>Since</th><th>Seen</th><th>What</th><th></th></tr>${rows.join("")}</table>`;
    if (window.localize) localize($("history-body"));
    $("history").hidden = false;
  }

  async function load() {
    const st = $("status"), u = query();
    if (root.dataset.rv) u.set("rv", "1");
    st.textContent = root.dataset.rv ? "Finding paths (and asking Route Views' collectors, which takes a little longer)…" : "Finding paths…";
    const d = await api("/api/pathfinder?" + u);
    if (!d) return;
    if (d.error) { st.className = "msg err"; st.textContent = d.error; return; }
    D = d; BASE = d.paths.slice();
    st.hidden = true;
    $("result").hidden = false;
    header(); remark(); history();
    // your traffic: a traceroute from this server, at once for an address or host name, on request for the rest
    if (D.can_trace) {
      $("trace").hidden = false;
      $("trace-title").textContent = `Your traffic to them: traceroute from this server to ${D.trace_to}`;
      if (!D.may_check) { $("retrace").hidden = true; $("trace-summary").innerHTML = '<div class="muted">Traceroutes here are limited to admins, or turned off.</div>'; }
      else if (D.target.ip) trace();
      else {
        $("retrace").textContent = `Traceroute toward ${D.trace_to}`;
        $("trace-summary").innerHTML = `<div class="muted">${esc(D.trace_to)} is just an address in their prefix: it may not answer, but the routers on the way to their network do.</div>`;
      }
    }
    // their traffic: RIPE Atlas, on request (it costs credits)
    $("back").hidden = false;
    if (!root.dataset.atlas) $("back-summary").innerHTML = '<div class="muted">This comes from a RIPE Atlas probe inside their network (or their main upstream) tracing to you. RIPE Atlas isn\'t set up here: Settings › RIPE Atlas.</div>';
    else if (!D.may_check) $("back-summary").innerHTML = '<div class="muted">RIPE Atlas traces here are limited to admins, or turned off.</div>';
    else {
      $("back-run").hidden = false;
      $("back-run").textContent = `Trace back from them (~${root.dataset.credits} credits)`;
      $("back-summary").innerHTML = `<div class="muted">A RIPE Atlas traceroute to you from probes inside AS${D.target.asn}${D.main.length ? `, or else in their main upstream (${D.main.map(a => "AS" + a).join(", ")})` : ""}: the path their traffic takes back to you.</div>`;
    }
  }

  $("canvas").addEventListener("mousemove", tip);
  $("canvas").addEventListener("mouseleave", () => { $("tip").style.display = "none"; });
  $("retrace").addEventListener("click", trace);
  $("back-run").addEventListener("click", back);
  const wo = $("watch-open");
  if (wo) wo.addEventListener("click", () => { const f = $("watch-form"); f.hidden = !f.hidden; });
  window.addEventListener("resize", () => draw());
  if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => draw());
  load();
})();
