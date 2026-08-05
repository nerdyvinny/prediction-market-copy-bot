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

// Whole dollars, for cells too narrow to hold cents.
const fmtUsdWhole = (v) => {
  const r = Math.round(v);
  return `${r < 0 ? "−" : "+"}$${Math.abs(r).toLocaleString("en-US")}`;
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

/* Market name as a click-through to that market on polymarket.com. `url` is
   null whenever the server has no slug for the row — a market Gamma has never
   heard of, or a lookup that was still failing on this poll — so the name
   falls back to plain text rather than a link that goes nowhere. */
const marketLink = (name, url) =>
  url
    ? `<a class="mkt-link" href="${url}" target="_blank" rel="noopener noreferrer"
        ><span class="mkt-name">${name}</span><span class="mkt-arrow">↗</span></a>`
    : name;

/* ---- hero ----------------------------------------------------------- */

function renderHero(m) {
  $("#open-pnl").textContent = fmtUsd(m.open_pnl, true);
  $("#open-pnl").className = `value ${pnlClass(m.open_pnl)}`;
  $("#closed-pnl").textContent = fmtUsd(m.closed_pnl, true);
  $("#closed-pnl").className = `value ${pnlClass(m.closed_pnl)}`;

  // Win rate is only meaningful once something has actually closed.
  const rate = m.win_rate;
  $("#win-rate").textContent = rate === null || rate === undefined
    ? "—" : `${(rate * 100).toFixed(0)}%`;
  $("#win-rate").className = `value ${
    rate === null || rate === undefined ? "" : rate >= 0.5 ? "pos" : "neg"}`;
  $("#win-rate-caption").textContent = m.closed_trades
    ? `${m.wins} of ${m.closed_trades} closed trade${m.closed_trades === 1 ? "" : "s"} in profit` +
      (m.flat ? ` · ${m.flat} flat` : "")
    : "no closed trades yet";
  $("#strip").innerHTML =
    `Total <span class="${pnlClass(m.total_pnl)}">${fmtUsd(m.total_pnl, true)}</span>` +
    ` &nbsp;·&nbsp; ${fmtUsd(m.deployed_usd)} of ${fmtUsd(m.bankroll_usd)} deployed` +
    ` &nbsp;·&nbsp; ${m.open_positions} open position${m.open_positions === 1 ? "" : "s"}` +
    ` &nbsp;·&nbsp; ${m.fills} trade${m.fills === 1 ? "" : "s"} logged`;
}

/* ---- P&L calendar ------------------------------------------------------ */

/* Day buckets are keyed in *local* time so a cell lines up with the day the
   user actually watched the bot trade — the rest of the page prints local
   clock times too. Fills carry `net_realized_usd` from the server (realized
   P&L minus that fill's fee), so a month's cells sum to the closed P&L. */

const dayKey = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate()).padStart(2, "0")}`;

function bucketDays(fills) {
  const days = new Map();
  for (const f of fills) {
    const d = new Date(f.ts);
    const k = dayKey(d);
    let b = days.get(k);
    if (!b) days.set(k, (b = { pnl: 0, buys: 0, sells: 0, fills: 0 }));
    b.pnl += f.net_realized_usd || 0;
    b.fills += 1;
    if (f.side === "BUY") b.buys += 1; else b.sells += 1;
  }
  return days;
}

// null = "follow the data": pinned to the newest month with activity until the
// user clicks an arrow, after which their choice sticks across polls.
let calMonth = null;

function monthLabel(y, m) {
  return new Date(y, m, 1).toLocaleString(undefined, {
    month: "long", year: "numeric",
  });
}

function renderCalendar(fills) {
  const days = bucketDays(fills);

  if (calMonth === null) {
    const latest = fills.length
      ? new Date(Math.max(...fills.map((f) => new Date(f.ts).getTime())))
      : new Date();
    calMonth = { y: latest.getFullYear(), m: latest.getMonth() };
  }
  const { y, m } = calMonth;

  const first = new Date(y, m, 1);
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  const todayKey = dayKey(new Date());

  let monthPnl = 0, monthFills = 0, monthBuys = 0, activeDays = 0;
  for (let d = 1; d <= daysInMonth; d++) {
    const b = days.get(dayKey(new Date(y, m, d)));
    if (!b) continue;
    monthPnl += b.pnl; monthFills += b.fills; monthBuys += b.buys; activeDays += 1;
  }
  // Shade relative to the month's own biggest day so a quiet month still reads.
  let peak = 0;
  for (let d = 1; d <= daysInMonth; d++) {
    const b = days.get(dayKey(new Date(y, m, d)));
    if (b) peak = Math.max(peak, Math.abs(b.pnl));
  }

  $("#cal-month").textContent = monthLabel(y, m);
  $("#cal-sub").innerHTML = activeDays
    ? `<span class="${pnlClass(monthPnl)}">${fmtUsd(monthPnl, true)}</span>` +
      ` · ${monthBuys} entr${monthBuys === 1 ? "y" : "ies"}` +
      ` · ${monthFills} fill${monthFills === 1 ? "" : "s"} over ${activeDays} day${activeDays === 1 ? "" : "s"}`
    : "no activity this month";

  const cells = [];
  for (let i = 0; i < first.getDay(); i++) {
    cells.push(`<div class="cal-cell blank"></div>`);
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const date = new Date(y, m, d);
    const k = dayKey(date);
    const b = days.get(k);
    const today = k === todayKey ? " today" : "";
    if (!b) {
      cells.push(`<div class="cal-cell${today}"><span class="cal-d">${d}</span></div>`);
      continue;
    }
    const sign = b.pnl > 0.005 ? "pos" : b.pnl < -0.005 ? "neg" : "flat";
    // 0.12 floor: a tiny-but-real day should still be visibly not-empty.
    const alpha = peak ? (0.12 + 0.58 * Math.min(1, Math.abs(b.pnl) / peak)) : 0.12;
    const style = sign === "flat" ? "" :
      ` style="--cell-a:${alpha.toFixed(3)}"`;
    const title = `${date.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}` +
      ` — ${fmtUsd(b.pnl, true)} · ${b.buys} entries, ${b.sells} exits`;
    // Two renderings of the same numbers; CSS picks one by viewport. A phone
    // cell is ~35px wide, which ellipsises "+$18.19" down to a useless "+$…".
    cells.push(`<div class="cal-cell has ${sign}${today}"${style} title="${title}">
      <span class="cal-d">${d}</span>
      <span class="cal-pnl wide ${pnlClass(b.pnl)}">${fmtUsd(b.pnl, true)}</span>
      <span class="cal-pnl narrow ${pnlClass(b.pnl)}">${fmtUsdWhole(b.pnl)}</span>
      <span class="cal-n"><span class="wide">${b.fills} trade${b.fills === 1 ? "" : "s"}</span><span class="narrow">${b.fills}×</span></span>
    </div>`);
  }
  $("#cal-grid").innerHTML = cells.join("");

  // Don't let the user page into an empty future.
  const now = new Date();
  $("#cal-next").disabled = y > now.getFullYear() ||
    (y === now.getFullYear() && m >= now.getMonth());
}

function shiftMonth(delta) {
  if (calMonth === null) return;
  const d = new Date(calMonth.y, calMonth.m + delta, 1);
  calMonth = { y: d.getFullYear(), m: d.getMonth() };
  renderCalendar(lastState ? lastState.fills : []);
}

/* ---- leaders we follow ----------------------------------------------- */

/* Clicking a leader opens their live Polymarket book underneath the row.
   `leaderBooks` survives the 20s poll re-render so an open panel doesn't
   flicker or collapse while the user is reading it. */
const expandedLeaders = new Set();
const leaderBooks = new Map();  // wallet -> {loading, error, data}

function leaderBookHtml(wallet) {
  const st = leaderBooks.get(wallet);
  if (!st || st.loading) return `<div class="lp-note">Loading their open trades…</div>`;
  if (st.error) return `<div class="lp-note neg">Couldn't load: ${st.error}</div>`;
  const d = st.data;
  if (!d.positions.length) {
    return `<div class="lp-note">No open positions on Polymarket right now.</div>`;
  }
  const rows = d.positions.map((p) => {
    const mine = p.copied
      ? `<span class="chip ${p.our_status}">${
          p.copied_from_this_leader ? "Copied" : "Copied (other leader)"}${
          p.our_status === "open" ? "" : " · closed"}</span>`
      : `<span class="lp-skip">not copied</span>`;
    const sub = `${fmtShares(p.shares)} @ ${fmtPrice(p.avg_price)} → ${fmtPrice(p.cur_price)}` +
      (p.end_date ? ` · ends ${p.end_date}` : "") +
      (p.our_invested_usd ? ` · we put in ${fmtUsd(p.our_invested_usd)}` : "");
    const name = p.title || `<span class="mono-id">${p.market_id.slice(0, 18)}…</span>`;
    return `<tr class="${p.copied ? "lp-copied" : ""}">
      <td class="left market" title="${p.title}">${marketLink(name, p.url)}
        <span class="rowsub">${sub}</span></td>
      <td class="left">${p.outcome}</td>
      <td>${fmtUsd(p.value_usd)}</td>
      <td class="${pnlClass(p.pnl_usd)}">${fmtUsd(p.pnl_usd, true)}</td>
      <td class="left">${mine}</td>
    </tr>`;
  }).join("");
  const head = `<div class="lp-note">${d.positions.length} open · ` +
    `${d.copied_count} copied by the bot · ${fmtUsd(d.total_value_usd)} on the table` +
    (d.stale ? ` · <span class="neg">last known book</span>` : "") + `</div>`;
  return head + `<div class="table-wrap"><table class="lp-table">
    <thead><tr><th class="left">Market</th><th class="left">Side</th>
      <th>Worth now</th><th>Their P&amp;L</th><th class="left">Us</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function renderLeaders(leaders) {
  const tb = $("#leaders tbody");
  $("#leaders-sub").textContent = leaders && leaders.length
    ? `${leaders.length} followed · click one to see their open trades` : "";
  if (!leaders || !leaders.length) {
    tb.innerHTML = `<tr><td colspan="5" class="left empty">No leaders followed yet — the bot picks them on its next rescore.</td></tr>`;
    return;
  }
  tb.innerHTML = leaders.map((l) => {
    const copied = l.copied_trades
      ? `${l.copied_trades} trade${l.copied_trades === 1 ? "" : "s"}` : "—";
    const open = expandedLeaders.has(l.wallet);
    const detail = open
      ? `<tr class="lp-row"><td colspan="5" class="left">${leaderBookHtml(l.wallet)}</td></tr>`
      : "";
    return `<tr class="leader-row${open ? " open" : ""}" data-wallet="${l.wallet}"
        role="button" tabindex="0" aria-expanded="${open}">
      <td class="left"><span class="caret">${open ? "▾" : "▸"}</span>
        <span class="mono-id">${shortAddr(l.wallet)}</span></td>
      <td>${l.score.toFixed(3)}</td>
      <td>${fmtUsd(l.exposure_usd)}</td>
      <td>${copied}</td>
      <td class="left"><a class="leader-link" href="${l.profile_url}" target="_blank"
        rel="noopener noreferrer" title="Open ${l.wallet} on Polymarket">Polymarket ↗</a></td>
    </tr>${detail}`;
  }).join("");
}

async function loadLeaderBook(wallet) {
  leaderBooks.set(wallet, { loading: true });
  renderLeaders(lastState ? lastState.leaders : []);
  try {
    const res = await fetch(`/api/leader/${wallet}/positions`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    leaderBooks.set(wallet, { data: await res.json() });
  } catch (err) {
    leaderBooks.set(wallet, { error: err.message });
  }
  renderLeaders(lastState ? lastState.leaders : []);
}

function toggleLeader(wallet) {
  if (expandedLeaders.has(wallet)) {
    expandedLeaders.delete(wallet);
    renderLeaders(lastState ? lastState.leaders : []);
    return;
  }
  expandedLeaders.add(wallet);
  loadLeaderBook(wallet);  // always refetch: their book moves faster than our poll
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
      <td class="left market" title="${p.question || p.market_id}">${marketLink(name, p.url)}${flag}
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
  const closed = trades.filter((t) => t.status === "closed");
  const wins = closed.filter((t) => t.net_usd > 0.005).length;
  const losses = closed.filter((t) => t.net_usd < -0.005).length;
  $("#trades-sub").textContent = trades.length
    ? `${trades.length} trades · ${open} open, ${closed.length} closed` +
      (closed.length ? ` (${wins}W / ${losses}L)` : "")
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
      <td class="left market" title="${t.question || t.market_id}">${marketLink(name, t.url)}
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
      <td class="left market" title="${f.question || f.market_id}">${marketLink(name, f.url)}</td>
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
let lastState = null;

async function refresh() {
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(res.statusText);
    const s = await res.json();
    lastState = s;

    $("#mode-badge").textContent = s.mode.toUpperCase();
    $("#mode-badge").classList.toggle("live", s.mode === "live");
    $("#engine-dot").className = `dot ${s.engine_running ? "on" : "off"}`;
    $("#engine-text").textContent = s.engine_running ? "Bot running" : "Bot stopped";
    $("#updated").textContent = `Updated ${fmtClock(s.now)}`;

    renderHero(s.summary);
    renderCalendar(s.fills);
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

/* Delegated so handlers survive the poll re-rendering the tables. */
$("#leaders tbody").addEventListener("click", (e) => {
  if (e.target.closest("a")) return;  // let the Polymarket link through
  const row = e.target.closest(".leader-row");
  if (row) toggleLeader(row.dataset.wallet);
});

$("#leaders tbody").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const row = e.target.closest(".leader-row");
  if (!row) return;
  e.preventDefault();
  toggleLeader(row.dataset.wallet);
});

$("#cal-prev").addEventListener("click", () => shiftMonth(-1));
$("#cal-next").addEventListener("click", () => shiftMonth(1));

renderUpdates(loadUpdates());
refresh();
setInterval(refresh, POLL_MS);
