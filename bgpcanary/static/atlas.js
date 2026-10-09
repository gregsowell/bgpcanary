// RIPE Atlas views: radial traceroute map, carrier scorecard, latency/loss small multiples, hop analysis.
(function () {
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const PAL = ["#2463eb", "#e8710a", "#12a36b", "#9b4dca", "#d23f8a", "#0f9bb5", "#b58a00", "#5b6ee1",
               "#c2410c", "#0d9488", "#a21caf", "#65a30d", "#dc2626", "#0369a1"];
  const med = xs => { if (!xs.length) return null; const s = [...xs].sort((a, b) => a - b), m = s.length >> 1;
                      return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
  const pctl = (xs, q) => { if (!xs.length) return null; const s = [...xs].sort((a, b) => a - b), k = (s.length - 1) * q,
                            lo = Math.floor(k); return s[lo] + (s[Math.min(lo + 1, s.length - 1)] - s[lo]) * (k - lo); };
  const ms = v => v == null ? "–" : (v < 10 ? v.toFixed(1) : Math.round(v)) + " ms";
  const pct = v => v == null ? "–" : (v < 1 && v > 0 ? v.toFixed(1) : Math.round(v)) + "%";
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  let D, S = { group: "carrier", hl: null, hlType: null }, charts = [];

  function short(a) {
    const n = (D.names[a] || "").split(/ - |,/)[0].trim();
    return n.length > 22 ? n.slice(0, 21) + "…" : n;
  }
  const asLabel = a => a == null ? "unknown" : `AS${a}${short(a) ? " " + short(a) : ""}`;

  // ---------------------------------------------------------------- grouping
  function groupOf(pid) {
    const t = D.traceBy[pid], p = D.probes[pid] || {};
    if (S.group === "region") return p.region || "Other";
    if (S.group === "probe_asn") return p.asn ? asLabel(p.asn) : "unknown";
    if (!t) return "no traceroute yet";
    const a = S.group === "up" ? t.up : t.carrier;
    if (a != null) return t.path[0] === a ? `inside ${asLabel(a)}` : asLabel(a);
    if (t.reached) return t.path[0] === D.dest_asn ? "inside the target's network"
      : S.group === "up" ? "upstream unclear" : "reached (path unclear)";
    return t.last_asn ? `not reaching (stops in ${asLabel(t.last_asn)})` : "not reaching";
  }

  // ---------------------------------------------------------------- colors per ASN
  function asnColors() {
    const count = {};
    for (const t of D.traces) for (const a of new Set(t.path)) count[a] = (count[a] || 0) + 1;
    const order = Object.keys(count).map(Number).filter(a => a !== D.dest_asn).sort((a, b) => count[b] - count[a]);
    const col = {};
    order.forEach((a, i) => col[a] = i < PAL.length ? PAL[i] : "#8a8f98");
    col[D.dest_asn] = css("--ok");
    D.asnCount = count; D.asnOrder = [D.dest_asn, ...order].filter(a => count[a]); D.col = col;
  }
  const colorOf = a => a == null ? (css("--muted") || "#8a8f98") : (D.col[a] || "#8a8f98");

  // ---------------------------------------------------------------- radial traceroute map
  function drawRadial(cv = document.getElementById("radial"), tip = document.getElementById("rtip"), compact = false) {
    const wrap = cv.parentElement;
    // wider than tall, so the group labels around the ring have room on the left and right (compact: no labels)
    const W = Math.max(280, Math.min(1000, wrap.clientWidth || 720)), H = Math.min(compact ? 420 : 720, W), dpr = window.devicePixelRatio || 1;
    cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + "px"; cv.style.height = H + "px";
    const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    const cx = W / 2, cy = H / 2, R = W < 500 || compact ? H / 2 - 30 : Math.min(H / 2 - 40, W / 2 - 205), r0 = 26;
    const tr = [...D.traces].sort((a, b) => groupOf(a.prb).localeCompare(groupOf(b.prb)) || a.prb - b.prb);
    const lastResp = t => { let k = -1; t.hops.forEach((h, i) => { if (h.ip) k = i; }); return k; };
    const maxHop = Math.max(4, ...tr.map(t => (t.hops[lastResp(t)] || {}).h || 1));
    const step = (R - r0) / maxHop, N = tr.length || 1, dots = [];
    const dim = t => {
      if (!S.hl) return false;
      if (S.hlType === "asn") return !t.path.includes(S.hl);
      return groupOf(t.prb) !== S.hl;
    };
    const muted = css("--muted"), text = css("--text"), crit = css("--crit"), border = css("--border");
    // hop rings
    g.strokeStyle = border; g.lineWidth = 1;
    for (let h = 5; h <= maxHop; h += 5) { g.beginPath(); g.arc(cx, cy, R - (h - 1) * step, 0, Math.PI * 2); g.stroke(); }
    g.fillStyle = muted; g.font = "10px system-ui"; g.textAlign = "center";
    for (let h = 5; h <= maxHop; h += 5) g.fillText(`hop ${h}`, cx, cy - (R - (h - 1) * step) - 3);
    tr.forEach((t, i) => {
      const ang = -Math.PI / 2 + (i + .5) * 2 * Math.PI / N, ca = Math.cos(ang), sa = Math.sin(ang);
      const pos = h => [cx + ca * (R - (h - 1) * step), cy + sa * (R - (h - 1) * step)];
      const k = lastResp(t), faded = dim(t);
      g.globalAlpha = faded ? .12 : 1;
      // spoke
      const end = t.reached ? [cx + ca * r0, cy + sa * r0] : (k >= 0 ? pos(t.hops[k].h) : pos(1));
      g.strokeStyle = t.reached ? border : crit; g.lineWidth = t.reached ? 1 : 1.2;
      g.beginPath(); g.moveTo(cx + ca * (R + 6), cy + sa * (R + 6)); g.lineTo(end[0], end[1]); g.stroke();
      // hops
      t.hops.slice(0, k + 1).forEach(h => {
        const [x, y] = pos(h.h);
        if (h.ip) {
          g.fillStyle = colorOf(h.asn); g.beginPath(); g.arc(x, y, W < 500 ? 2.5 : 3.5, 0, Math.PI * 2); g.fill();
          if (!faded) dots.push({ x, y, t, h });
        } else { g.fillStyle = muted; g.fillRect(x - 1, y - 1, 2, 2); }
      });
      if (!t.reached) {
        const [x, y] = end, s = 5; g.strokeStyle = crit; g.lineWidth = 2;
        g.beginPath(); g.moveTo(x - s, y - s); g.lineTo(x + s, y + s); g.moveTo(x + s, y - s); g.lineTo(x - s, y + s); g.stroke();
        if (!faded) dots.push({ x, y, t, h: null });
      }
      // probe marker
      g.fillStyle = text; g.beginPath(); g.arc(cx + ca * (R + 8), cy + sa * (R + 8), 2, 0, Math.PI * 2); g.fill();
      if (!faded) dots.push({ x: cx + ca * (R + 8), y: cy + sa * (R + 8), t, h: "probe" });
      g.globalAlpha = 1;
    });
    // group arcs + labels
    const groups = [];
    tr.forEach((t, i) => { const k = groupOf(t.prb); if (!groups.length || groups[groups.length - 1].k !== k) groups.push({ k, a: i, b: i }); else groups[groups.length - 1].b = i; });
    groups.forEach(gr => {
      const a0 = -Math.PI / 2 + gr.a * 2 * Math.PI / N, a1 = -Math.PI / 2 + (gr.b + 1) * 2 * Math.PI / N, mid = (a0 + a1) / 2;
      g.strokeStyle = D.gcol[gr.k] || "#8a8f98"; g.lineWidth = 4; g.globalAlpha = S.hl && S.hlType === "group" && S.hl !== gr.k ? .2 : 1;
      g.beginPath(); g.arc(cx, cy, R + 16, a0 + .01, a1 - .01); g.stroke();
      if (W >= 500 && !compact && (a1 - a0) > .12) {
        const lx = cx + Math.cos(mid) * (R + 26), ly = cy + Math.sin(mid) * (R + 26);
        g.fillStyle = text; g.font = "11px system-ui"; g.textAlign = Math.cos(mid) > .2 ? "left" : Math.cos(mid) < -.2 ? "right" : "center";
        g.textBaseline = Math.sin(mid) > .5 ? "top" : Math.sin(mid) < -.5 ? "bottom" : "middle";
        const lbl = gr.k.length > 26 ? gr.k.slice(0, 25) + "…" : gr.k;
        g.fillText(`${lbl} (${gr.b - gr.a + 1})`, lx, ly);
      }
      g.globalAlpha = 1;
    });
    g.textBaseline = "alphabetic";
    // target
    g.fillStyle = css("--ok"); g.beginPath(); g.arc(cx, cy, r0 - 6, 0, Math.PI * 2); g.fill();
    g.fillStyle = "#fff"; g.font = "bold 10px system-ui"; g.textAlign = "center"; g.fillText("target", cx, cy + 3);
    if (!tr.length) { g.fillStyle = muted; g.font = "13px system-ui"; g.fillText("No traceroutes in this window yet", cx, cy + 50); }

    const data = D;   // the compare view draws several maps, swapping D between them
    cv.onmousemove = e => {
      D = data;
      const rc = cv.getBoundingClientRect(), mx = e.clientX - rc.left, my = e.clientY - rc.top;
      let best = null, bd = 64;
      for (const d of dots) { const dd = (d.x - mx) ** 2 + (d.y - my) ** 2; if (dd < bd) { bd = dd; best = d; } }
      if (!best) { tip.style.display = "none"; return; }
      const p = D.probes[best.t.prb] || {}, t = best.t;
      let s = `Probe ${t.prb} · ${p.cc || "?"} · ${asLabel(p.asn)}\n${groupOf(t.prb)}\n`;
      if (best.h === "probe") s += `${t.reached ? "reached the target" : "did NOT reach the target"} · ${fmtTs(t.ts)}\npath: ${t.path.map(a => "AS" + a).join(" → ")}` +
        (t.inferred ? `\nentered through ${asLabel(t.up)} (inferred from BGP: its routers answer from ${asLabel(t.carrier)} addresses)` : "");
      else if (best.h) s += `hop ${best.h.h}: ${best.h.ip}\n${asLabel(best.h.asn)}\nRTT ${ms(best.h.rtt)}`;
      else s += `last answer at hop ${(t.hops.filter(h => h.ip).slice(-1)[0] || {}).h || "?"}, then silence`;
      tip.textContent = s; tip.style.display = "block";
      tip.style.left = Math.min(mx + 14, cv.clientWidth - 250) + "px"; tip.style.top = (my + 14) + "px";
    };
    cv.onmouseleave = () => tip.style.display = "none";
  }

  function drawLegend() {
    const el = document.getElementById("legend");
    const rows = D.asnOrder.slice(0, 16).map(a => {
      const on = S.hlType === "asn" && S.hl === a;
      return `<div class="linklike" data-asn="${a}" style="display:flex;gap:8px;align-items:center;padding:2px 4px;border-radius:4px;${on ? "background:var(--code-bg);font-weight:600" : ""}">
        <span style="width:10px;height:10px;border-radius:50%;background:${colorOf(a)};flex:none"></span>
        <span style="color:var(--text)">${esc(asLabel(a))}${a === D.dest_asn ? " <span class='muted'>(target)</span>" : D.upstreams.includes(a) ? " <span class='muted'>(your upstream)</span>" : ""}</span>
        <span class="muted" style="margin-left:auto">${D.asnCount[a]}</span></div>`;
    }).join("");
    const nOk = D.traces.filter(t => t.reached).length;
    el.innerHTML = `<div class="muted" style="margin-bottom:6px">Networks on the paths <span style="float:right">probes</span></div>${rows || "<span class='muted'>none</span>"}
      <div class="muted" style="margin-top:10px;font-size:12px">${nOk} of ${D.traces.length} latest traceroutes reached the target.
      Gray squares are hops that didn't answer (common; routers often ignore traceroute). ${S.hl ? "<span class='linklike' data-clear='1'>Clear highlight</span>" : ""}</div>`;
    el.querySelectorAll("[data-asn]").forEach(x => x.onclick = () => highlight("asn", +x.dataset.asn));
    el.querySelectorAll("[data-clear]").forEach(x => x.onclick = () => highlight(null, null));
  }

  function highlight(type, v) {
    if (S.hlType === type && S.hl === v) { type = null; v = null; }
    S.hlType = type; S.hl = v; drawRadial(); drawLegend(); drawScore();
  }

  // ---------------------------------------------------------------- per-group aggregates
  function groupStats() {
    const gs = {}, recent = D.end - 3600;
    const G = k => gs[k] || (gs[k] = { k, probes: [], tr: 0, ok: 0, runs: 0, runsOk: 0, rtts: [], sent: 0, rcvd: 0, sentH: 0, rcvdH: 0, bins: {} });
    for (const pid of Object.keys(D.probes)) {
      const g = G(groupOf(pid)), p = D.probes[pid], t = D.traceBy[pid];
      g.probes.push(+pid); g.runs += p.runs; g.runsOk += p.ok;
      if (t) { g.tr++; g.ok += t.reached ? 1 : 0; }
      for (const [ts, mn, md, mx, sent, rcvd] of D.pings[pid] || []) {
        if (md != null) g.rtts.push(md);
        g.sent += sent; g.rcvd += rcvd;
        if (ts >= recent) { g.sentH += sent; g.rcvdH += rcvd; }
        const b = g.bins[ts] || (g.bins[ts] = { md: [], sent: 0, rcvd: 0 });
        if (md != null) b.md.push(md); b.sent += sent; b.rcvd += rcvd;
      }
    }
    return Object.values(gs).sort((a, b) => b.probes.length - a.probes.length || a.k.localeCompare(b.k));
  }

  function drawStats(all) {
    const tr = D.traces, ok = tr.filter(t => t.reached).length;
    const rtts = all.flatMap(g => g.rtts), sent = all.reduce((s, g) => s + g.sent, 0), rcvd = all.reduce((s, g) => s + g.rcvd, 0);
    const card = (l, v, s, bad) => `<div class="panel stat"><div class="label">${l}</div><div class="value" style="${bad ? "color:var(--crit)" : ""}">${v}</div><div class="sub">${s}</div></div>`;
    const reachPct = tr.length ? ok * 100 / tr.length : null, loss = sent ? (1 - rcvd / sent) * 100 : null;
    document.getElementById("stats").innerHTML =
      card("Probes", Object.keys(D.probes).length, D.demo ? "sample data (k.root-servers.net)" : `toward ${esc(D.target)}${D.label ? " · " + esc(D.label) : ""}`) +
      card("Reaching the target", tr.length ? `${ok} / ${tr.length}` : "–", tr.length ? `${pct(reachPct)} of latest traceroutes` : "no traceroutes yet", reachPct != null && reachPct < 90) +
      card("Median latency", ms(med(rtts)), `90th percentile ${ms(pctl(rtts, .9))}`) +
      card("Packet loss", pct(loss), sent ? `${(sent - rcvd).toLocaleString()} of ${sent.toLocaleString()} pings lost` : "no pings yet", loss != null && loss > 2);
  }

  function drawScore() {
    const all = groupStats();
    const lossCell = (s, r) => { if (!s) return "<td class='muted'>–</td>"; const l = (1 - r / s) * 100;
      return `<td style="${l > 2 ? "color:var(--crit);font-weight:600" : ""}">${pct(l)}</td>`; };
    const rows = all.map(g => {
      const reach = g.tr ? g.ok * 100 / g.tr : null, on = S.hlType === "group" && S.hl === g.k;
      return `<tr class="linklike" data-g="${esc(g.k)}" style="${on ? "background:var(--code-bg)" : ""}">
        <td><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${D.gcol[g.k]};margin-right:6px"></span><span style="color:var(--text)">${esc(g.k)}</span></td>
        <td>${g.probes.length}</td>
        <td style="${reach != null && reach < 90 ? "color:var(--crit);font-weight:600" : ""}">${g.tr ? `${g.ok}/${g.tr}` : "–"}</td>
        <td>${g.runs ? pct(g.runsOk * 100 / g.runs) : "–"}</td>
        <td>${ms(med(g.rtts))}</td><td>${ms(pctl(g.rtts, .9))}</td>
        ${lossCell(g.sent, g.rcvd)}${lossCell(g.sentH, g.rcvdH)}</tr>`;
    }).join("");
    const el = document.getElementById("score");
    el.innerHTML = `<tr><th>Group</th><th>Probes</th><th>Reached (latest)</th><th>Reached (window)</th><th>Median RTT</th><th>p90 RTT</th><th>Loss</th><th>Loss, last hour</th></tr>${rows}`;
    el.querySelectorAll("[data-g]").forEach(x => x.onclick = () => highlight("group", x.dataset.g));
    return all;
  }

  // ---------------------------------------------------------------- small multiples
  function drawMultiples(all) {
    charts.forEach(c => c.destroy()); charts = [];
    const el = document.getElementById("multiples");
    const withPings = all.filter(g => g.sent);
    if (!withPings.length) { el.innerHTML = "<p class='muted'>No ping results in this window yet.</p>"; return; }
    const total = { k: "All probes", bins: {} };
    for (const g of withPings) for (const [ts, b] of Object.entries(g.bins)) {
      const t = total.bins[ts] || (total.bins[ts] = { md: [], sent: 0, rcvd: 0 }); t.md.push(...b.md); t.sent += b.sent; t.rcvd += b.rcvd;
    }
    const list = [total, ...withPings.slice(0, 11)];
    // one shared scale, set by the typical slow probes rather than a single spike (spikes are clipped)
    const all95 = pctl(list.slice(1).flatMap(g => Object.values(g.bins).flatMap(b => b.md)), .98) || 10;
    const stepY = [5, 10, 20, 25, 50, 100, 200, 250, 500, 1000].find(x => all95 * 1.25 / x <= 5) || 1000;
    const ymax = Math.max(10, Math.ceil(all95 * 1.25 / stepY) * stepY);
    el.innerHTML = list.map((g, i) => `<div style="min-width:0"><div style="font-size:13px;font-weight:600;margin-bottom:2px">${esc(g.k)}
      <span class="muted" style="font-weight:400">${g.probes ? `· ${g.probes.length} probe${g.probes.length !== 1 ? "s" : ""}` : ""}</span></div>
      <div class="chart" style="height:170px"><canvas id="mx${i}"></canvas></div></div>`).join("");
    const muted = css("--muted"), border = css("--border");
    const span = D.end - D.start, pad = n => String(n).padStart(2, "0");
    list.forEach((g, i) => {
      const ts = Object.keys(g.bins).map(Number).sort((a, b) => a - b);
      const pts = f => ts.map(t => ({ x: t * 1000, y: f(g.bins[t]) }));
      const color = i === 0 ? css("--accent") : D.gcol[g.k];
      // band: fastest..slowest probe, or 10th..90th percentile once a group has enough probes to have outliers
      const lo = xs => !xs.length ? null : xs.length >= 5 ? pctl(xs, .1) : Math.min(...xs);
      const hi = xs => !xs.length ? null : xs.length >= 5 ? pctl(xs, .9) : Math.max(...xs);
      charts.push(new Chart(document.getElementById("mx" + i), {
        type: "line", data: { datasets: [
          { type: "line", label: "slow probes", data: pts(b => hi(b.md)), borderWidth: 0, pointRadius: 0, fill: "+1", backgroundColor: color + "26", yAxisID: "y" },
          { type: "line", label: "fast probes", data: pts(b => lo(b.md)), borderWidth: 0, pointRadius: 0, fill: false, yAxisID: "y" },
          { type: "line", label: "median RTT", data: pts(b => med(b.md)), borderColor: color, borderWidth: 1.6, pointRadius: 0, yAxisID: "y", spanGaps: false },
          { type: "bar", label: "loss %", data: pts(b => b.sent ? (1 - b.rcvd / b.sent) * 100 : null), backgroundColor: "rgba(200,38,43,.65)", yAxisID: "loss", barPercentage: 1, categoryPercentage: 1 },
        ] },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
          interaction: { mode: "index", intersect: false },
          plugins: { legend: { display: false }, tooltip: { callbacks: {
            title: it => it.length ? fmtTs(it[0].parsed.x / 1000) : "",
            label: c => `${c.dataset.label}: ${c.dataset.yAxisID === "loss" ? pct(c.parsed.y) : ms(c.parsed.y)}` } } },
          scales: {
            x: { type: "linear", min: D.start * 1000, max: D.end * 1000, grid: { color: border },
                 ticks: { color: muted, maxTicksLimit: 5, maxRotation: 0, callback: v => { const d = new Date(v);
                   return span > 86400 * 2 ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())}` : `${pad(d.getHours())}:${pad(d.getMinutes())}`; } } },
            y: { min: 0, max: ymax, grid: { color: border }, ticks: { color: muted, stepSize: stepY, maxTicksLimit: 6, callback: v => v + " ms" } },
            loss: { position: "right", min: 0, max: 100, grid: { display: false }, ticks: { color: muted, maxTicksLimit: 3, callback: v => v + "%" } },
          }
        }
      }));
    });
  }

  // ---------------------------------------------------------------- hop analysis
  function drawHops() {
    const jumps = {}, stops = {};
    for (const t of D.traces) {
      const hs = t.hops.filter(h => h.ip && h.rtt != null);
      for (let i = 1; i < hs.length; i++) {
        const a = hs[i - 1].asn, b = hs[i].asn;
        if (a && b && a !== b) { const k = a + ">" + b; (jumps[k] = jumps[k] || []).push(hs[i].rtt - hs[i - 1].rtt); }
      }
      if (!t.reached) { const k = t.last_asn || 0; (stops[k] = stops[k] || []).push(t.prb); }
    }
    const jr = Object.entries(jumps).filter(([, v]) => v.length >= 2).map(([k, v]) => ({ k, n: v.length, m: med(v), p: pctl(v, .9) }))
      .sort((a, b) => b.m - a.m).slice(0, 12);
    document.getElementById("jumps").innerHTML = jr.length ? `<tr><th>From → into</th><th>Paths</th><th>Median added</th><th>p90</th></tr>` + jr.map(r => {
      const [a, b] = r.k.split(">").map(Number);
      return `<tr><td>${esc(asLabel(a))} → <strong>${esc(asLabel(b))}</strong></td><td>${r.n}</td><td>${ms(r.m)}</td><td>${ms(r.p)}</td></tr>`;
    }).join("") : "<tr><td class='muted'>No network boundary is crossed by two or more probes' latest traceroutes yet.</td></tr>";
    const sr = Object.entries(stops).sort((a, b) => b[1].length - a[1].length);
    document.getElementById("stops").innerHTML = sr.length ? `<tr><th>Last network answering</th><th>Probes</th><th>Probe IDs</th></tr>` + sr.map(([a, ps]) =>
      `<tr><td>${+a ? esc(asLabel(+a)) : "nothing answered"}</td><td>${ps.length}</td><td class="mono" style="font-size:11px">${ps.slice(0, 12).join(", ")}${ps.length > 12 ? " …" : ""}</td></tr>`).join("")
      : `<tr><td class="muted">${D.traces.length ? "Every latest traceroute reached the target." : "No traceroutes yet."}</td></tr>`;
  }

  function drawAll() {
    asnColors();
    D.gcol = {}; groupStats().forEach((g, i) => D.gcol[g.k] = PAL[i % PAL.length]);   // same color per group everywhere
    drawRadial(); drawLegend(); const all = drawScore(); drawStats(all); drawMultiples(all); drawHops();
  }

  // ---------------------------------------------------------------- compare several targets
  const TPAL = ["#2463eb", "#e8710a", "#12a36b", "#9b4dca", "#d23f8a", "#0f9bb5", "#b58a00", "#dc2626"];
  const tname = d => d.label ? `${d.label}` : d.target;

  function binsOf(d) {   // all probes together: {bin_ts: {md: [...], sent, rcvd}}
    const bins = {};
    for (const rows of Object.values(d.pings)) for (const [ts, mn, md, mx, sent, rcvd] of rows) {
      const b = bins[ts] || (bins[ts] = { md: [], sent: 0, rcvd: 0 });
      if (md != null) b.md.push(md); b.sent += sent; b.rcvd += rcvd;
    }
    return bins;
  }

  function drawCompare(C) {
    const items = C.items, recent = C.end - 3600;
    // shared colors: networks (maps) and groups (matrix) mean the same thing in every target
    const count = {};
    for (const d of items) for (const t of d.traces) for (const a of new Set(t.path)) count[a] = (count[a] || 0) + 1;
    const order = Object.keys(count).map(Number).sort((a, b) => count[b] - count[a]);
    const col = {}; order.forEach((a, i) => col[a] = i < PAL.length ? PAL[i] : "#8a8f98");
    for (const d of items) { col[d.dest_asn] = css("--ok"); }
    const gtotal = {};   // probes per group (the most any one target has; targets share their probes)
    for (const d of items) {
      D = d; const n = {};
      for (const pid of Object.keys(d.probes)) { const k = groupOf(pid); n[k] = (n[k] || 0) + 1; }
      for (const [k, v] of Object.entries(n)) gtotal[k] = Math.max(gtotal[k] || 0, v);
    }
    const gkeys = Object.keys(gtotal).sort((a, b) => gtotal[b] - gtotal[a] || a.localeCompare(b));
    const gcol = {}; gkeys.forEach((k, i) => gcol[k] = PAL[i % PAL.length]);
    for (const d of items) { d.col = col; d.gcol = gcol; d.asnCount = count; }

    // per target + per (group, target) aggregates
    const stats = items.map(d => {
      D = d;
      const st = { rtts: [], sent: 0, rcvd: 0, sentH: 0, rcvdH: 0, tr: d.traces.length, ok: d.traces.filter(t => t.reached).length, groups: {} };
      for (const pid of Object.keys(d.probes)) {
        const k = groupOf(pid), t = d.traceBy[pid];
        const g = st.groups[k] || (st.groups[k] = { n: 0, rtts: [], sent: 0, rcvd: 0, tr: 0, ok: 0 });
        g.n++; if (t) { g.tr++; g.ok += t.reached ? 1 : 0; }
        for (const [ts, mn, md, mx, sent, rcvd] of d.pings[pid] || []) {
          if (md != null) { st.rtts.push(md); g.rtts.push(md); }
          st.sent += sent; st.rcvd += rcvd; g.sent += sent; g.rcvd += rcvd;
          if (ts >= recent) { st.sentH += sent; st.rcvdH += rcvd; }
        }
      }
      return st;
    });
    const loss = (s, r) => s ? (1 - r / s) * 100 : null;
    const bad = v => v != null && v > 2 ? "color:var(--crit);font-weight:600" : "";
    const link = d => `/atlas?target=${encodeURIComponent(d.target)}`;

    document.getElementById("cmp-score").innerHTML =
      `<tr><th>Target</th><th>Group</th><th>Probes</th><th>Reached (latest)</th><th>Median RTT</th><th>p90 RTT</th><th>Loss</th><th>Loss, last hour</th><th>Mostly enters via</th></tr>` +
      items.map((d, i) => {
        const s = stats[i], reach = s.tr ? s.ok * 100 / s.tr : null;
        const top = Object.entries(s.groups).sort((a, b) => b[1].n - a[1].n).slice(0, 3)
          .map(([k, g]) => `${esc(k)} <span class="muted">(${g.n})</span>`).join(", ");
        return `<tr><td><span style="display:inline-block;width:12px;height:3px;background:${TPAL[i % TPAL.length]};vertical-align:middle;margin-right:6px"></span>
            <a href="${link(d)}"><strong>${esc(tname(d))}</strong></a>${d.label ? ` <span class="muted mono">${esc(d.target)}</span>` : ""}</td>
          <td>${esc(d.group || "")}</td><td>${Object.keys(d.probes).length}</td>
          <td style="${reach != null && reach < 90 ? "color:var(--crit);font-weight:600" : ""}">${s.tr ? `${s.ok}/${s.tr}` : "–"}</td>
          <td>${ms(med(s.rtts))}</td><td>${ms(pctl(s.rtts, .9))}</td>
          <td style="${bad(loss(s.sent, s.rcvd))}">${pct(loss(s.sent, s.rcvd))}</td><td style="${bad(loss(s.sentH, s.rcvdH))}">${pct(loss(s.sentH, s.rcvdH))}</td>
          <td style="font-size:12px">${top || "–"}</td></tr>`;
      }).join("");

    // overlay charts
    charts.forEach(c => c.destroy()); charts = [];
    const muted = css("--muted"), border = css("--border"), span = C.end - C.start, pad = n => String(n).padStart(2, "0");
    const bins = items.map(binsOf);
    const xAxis = { type: "linear", min: C.start * 1000, max: C.end * 1000, grid: { color: border },
      ticks: { color: muted, maxTicksLimit: 7, maxRotation: 0, callback: v => { const d = new Date(v);
        return span > 86400 * 2 ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())}` : `${pad(d.getHours())}:${pad(d.getMinutes())}`; } } };
    const line = f => items.map((d, i) => {
      const ts = Object.keys(bins[i]).map(Number).sort((a, b) => a - b);
      return { label: tname(d), data: ts.map(t => ({ x: t * 1000, y: f(bins[i][t]) })), borderColor: TPAL[i % TPAL.length],
               backgroundColor: TPAL[i % TPAL.length], borderWidth: 1.6, pointRadius: 0, spanGaps: false };
    });
    const opts = (yfmt, ymax) => ({ responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: css("--text"), boxWidth: 12 } },
                 tooltip: { callbacks: { title: it => it.length ? fmtTs(it[0].parsed.x / 1000) : "", label: c => `${c.dataset.label}: ${yfmt(c.parsed.y)}` } } },
      scales: { x: xAxis, y: { min: 0, max: ymax, grid: { color: border }, ticks: { color: muted, callback: v => yfmt(v) } } } });
    charts.push(new Chart(document.getElementById("cmp-rtt"), { type: "line", data: { datasets: line(b => med(b.md)) }, options: opts(ms) }));
    charts.push(new Chart(document.getElementById("cmp-loss"), { type: "line",
      data: { datasets: line(b => b.sent ? (1 - b.rcvd / b.sent) * 100 : null) }, options: opts(pct, undefined) }));

    // carrier x target matrix
    const cell = g => {
      if (!g) return `<td class="muted">–</td>`;
      const l = loss(g.sent, g.rcvd), fail = g.tr && g.ok < g.tr;
      const red = Math.min(.35, ((l || 0) / 10) * .35 + (fail ? .2 : 0));
      return `<td style="background:rgba(200,38,43,${red.toFixed(2)});white-space:nowrap">${ms(med(g.rtts))} · ${pct(l)}
        <span class="muted" style="font-size:11px">${g.tr ? `${g.ok}/${g.tr} reached` : ""}</span></td>`;
    };
    const matrix = document.getElementById("cmp-matrix");   // the dashboard's Atlas summary has no matrix or maps
    if (!matrix) return;
    matrix.innerHTML =
      `<tr><th>${esc(document.getElementById("groupby").selectedOptions[0].text)}</th>${items.map((d, i) =>
        `<th style="white-space:nowrap"><span style="display:inline-block;width:12px;height:3px;background:${TPAL[i % TPAL.length]};vertical-align:middle;margin-right:4px"></span>${esc(tname(d))}${d.group ? ` <span class="muted">[${esc(d.group)}]</span>` : ""}</th>`).join("")}</tr>` +
      gkeys.map(k => `<tr><td><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${gcol[k]};margin-right:6px"></span>${esc(k)}
        <span class="muted">(${gtotal[k]})</span></td>${stats.map(s => cell(s.groups[k])).join("")}</tr>`).join("");

    // one compact traceroute map per target
    const maps = document.getElementById("cmp-maps");
    maps.innerHTML = items.map((d, i) => `<div style="min-width:0"><div style="font-size:13px;font-weight:600;margin-bottom:4px">
        <span style="display:inline-block;width:12px;height:3px;background:${TPAL[i % TPAL.length]};vertical-align:middle;margin-right:6px"></span>
        <a href="${link(d)}">${esc(tname(d))}</a>${d.group ? ` <span class="muted" style="font-weight:400">[${esc(d.group)}]</span>` : ""}
        <span class="muted" style="font-weight:400">· ${stats[i].ok}/${stats[i].tr} reached</span></div>
        <div class="rp-wrap"><canvas id="cmap${i}"></canvas><div class="rp-tip" id="ctip${i}"></div></div></div>`).join("");
    items.forEach((d, i) => { D = d; drawRadial(document.getElementById("cmap" + i), document.getElementById("ctip" + i), true); });
  }

  async function initCompare(o) {
    const q = new URLSearchParams({ targets: o.compare.join(",") });
    if (o.start) { q.set("start", Math.floor(o.start)); q.set("end", Math.floor(o.end)); } else q.set("hours", o.hours);
    const r = await fetch("/api/atlas/compare?" + q);
    if (r.status === 401) { location.href = "/login"; return; }
    const C = await r.json();
    for (const d of C.items) { d.traceBy = {}; for (const t of d.traces) d.traceBy[t.prb] = t; }
    try { S.group = localStorage.getItem("atlas.group") || S.group; } catch (e) { }
    const sel = document.getElementById("groupby");
    if (sel) {
      sel.value = S.group;
      sel.onchange = () => { S.group = sel.value; try { localStorage.setItem("atlas.group", S.group); } catch (e) { } drawCompare(C); };
    }
    if (!C.items.some(d => Object.keys(d.probes).length)) {
      document.getElementById("cmp-score").innerHTML = "<tr><td class='muted'>No RIPE Atlas results in this window yet.</td></tr>";
      return;
    }
    drawCompare(C);
    let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => drawCompare(C), 150); });
  }

  // dashboard: the side-by-side table + latency/loss charts for the baseline targets (works with one target too)
  window.initAtlasSummary = (targets, hours) => initCompare({ compare: targets, hours });

  window.initAtlas = async function (o) {
    if (o.compare && o.compare.length > 1) return initCompare(o);
    const q = new URLSearchParams({ target: o.target });
    if (o.start) { q.set("start", Math.floor(o.start)); q.set("end", Math.floor(o.end)); } else q.set("hours", o.hours);
    if (o.msm) q.set("msm", o.msm);
    const r = await fetch("/api/atlas?" + q);
    if (r.status === 401) { location.href = "/login"; return; }
    D = await r.json();
    D.traceBy = {}; for (const t of D.traces) D.traceBy[t.prb] = t;
    try { S.group = localStorage.getItem("atlas.group") || S.group; } catch (e) { }
    const sel = document.getElementById("groupby"); sel.value = S.group;
    sel.onchange = () => { S.group = sel.value; S.hl = S.hlType === "asn" ? S.hl : null; if (S.hlType === "group") S.hlType = null;
      try { localStorage.setItem("atlas.group", S.group); } catch (e) { } drawAll(); };
    drawAll();
    let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawRadial, 150); });
  };
})();
