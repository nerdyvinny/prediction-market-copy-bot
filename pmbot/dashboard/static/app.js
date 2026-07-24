/* pmbot dashboard front-end: poll /api/state, render everything. */

"use strict";

const POLL_MS = 20000;
const UPDATES_KEY = "pmbot_updates_v1";
const UPDATES_MAX = 80;

const $ = (sel) => document.querySelector(sel);

const fmtUsd = (v, signed = false) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v < 0 ? "−" : signed && v > 0 ? "+" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
};

const fmtPrice = (v) =>
  v === null || v === undefined ? "—" : v.toFixed(v < 0.01 ? 4 : 2);

const fmtShares = (v) =>
  v.toLocaleString("en-US", { maximumFractionDigits: 1 });

const shortAddr = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "—");

const fmtTime = (iso) => {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
};

const fmtClock = (iso) =>
  new Date(iso).toLocaleTimeString(undefined, {
    hour: "numeric", minute: "2-digit", second: "2-digit",
  });

const fmtStamp = (iso) => {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", second: "2-digit",
  });
};

const pnlClass = (v) =>
  v === null || v === undefined ? "" : v > 0.005 ? "pos" : v < -0.005 ? "neg" : "";

/* ---- hero ----------------------------------------------------------- */

function renderHero(m) {
  $("#open-pnl").textContent = fmtUsd(m.open_pnl, true);
  $("#open-pnl").className = `value ${pnlClass(m.open_pnl)}`;
  $("#closed-pnl").textContent = fmtUsd(m.closed_pnl, true);
  $("#closed-pnl").className = `value ${pnlClass(m.closed_pnl)}`;
  $("#strip").innerHTML =
    `Total <span class="${pnlClass(m.total_pnl)}">${fmtUsd(m.total_pnl, true)}</span>` +
    ` &nbsp;·&nbsp; ${fmtUsd(m.deployed_usd)} of ${fmtUsd(m.bankroll_usd)} deployed` +
    ` &nbsp;·&nbsp; ${m.open_positions} open position${m.open_positions === 1 ? "" : "s"}` +
    ` &nbsp;·&nbsp; ${m.fills} trade${m.fills === 1 ? "" : "s"} logged`;
}

/* ---- leaders we follow ----------------------------------------------- */

function renderLeaders(leaders) {
  const tb = $("#leaders tbody");
  $("#leaders-sub").textContent = leaders && leaders.length
    ? `${leaders.length} followed · best first` : "";
  if (!leaders || !leaders.length) {
    tb.innerHTML = `<tr><td colspan="5" class="left empty">No leaders followed yet — the bot picks them on its next rescore.</td></tr>`;
    return;
  }
  tb.innerHTML = leaders.map((l) => {
    const copied = l.copied_trades
      ? `${l.copied_trades} trade${l.copied_trades === 1 ? "" : "s"}` : "—";
    return `<tr>
      <td class="left"><a class="leader-link" href="${l.profile_url}" target="_blank" rel="noopener noreferrer"
        title="Open ${l.wallet} on Polymarket">${shortAddr(l.wallet)} ↗</a></td>
      <td>${l.score.toFixed(3)}</td>
      <td>${fmtUsd(l.exposure_usd)}</td>
      <td>${copied}</td>
      <td class="left"></td>
    </tr>`;
  }).join("");
}

/* ---- open positions -------------------------------------------------- */

function renderPositions(positions) {
  const tb = $("#positions tbody");
  $("#pos-sub").textContent = positions.length ? `${positions.length} open` : "";
  if (!positions.length) {
    tb.innerHTML = `<tr><td colspan="5" class="left empty">No open positions.</td></tr>`;
    return;
  }
  tb.innerHTML = positions.map((p) => {
    const name = p.question ||
      `<span class="mono-id">${p.market_id.slice(0, 18)}…</span>`;
    const flag = p.anomaly ? `<span class="flag">SHORT / CHECK</span>` : "";
    const sub = `${fmtShares(p.shares)} shares @ ${fmtPrice(p.avg_price)}` +
      (p.mid !== null ? ` · now ${fmtPrice(p.mid)}` : " · no quote");
    return `<tr>
      <td class="left market" title="${p.question || p.market_id}">${name}${flag}
        <span class="rowsub">${sub}</span></td>
      <td class="left">${p.outcome}</td>
      <td>${fmtUsd(p.cost_usd)}</td>
      <td>${fmtUsd(p.value_usd)}</td>
      <td class="${pnlClass(p.unrealized_usd)}">${fmtUsd(p.unrealized_usd, true)}</td>
    </tr>`;
  }).join("");
}

/* ---- copied trades: money in, and where it stands -------------------- */

function standsText(t) {
  const parts = [];
  if (t.status === "open") {
    if (t.returned_usd > 0.005) parts.push(`${fmtUsd(t.returned_usd)} back`);
    parts.push(t.open_value_usd === null
      ? "in market (no quote)"
      : `worth ${fmtUsd(t.open_value_usd)} in market`);
  } else {
    parts.push(`returned ${fmtUsd(t.returned_usd)}`);
  }
  return parts.join(" · ");
}

