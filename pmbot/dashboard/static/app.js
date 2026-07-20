/* pmbot dashboard front-end: poll /api/state, render everything. */

"use strict";

const POLL_MS = 20000;

const $ = (sel) => document.querySelector(sel);

const fmtUsd = (v, signed = false) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v < 0 ? "-" : signed && v > 0 ? "+" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
};

const fmtPrice = (v) =>
  v === null || v === undefined ? "—" : v.toFixed(v < 0.01 ? 4 : 3);

const fmtShares = (v) =>
  v.toLocaleString("en-US", { maximumFractionDigits: 1 });

const shortAddr = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "—");

const fmtTime = (iso) => {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
};

const pnlClass = (v) => (v > 0.005 ? "pos" : v < -0.005 ? "neg" : "");

/* ---- stat tiles ---------------------------------------------------- */

function renderStats(s) {
  const m = s.summary;
  const total = m.net_pnl + m.unrealized_pnl;
  const tiles = [
    { label: "Total P&L", value: fmtUsd(total, true), cls: pnlClass(total),
      note: "realized + open positions" },
    { label: "Realized P&L", value: fmtUsd(m.net_pnl, true), cls: pnlClass(m.net_pnl),
      note: `${fmtUsd(m.fees_usd)} fees included` },
    { label: "Unrealized P&L", value: fmtUsd(m.unrealized_pnl, true),
      cls: pnlClass(m.unrealized_pnl), note: "at current market prices" },
    { label: "Deployed", value: fmtUsd(m.deployed_usd),
      note: `of ${fmtUsd(m.bankroll_usd)} bankroll` },
    { label: "Open positions", value: m.open_positions,
      note: `${m.fills} fills total` },
    { label: "Leaders copied", value: m.leaders, note: "with open exposure" },
  ];
  $("#stats").innerHTML = tiles.map((t) => `
    <div class="tile">
      <div class="label">${t.label}</div>
      <div class="value ${t.cls || ""}">${t.value}</div>
      <div class="note">${t.note}</div>
    </div>`).join("");
}

/* ---- P&L chart ------------------------------------------------------ */

