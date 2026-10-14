// bgpcanary front-end helpers
(function () {
  const pad = n => String(n).padStart(2, "0");
  window.fmtTs = function (ts, withSec) {
    const d = new Date(ts * 1000);
    let s = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (withSec) s += ":" + pad(d.getSeconds());
    return s;
  };
  function localize(root) {
    (root || document).querySelectorAll("time[data-ts]").forEach(el => {
      const ts = parseFloat(el.dataset.ts);
      if (!ts) return;
      el.textContent = fmtTs(ts, el.dataset.sec === "1");
      el.title = new Date(ts * 1000).toISOString().replace("T", " ").replace(/\.\d+Z/, " UTC");
    });
  }
  window.localize = localize;
  document.addEventListener("DOMContentLoaded", () => localize());

  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const PALETTE = ["#2463eb", "#e8710a", "#12a36b", "#9b4dca", "#d23f8a", "#0f9bb5", "#8a8f98"];

  // Shades outage/blip/anomaly windows behind the chart.
  const eventBands = {
    id: "eventBands",
    beforeDatasetsDraw(chart, _a, opts) {
      const evs = opts.events || [];
      const { ctx, chartArea: a, scales: { x } } = chart;
      ctx.save();
      for (const e of evs) {
        // informational notices (transit filter checks, chain changes...) are often open for days: only brief
        // dips are shaded among the info events, as the chart captions say
        if (e.severity === "info" && e.kind !== "blip") continue;
        const s = e.start_ts * 1000, en = (e.end_ts || Date.now() / 1000) * 1000;
        let x0 = x.getPixelForValue(s), x1 = x.getPixelForValue(en);
        if (x1 < a.left || x0 > a.right) continue;
        x0 = Math.max(x0, a.left); x1 = Math.min(Math.max(x1, x0 + 2), a.right);
        const col = e.severity === "critical" ? "rgba(200,38,43,.16)" : e.severity === "warning" ? "rgba(199,119,0,.16)" : "rgba(59,111,214,.10)";
        ctx.fillStyle = col;
        ctx.fillRect(x0, a.top, x1 - x0, a.bottom - a.top);
      }
      ctx.restore();
    }
  };

  function timeAxis(start, end) {
    const span = end - start;
    return {
      type: "linear", min: start * 1000, max: end * 1000,
      ticks: {
        color: css("--muted"), maxRotation: 0, autoSkip: true, maxTicksLimit: 8,
        callback: v => {
          const d = new Date(v);
          if (span > 3 * 86400) return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
          if (span > 86400) return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
          return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
        }
      },
      grid: { color: css("--border") }
    };
  }

  const charts = {};
  window.renderCharts = async function (opts) {
    const { prefix, start, end, visId, chainId, churnId } = opts;
    const q = new URLSearchParams({ prefix, start: Math.floor(start), end: Math.floor(end) });
    const r = await fetch("/api/series?" + q);
    if (r.status === 401) { location.href = "/login"; return; }
    const d = await r.json();
    const xs = d.ts.map(t => t * 1000);
    const pts = arr => arr.map((v, i) => ({ x: xs[i], y: v }));
    const tooltip = {
      callbacks: { title: items => items.length ? fmtTs(items[0].parsed.x / 1000, true) : "" }
    };
    const common = {
      responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: css("--text"), boxWidth: 12 } }, tooltip, eventBands: { events: d.events } },
    };
    const mk = (id, cfg) => {
      if (!id) return;
      if (charts[id]) charts[id].destroy();
      charts[id] = new Chart(document.getElementById(id), cfg);
    };
    mk(visId, {
      type: "line", plugins: [eventBands],
      data: {
        datasets: [
          { label: "Peers seeing prefix", data: pts(d.visible), borderColor: PALETTE[0], backgroundColor: "rgba(36,99,235,.12)", fill: true, pointRadius: 0, borderWidth: 1.5, stepped: true },
          { label: "Lowest in bucket", data: pts(d.min), borderColor: css("--crit"), pointRadius: 0, borderWidth: 1, stepped: true, borderDash: [2, 2] },
          { label: "Baseline", data: pts(d.baseline), borderColor: css("--muted"), pointRadius: 0, borderWidth: 1, borderDash: [6, 4] },
          { label: `Alert threshold (${d.threshold_pct}%)`, data: pts(d.baseline.map(b => b == null ? null : b * d.threshold_pct / 100)), borderColor: css("--warn"), pointRadius: 0, borderWidth: 1, borderDash: [3, 3] },
        ]
      },
      options: { ...common, scales: { x: timeAxis(d.start, d.end), y: { beginAtZero: true, ticks: { color: css("--muted") }, grid: { color: css("--border") } } } }
    });
    const keys = Object.keys(d.chains);
    mk(chainId, {
      type: "line", plugins: [eventBands],
      data: {
        datasets: keys.map((k, i) => ({
          label: k === "other" ? "other" : (d.chain_labels[k] || k), data: pts(d.chains[k]),
          borderColor: PALETTE[i % PALETTE.length], backgroundColor: PALETTE[i % PALETTE.length] + "55",
          fill: true, pointRadius: 0, borderWidth: 1, stepped: true, hidden: k === "other" && d.chains[k].every(v => !v)
        }))
      },
      options: { ...common, scales: { x: timeAxis(d.start, d.end), y: { stacked: true, beginAtZero: true, ticks: { color: css("--muted") }, grid: { color: css("--border") } } } }
    });
    mk(churnId, {
      type: "bar", plugins: [eventBands],
      data: {
        datasets: [
          { label: "Announcements", data: pts(d.updates), backgroundColor: PALETTE[0] },
          { label: "Withdrawals", data: pts(d.withdrawals), backgroundColor: css("--crit") },
        ]
      },
      options: { ...common, scales: { x: { ...timeAxis(d.start, d.end), stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { color: css("--muted") }, grid: { color: css("--border") } } } }
    });
    return d;
  };

  // Several prefixes at once (dashboard combined view): one visibility line per prefix as % of its own
  // baseline, in the same colors as the connectivity tree's strands; chains and churn summed.
  const PCOL = ["#2463eb", "#e8710a", "#12a36b", "#9b4dca", "#d23f8a", "#0f9bb5",
                "#b8860b", "#5c6bc0", "#6d4c41", "#00796b", "#7cb342", "#455a64"];   // same as replay.js
  window.renderMultiCharts = async function (opts) {
    const { prefixes, start, end, visId, chainId, churnId } = opts;
    const q = new URLSearchParams({ prefixes: prefixes.join(","), start: Math.floor(start), end: Math.floor(end) });
    const r = await fetch("/api/series/multi?" + q);
    if (r.status === 401) { location.href = "/login"; return; }
    const d = await r.json();
    const xs = d.ts.map(t => t * 1000);
    const pts = arr => arr.map((v, i) => ({ x: xs[i], y: v }));
    const tooltip = { callbacks: { title: items => items.length ? fmtTs(items[0].parsed.x / 1000, true) : "" } };
    const common = {
      responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: css("--text"), boxWidth: 12 } }, tooltip, eventBands: { events: d.events } },
    };
    const mk = (id, cfg) => {
      if (!id) return;
      if (charts[id]) charts[id].destroy();
      charts[id] = new Chart(document.getElementById(id), cfg);
    };
    const color = p => PCOL[prefixes.indexOf(p) % PCOL.length];
    const sets = d.lines.map(l => ({ label: l.prefix, data: pts(l.pct), borderColor: color(l.prefix), pointRadius: 0, borderWidth: 1.5, stepped: true }));
    if (d.others) sets.push({ label: `lowest of ${d.others.count} others`, data: pts(d.others.pct), borderColor: css("--muted"), pointRadius: 0, borderWidth: 1.5, stepped: true, borderDash: [4, 3] });
    sets.push({ label: `Alert threshold (${d.threshold_pct}%)`, data: xs.map(x => ({ x, y: d.threshold_pct })), borderColor: css("--warn"), pointRadius: 0, borderWidth: 1, borderDash: [3, 3] });
    const pctTip = { ...tooltip, callbacks: { ...tooltip.callbacks, label: c => c.parsed.y == null ? null : `${c.dataset.label}: ${c.parsed.y.toFixed(1)}%` } };
    mk(visId, {
      type: "line", plugins: [eventBands], data: { datasets: sets },
      options: { ...common, plugins: { ...common.plugins, tooltip: pctTip },
                 scales: { x: timeAxis(d.start, d.end), y: { min: 0, suggestedMax: 105, ticks: { color: css("--muted"), callback: v => v + "%" }, grid: { color: css("--border") } } } }
    });
    const keys = Object.keys(d.chains);
    mk(chainId, {
      type: "line", plugins: [eventBands],
      data: {
        datasets: keys.map((k, i) => ({
          label: k === "other" ? "other" : (d.chain_labels[k] || k), data: pts(d.chains[k]),
          borderColor: PALETTE[i % PALETTE.length], backgroundColor: PALETTE[i % PALETTE.length] + "55",
          fill: true, pointRadius: 0, borderWidth: 1, stepped: true, hidden: k === "other" && d.chains[k].every(v => !v)
        }))
      },
      options: { ...common, scales: { x: timeAxis(d.start, d.end), y: { stacked: true, beginAtZero: true, ticks: { color: css("--muted") }, grid: { color: css("--border") } } } }
    });
    mk(churnId, {
      type: "bar", plugins: [eventBands],
      data: {
        datasets: [
          { label: "Announcements", data: pts(d.updates), backgroundColor: PALETTE[0] },
          { label: "Withdrawals", data: pts(d.withdrawals), backgroundColor: css("--crit") },
        ]
      },
      options: { ...common, scales: { x: { ...timeAxis(d.start, d.end), stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { color: css("--muted") }, grid: { color: css("--border") } } } }
    });
    return d;
  };
})();

// Tab icon + title follow outage state, so a background tab shows trouble (checked every minute).
(function () {
  if (!document.querySelector("header")) return;   // not signed in
  const icon = document.getElementById("favicon");
  const check = async () => {
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      if (!r.ok) return;
      const down = (await r.json()).prefixes.some(p => p.state === "down");
      const href = down ? "/static/favicon-alert.svg" : "/static/favicon.svg";
      if (icon && !icon.href.endsWith(href)) icon.href = href;
      const logo = document.getElementById("brand-logo");   // header logo follows the same state
      if (logo && !logo.src.endsWith(href)) logo.src = href;
      const base = document.title.replace(/^OUTAGE · /, "");
      document.title = (down ? "OUTAGE · " : "") + base;
    } catch (e) { /* offline or restarting */ }
  };
  setInterval(check, 60000);
})();