function renderTrades(trades) {
  const tb = $("#trades tbody");
  const open = trades.filter((t) => t.status === "open").length;
  $("#trades-sub").textContent = trades.length
    ? `${trades.length} trades · ${open} open, ${trades.length - open} closed`
    : "";
  if (!trades.length) {
    tb.innerHTML = `<tr><td colspan="5" class="left empty">No trades copied yet.</td></tr>`;
    return;
  }
  tb.innerHTML = trades.map((t) => {
    const name = t.question ||
      `<span class="mono-id">${t.market_id.slice(0, 18)}…</span>`;
    const from = t.leader
      ? `copied from <span class="mono-id" title="${t.leader}">${shortAddr(t.leader)}</span>`
      : "";
    const sub = [t.outcome, from, fmtTime(t.first_ts)].filter(Boolean).join(" · ");
    return `<tr>
      <td class="left market" title="${t.question || t.market_id}">${name}
        <span class="rowsub">${sub}</span></td>
      <td>${fmtUsd(t.invested_usd)}</td>
      <td class="left">${standsText(t)}</td>
      <td class="${pnlClass(t.net_usd)}">${fmtUsd(t.net_usd, true)}</td>
      <td class="left"><span class="chip ${t.status}">${t.status === "open" ? "Open" : "Closed"}</span></td>
    </tr>`;
  }).join("");
}

/* ---- trade log -------------------------------------------------------- */

function renderFills(fills) {
  const tb = $("#fills tbody");
  $("#fills-sub").textContent = fills.length ? `${fills.length} fills, newest first` : "";
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
    const from = f.source_leader
      ? `<span class="mono-id" title="${f.source_leader}">${shortAddr(f.source_leader)}</span>`
      : (f.reason === "settlement" ? "Settlement" : "—");
    return `<tr>
      <td class="left">${fmtTime(f.ts)}</td>
      <td class="left market" title="${f.question || f.market_id}">${name}</td>
      <td class="left">${act}</td>
      <td>${fmtPrice(f.fill_price)}</td>
      <td>${fmtUsd(f.size_usd)}</td>
      <td class="left">${from}</td>
    </tr>`;
  }).join("");
}

/* ---- updates feed ------------------------------------------------------ */

function loadUpdates() {
  try {
    return JSON.parse(localStorage.getItem(UPDATES_KEY)) || [];
  } catch {
    return [];
  }
}

function logUpdate(entry) {
  const list = loadUpdates();
  list.unshift(entry);
  if (list.length > UPDATES_MAX) list.length = UPDATES_MAX;
  try {
    localStorage.setItem(UPDATES_KEY, JSON.stringify(list));
  } catch { /* storage full/unavailable: feed still renders this session */ }
  renderUpdates(list);
}

function renderUpdates(list) {
  $("#updates-sub").textContent = list.length
    ? `last ${list.length} check${list.length === 1 ? "" : "s"}` : "";
  $("#updates").innerHTML = list.length
    ? list.map((u) => `<li>
        <span class="t">${fmtStamp(u.ts)}</span>
        <span class="what">${u.what}</span>
      </li>`).join("")
    : `<li><span class="what">No updates yet.</span></li>`;
}

function describeUpdate(s, prevFills) {
  const bot = s.engine_running ? "bot running" : "bot stopped";
  if (prevFills !== null && s.summary.fills > prevFills) {
    const n = s.summary.fills - prevFills;
    return `<strong>${n} new trade${n === 1 ? "" : "s"} copied</strong> · ${bot} · ` +
      `total ${fmtUsd(s.summary.total_pnl, true)}`;
  }
  return `Updated · ${bot} · ${s.summary.open_positions} open · ` +
    `total ${fmtUsd(s.summary.total_pnl, true)}`;
}

/* ---- main loop --------------------------------------------------------- */

let lastFillCount = null;

async function refresh() {
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(res.statusText);
    const s = await res.json();

    $("#mode-badge").textContent = s.mode.toUpperCase();
    $("#mode-badge").classList.toggle("live", s.mode === "live");
    $("#engine-dot").className = `dot ${s.engine_running ? "on" : "off"}`;
    $("#engine-text").textContent = s.engine_running ? "Bot running" : "Bot stopped";
    $("#updated").textContent = `Updated ${fmtClock(s.now)}`;

    renderHero(s.summary);
    renderLeaders(s.leaders);
    renderPositions(s.positions);
    renderTrades(s.trades);
    renderFills(s.fills);

    logUpdate({ ts: s.now, what: describeUpdate(s, lastFillCount) });
    lastFillCount = s.summary.fills;
  } catch (err) {
    $("#updated").textContent = `Update failed — ${err.message}`;
    logUpdate({
      ts: new Date().toISOString(),
      what: `Update failed — ${err.message}`,
    });
  }
}

renderUpdates(loadUpdates());
refresh();
setInterval(refresh, POLL_MS);