function renderChart(points) {
  const el = $("#chart");
  $("#chart-sub").textContent = points.length
    ? `cumulative, net of fees · ${points.length} fills`
    : "";
  if (points.length < 2) {
    el.innerHTML = `<div class="empty">Not enough closed trades to draw a curve yet —
      it will fill in as the bot trades.</div>`;
    return;
  }

  const W = 1000, H = 260, padL = 56, padR = 16, padT = 14, padB = 26;
  const xs = points.map((p) => new Date(p.ts).getTime());
  const ys = points.map((p) => p.net_pnl);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(0, ...ys), y1 = Math.max(0, ...ys);
  const span = (y1 - y0) || 1;
  y0 -= span * 0.1; y1 += span * 0.1;

  const X = (t) => padL + ((t - x0) / (x1 - x0 || 1)) * (W - padL - padR);
  const Y = (v) => padT + ((y1 - v) / (y1 - y0)) * (H - padT - padB);

  // step-after path: P&L only changes at a fill
  let d = `M ${X(xs[0])} ${Y(ys[0])}`;
  for (let i = 1; i < xs.length; i++)
    d += ` H ${X(xs[i])} V ${Y(ys[i])}`;

  // y ticks: 4 round-ish values
  const ticks = [];
  for (let i = 0; i <= 3; i++) ticks.push(y0 + ((y1 - y0) * i) / 3);

  const gridLines = ticks.map((v) => `
    <line x1="${padL}" x2="${W - padR}" y1="${Y(v)}" y2="${Y(v)}"
          stroke="var(--grid)" stroke-width="1"/>
    <text x="${padL - 8}" y="${Y(v) + 4}" text-anchor="end"
          fill="var(--muted)" font-size="11">${fmtUsd(v)}</text>`).join("");

  const zero = (0 >= y0 && 0 <= y1)
    ? `<line x1="${padL}" x2="${W - padR}" y1="${Y(0)}" y2="${Y(0)}"
             stroke="var(--baseline)" stroke-width="1.5"/>` : "";

  el.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Cumulative realized profit and loss">
      ${gridLines}${zero}
      <path d="${d}" fill="none" stroke="var(--series)" stroke-width="2"
            stroke-linejoin="round"/>
      ${points.map((p, i) => `
        <circle cx="${X(xs[i])}" cy="${Y(ys[i])}" r="3" fill="var(--series)"/>`).join("")}
      <text x="${padL}" y="${H - 8}" fill="var(--muted)" font-size="11">${fmtTime(points[0].ts)}</text>
      <text x="${W - padR}" y="${H - 8}" text-anchor="end" fill="var(--muted)"
            font-size="11">${fmtTime(points[points.length - 1].ts)}</text>
      <line id="xhair" y1="${padT}" y2="${H - padB}" stroke="var(--baseline)"
            stroke-width="1" style="display:none"/>
      <rect x="${padL}" y="${padT}" width="${W - padL - padR}" height="${H - padT - padB}"
            fill="transparent" id="hover-pane"/>
    </svg>
    <div class="tooltip" id="tip"></div>`;

  const svg = el.querySelector("svg");
  const pane = el.querySelector("#hover-pane");
  const xhair = el.querySelector("#xhair");
  const tip = el.querySelector("#tip");

  pane.addEventListener("mousemove", (ev) => {
    const rect = svg.getBoundingClientRect();
    const mx = ((ev.clientX - rect.left) / rect.width) * W;
    let best = 0, bd = Infinity;
    for (let i = 0; i < xs.length; i++) {
      const dd = Math.abs(X(xs[i]) - mx);
      if (dd < bd) { bd = dd; best = i; }
    }
    const px = X(xs[best]);
    xhair.setAttribute("x1", px);
    xhair.setAttribute("x2", px);
    xhair.style.display = "";
    tip.style.display = "block";
    tip.innerHTML = `${fmtTime(points[best].ts)}<br>
      <span class="v ${pnlClass(ys[best])}">${fmtUsd(ys[best], true)}</span> net`;
    const left = (px / W) * rect.width;
    tip.style.left = `${Math.min(left + 12, rect.width - tip.offsetWidth - 4)}px`;
    tip.style.top = `${((Y(ys[best]) / H) * rect.height) - 40}px`;
  });
  pane.addEventListener("mouseleave", () => {
    xhair.style.display = "none";
    tip.style.display = "none";
  });
}

/* ---- positions ------------------------------------------------------ */

function renderPositions(positions) {
  const tb = $("#positions tbody");
  $("#pos-sub").textContent = positions.length ? `${positions.length} open` : "";
  if (!positions.length) {
    tb.innerHTML = `<tr><td colspan="8" class="left empty">No open positions.</td></tr>`;
    return;
  }
  tb.innerHTML = positions
    .slice()
    .sort((a, b) => Math.abs(b.cost_usd) - Math.abs(a.cost_usd))
    .map((p) => {
      const name = p.question ||
        `<span class="mono-id">${p.market_id.slice(0, 18)}…</span>`;
      const flag = p.anomaly ? `<span class="flag">SHORT / CHECK</span>` : "";
      return `<tr>
        <td class="left market" title="${p.question || p.market_id}">${name}${flag}</td>
        <td class="left">${p.outcome}</td>
        <td>${fmtShares(p.shares)}</td>
        <td>${fmtPrice(p.avg_price)}</td>
        <td>${fmtPrice(p.mid)}</td>
        <td>${fmtUsd(p.cost_usd)}</td>
        <td>${fmtUsd(p.value_usd)}</td>
        <td class="${pnlClass(p.unrealized_usd ?? 0)}">${fmtUsd(p.unrealized_usd, true)}</td>
      </tr>`;
    }).join("");
}

/* ---- leaders --------------------------------------------------------- */

function renderLeaders(leaders) {
  const tb = $("#leaders tbody");
  const entries = Object.entries(leaders.exposure || {})
    .sort((a, b) => b[1] - a[1]);
  $("#leaders-sub").textContent =
    `auto-picks top ${leaders.top_n ?? "?"} · ${leaders.lookback_days ?? "?"}d form · ` +
    `≥${Math.round((leaders.min_win_rate ?? 0) * 100)}% win rate`;
  tb.innerHTML = entries.length
    ? entries.map(([w, usd]) => `<tr>
        <td class="left mono-id" title="${w}">${shortAddr(w)}</td>
        <td>${fmtUsd(usd)}</td>
      </tr>`).join("")
    : `<tr><td colspan="2" class="left empty">No leader exposure yet.</td></tr>`;
}

/* ---- config ----------------------------------------------------------- */

function renderConfig(cfg, mode) {
  const rows = [
    ["Mode", mode],
    ["Copy fraction", `${(cfg.copy_fraction * 100).toFixed(0)}% of leader size`],
    ["Max per market", fmtUsd(cfg.max_per_market_usd)],
    ["Max per leader", fmtUsd(cfg.max_per_leader_usd)],
    ["Entry price band", `${cfg.copy_price_min}–${cfg.copy_price_max}`],
    ["Min leader trade", fmtUsd(cfg.copy_min_leader_notional_usd)],
    ["Price drift guard", `skip if moved > ${(cfg.copy_max_price_drift * 100).toFixed(0)}¢`],
    ["Min market liquidity", fmtUsd(cfg.min_market_liquidity_usd)],
    ["Poll interval", `${cfg.poll_interval_seconds}s`],
    ["Assumed slippage", `${cfg.slippage_bps} bps`],
    ["Arbitrage strategy", cfg.arb_enabled ? "on" : "off"],
    ["Database", cfg.db_path],
  ];
  $("#config").innerHTML = rows.map(([k, v]) =>
    `<div class="row"><dt>${k}</dt><dd>${v}</dd></div>`).join("");
}

/* ---- fills ------------------------------------------------------------ */

function renderFills(fills) {
  const tb = $("#fills tbody");
  $("#fills-sub").textContent = fills.length ? `latest ${fills.length}` : "";
  if (!fills.length) {
    tb.innerHTML = `<tr><td colspan="6" class="left empty">No trades yet.</td></tr>`;
    return;
  }
  tb.innerHTML = fills.map((f) => {
    const name = f.question ||
      `<span class="mono-id">${f.market_id.slice(0, 18)}…</span>`;
    const act = f.side === "BUY"
      ? `<span class="side-buy">BUY</span> ${f.outcome}`
      : `<span class="side-sell">SELL</span> ${f.outcome}`;
    return `<tr>
      <td class="left">${fmtTime(f.ts)}</td>
      <td class="left market" title="${f.question || f.market_id}">${name}</td>
      <td class="left">${act}</td>
      <td>${fmtPrice(f.fill_price)}</td>
      <td>${fmtUsd(f.size_usd)}</td>
      <td class="left mono-id" title="${f.source_leader || ""}">${shortAddr(f.source_leader)}</td>
    </tr>`;
  }).join("");
}

/* ---- main loop --------------------------------------------------------- */

async function refresh() {
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(res.statusText);
    const s = await res.json();

    $("#mode-badge").textContent = s.mode.toUpperCase();
    $("#mode-badge").classList.toggle("live", s.mode === "live");
    $("#engine-dot").className = `dot ${s.engine_running ? "on" : "off"}`;
    $("#engine-text").textContent = s.engine_running
      ? "engine running" : "engine stopped";
    $("#updated").textContent =
      `updated ${new Date(s.now).toLocaleTimeString()}`;

    renderStats(s);
    renderChart(s.pnl_timeline);
    renderPositions(s.positions);
    renderLeaders(s.leaders);
    renderConfig(s.settings, s.mode);
    renderFills(s.fills);
  } catch (err) {
    $("#updated").textContent = `refresh failed — ${err.message}`;
  }
}

refresh();
setInterval(refresh, POLL_MS);
