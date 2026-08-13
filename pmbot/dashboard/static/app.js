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

/* Sub-cent prices are real on Polymarket, so they get four decimals — but a
   settlement at exactly 0 is a price, not a rounding artefact: "0.0000" reads
   like a glitch where "0.00" reads like a market that paid nothing. */
const fmtPrice = (v) =>
  v === null || v === undefined ? "—" : v.toFixed(v > 0 && v < 0.01 ? 4 : 2);

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
    if (!b) days.set(k, (b = { pnl: 0, buys: 0, sells: 0, fills: 0, list: [] }));
    b.pnl += f.net_realized_usd || 0;
    b.fills += 1;
    b.list.push(f);  // kept so clicking the cell can show that day's trades
    if (f.side === "BUY") b.buys += 1; else b.sells += 1;
  }
  return days;
}

// null = "follow the data": pinned to the newest month with activity until the
// user clicks an arrow, after which their choice sticks across polls.
let calMonth = null;
// Day the user clicked open, as a `dayKey`. Survives the poll so the panel
// refreshes in place instead of shutting itself while they read it.
let calDay = null;

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
    cells.push(`<div class="cal-cell has ${sign}${today}${k === calDay ? " sel" : ""}"${style}
      title="${title}" data-day="${k}" role="button" tabindex="0">
      <span class="cal-d">${d}</span>
      <span class="cal-pnl wide ${pnlClass(b.pnl)}">${fmtUsd(b.pnl, true)}</span>
      <span class="cal-pnl narrow ${pnlClass(b.pnl)}">${fmtUsdWhole(b.pnl)}</span>
      <span class="cal-n"><span class="wide">${b.fills} trade${b.fills === 1 ? "" : "s"}</span><span class="narrow">${b.fills}×</span></span>
    </div>`);
  }
  $("#cal-grid").innerHTML = cells.join("");
  renderDayDetail(days);

  // Don't let the user page into an empty future.
  const now = new Date();
  $("#cal-next").disabled = y > now.getFullYear() ||
    (y === now.getFullYear() && m >= now.getMonth());
}

function shiftMonth(delta) {
  if (calMonth === null) return;
  const d = new Date(calMonth.y, calMonth.m + delta, 1);
  calMonth = { y: d.getFullYear(), m: d.getMonth() };
  calDay = null;  // the open day isn't on screen any more
  renderCalendar(lastState ? lastState.fills : []);
}

/* ---- one day's trades -------------------------------------------------- */

/* Fills are the tape; a *trade* is all the fills on one outcome token. A day
   cell's P&L comes from that day's exits, so the panel splits the two: what
   the bot bought into that day (and where those positions stand now), and what
   closed that day (which is what actually moved the number). */

function groupDayFills(list, side) {
  const groups = new Map();
  for (const f of list) {
    if (f.side !== side) continue;
    let g = groups.get(f.token_id);
    if (!g) {
      groups.set(f.token_id, (g = {
        token_id: f.token_id, market_id: f.market_id, question: f.question,
        url: f.url, outcome: f.outcome, leader: f.source_leader,
        reason: f.reason, ts: f.ts, usd: 0, shares: 0, realized: 0, n: 0,
      }));
    }
    g.usd += f.size_usd;
    g.shares += Math.abs(f.shares);
    g.realized += f.net_realized_usd || 0;
    g.n += 1;
    if (f.ts > g.ts) g.ts = f.ts;
  }
  return [...groups.values()].sort((a, b) => (a.ts < b.ts ? 1 : -1));
}

const dayName = (g) =>
  g.question || `<span class="mono-id">${g.market_id.slice(0, 18)}…</span>`;

/* Where a position taken that day stands now: the live mid while it's open,
   and the price it left at once it's closed (a settlement pays 1.00 or 0.00). */
function nowCell(t) {
  if (!t) return `<span class="dim">—</span>`;
  if (t.status === "open") {
    return t.cur_price === null
      ? `<span class="dim">no quote</span>`
      : `${fmtPrice(t.cur_price)}<span class="rowsub">live</span>`;
  }
  return t.exit_price === null
    ? `<span class="dim">—</span>`
    : `${fmtPrice(t.exit_price)}<span class="rowsub">closed at</span>`;
}

function entriesTable(groups, trades) {
  const rows = groups.map((g) => {
    const t = trades.get(g.token_id);
    const price = g.shares ? g.usd / g.shares : null;
    const from = g.leader
      ? `copied from <span class="mono-id" title="${g.leader}">${shortAddr(g.leader)}</span>`
      : "";
    const sub = [g.outcome, from, fmtTime(g.ts)].filter(Boolean).join(" · ");
    const status = t
      ? `<span class="chip ${t.status}">${t.status === "open" ? "Open" : "Closed"}</span>`
      : "";
    return `<tr>
      <td class="left market" title="${g.question || g.market_id}">${marketLink(dayName(g), g.url)}
        <span class="rowsub">${sub}</span></td>
      <td>${fmtUsd(g.usd)}<span class="rowsub">${fmtPrice(price)} in</span></td>
      <td>${nowCell(t)}</td>
      <td class="${pnlClass(t ? t.net_usd : null)}">${fmtUsd(t ? t.net_usd : null, true)}</td>
      <td class="left">${status}</td>
    </tr>`;
  }).join("");
  return `<table class="lp-table">
    <thead><tr><th class="left">Market</th><th>Put in</th>
      <th>Price now</th><th>Net</th><th class="left">Status</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function exitsTable(groups, trades) {
  const rows = groups.map((g) => {
    const t = trades.get(g.token_id);
    const price = g.shares ? g.usd / g.shares : null;
    const how = g.reason === "settlement" ? "settled" : "sold";
    const sub = [g.outcome, `${how} at ${fmtPrice(price)}`, fmtTime(g.ts)]
      .filter(Boolean).join(" · ");
    const still = t && t.status === "open"
      ? `<span class="chip open">Part left open</span>`
      : `<span class="chip closed">Closed</span>`;
    return `<tr>
      <td class="left market" title="${g.question || g.market_id}">${marketLink(dayName(g), g.url)}
        <span class="rowsub">${sub}</span></td>
      <td>${fmtUsd(g.usd)}</td>
      <td class="${pnlClass(g.realized)}">${fmtUsd(g.realized, true)}</td>
      <td class="left">${still}</td>
    </tr>`;
  }).join("");
  return `<table class="lp-table">
    <thead><tr><th class="left">Market</th><th>Came back</th>
      <th>Booked that day</th><th class="left">Status</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderDayDetail(days) {
  const el = $("#cal-day");
  const b = calDay ? days.get(calDay) : null;
  if (!b) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  const trades = new Map(
    (lastState ? lastState.trades : []).map((t) => [t.token_id, t]));
  const [y, m, d] = calDay.split("-").map(Number);
  const label = new Date(y, m - 1, d).toLocaleDateString(undefined, {
    weekday: "long", month: "long", day: "numeric", year: "numeric",
  });
  const entries = groupDayFills(b.list, "BUY");
  const exits = groupDayFills(b.list, "SELL");

  const put = entries.reduce((s, g) => s + g.usd, 0);
  const sections = [];
  if (entries.length) {
    sections.push(`<h3 class="day-sec">Copied that day` +
      `<span class="day-secsub">${entries.length} trade${entries.length === 1 ? "" : "s"} · ${fmtUsd(put)} put in</span></h3>` +
      `<div class="table-wrap">${entriesTable(entries, trades)}</div>`);
  }
  if (exits.length) {
    sections.push(`<h3 class="day-sec">Closed that day` +
      `<span class="day-secsub">what booked the day's P&amp;L</span></h3>` +
      `<div class="table-wrap">${exitsTable(exits, trades)}</div>`);
  }

  el.hidden = false;
  el.innerHTML = `<div class="day-head">
      <div><span class="day-title">${label}</span>
        <span class="day-sub"><span class="${pnlClass(b.pnl)}">${fmtUsd(b.pnl, true)}</span>` +
        ` booked · ${b.fills} fill${b.fills === 1 ? "" : "s"}` +
        ` (${b.buys} in, ${b.sells} out)</span></div>
      <button type="button" class="day-close" id="cal-day-close" aria-label="Close">✕</button>
    </div>` + sections.join("");
}

function toggleDay(key) {
  calDay = calDay === key ? null : key;
  renderCalendar(lastState ? lastState.fills : []);
}

/* ---- leaders we follow ----------------------------------------------- */

/* Clicking a leader opens a panel under the row: their recent trades (a tape,
   newest first) or the book they're holding right now. The maps survive the
   20s poll re-render so an open panel doesn't flicker or collapse while the
   user is reading it. */
const expandedLeaders = new Set();
const leaderBooks = new Map();  // wallet -> {loading, error, data}
const leaderTapes = new Map();  // wallet -> {loading, error, data}
const leaderTab = new Map();    // wallet -> "trades" | "book"

const tabOf = (wallet) => leaderTab.get(wallet) || "trades";

/* What the bot did with a trade the leader made, as a chip. `copied` without
   `copied_from_this_leader` means we're in that market off someone else's
   signal — worth distinguishing, or the panel takes credit it hasn't earned. */
function copyChip(t) {
  if (!t.copied) return `<span class="lp-skip">not copied</span>`;
  const via = t.copied_from_this_leader ? "Copied" : "Copied (other leader)";
  return `<span class="chip ${t.our_status}">${via}${
    t.our_status === "open" ? "" : " · closed"}</span>`;
}

function leaderTapeHtml(wallet) {
  const st = leaderTapes.get(wallet);
  if (!st || st.loading) return `<div class="lp-note">Loading their recent trades…</div>`;
  if (st.error) return `<div class="lp-note neg">Couldn't load: ${st.error}</div>`;
  const d = st.data;
  if (!d.trades.length) {
    return `<div class="lp-note">No trades on record for this wallet.</div>`;
  }
  const rows = d.trades.map((t) => {
    const name = t.title || `<span class="mono-id">${t.market_id.slice(0, 18)}…</span>`;
    const act = t.side === "BUY"
      ? `<span class="side-buy">BUY</span> ${t.outcome}`
      : `<span class="side-sell">SELL</span> ${t.outcome}`;
    const sub = `${fmtShares(t.shares)} @ ${fmtPrice(t.price)}`;
    return `<tr class="${t.copied ? "lp-copied" : ""}">
      <td class="left nowrap">${fmtTime(t.ts)}</td>
      <td class="left market" title="${t.title}">${marketLink(name, t.url)}
        <span class="rowsub">${sub}</span></td>
      <td class="left">${act}</td>
      <td>${fmtUsd(t.usd_size)}</td>
      <td class="left">${copyChip(t)}</td>
    </tr>`;
  }).join("");
  const head = `<div class="lp-note">${d.trades.length} most recent fills · ` +
    `${d.copied_count} the bot copied` +
    (d.stale ? ` · <span class="neg">last known tape</span>` : "") + `</div>`;
  return head + `<div class="table-wrap lp-scroll"><table class="lp-table">
    <thead><tr><th class="left">Time</th><th class="left">Market</th>
      <th class="left">Action</th><th>Size</th><th class="left">Us</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function leaderBookHtml(wallet) {
  const st = leaderBooks.get(wallet);
  if (!st || st.loading) return `<div class="lp-note">Loading their open trades…</div>`;
  if (st.error) return `<div class="lp-note neg">Couldn't load: ${st.error}</div>`;
  const d = st.data;
  if (!d.positions.length) {
    return `<div class="lp-note">No open positions on Polymarket right now.</div>`;
  }
  const rows = d.positions.map((p) => {
    const mine = copyChip(p);
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

function leaderPanelHtml(wallet) {
  const tab = tabOf(wallet);
  const btn = (id, label) =>
    `<button type="button" class="lp-tab${tab === id ? " on" : ""}"
      data-wallet="${wallet}" data-tab="${id}">${label}</button>`;
  return `<div class="lp-tabs">${btn("trades", "Recent trades")}${
    btn("book", "Open book")}</div>` +
    (tab === "trades" ? leaderTapeHtml(wallet) : leaderBookHtml(wallet));
}

const LEADER_COLS = 7;

function renderLeaders(leaders) {
  const tb = $("#leaders tbody");
  $("#leaders-sub").textContent = leaders && leaders.length
    ? `${leaders.length} followed · click one to see their recent trades` : "";
  if (!leaders || !leaders.length) {
    tb.innerHTML = `<tr><td colspan="${LEADER_COLS}" class="left empty">No leaders followed yet — the bot picks them on its next rescore.</td></tr>`;
    return;
  }
  tb.innerHTML = leaders.map((l) => {
    // Every number in these three columns is about *our* copies of this
    // leader, not their own book — the panel below covers that.
    const copied = l.copied_trades
      ? `${l.copied_trades}<span class="rowsub">${
          l.open_trades ? `${l.open_trades} still open` : "all closed"}</span>`
      : "—";
    const rate = l.win_rate === null || l.win_rate === undefined
      ? `<span class="dim">—</span><span class="rowsub">nothing closed yet</span>`
      : `<span class="${l.win_rate >= 0.5 ? "pos" : "neg"}">${
          (l.win_rate * 100).toFixed(0)}%</span>` +
        `<span class="rowsub">${l.wins}W / ${l.losses}L${
          l.flat ? ` / ${l.flat}F` : ""}</span>`;
    const pnl = l.copied_trades
      ? `<span class="${pnlClass(l.net_usd)}">${fmtUsd(l.net_usd, true)}</span>` +
        `<span class="rowsub">${fmtUsd(l.closed_pnl_usd, true)} closed · ${
          fmtUsd(l.open_pnl_usd, true)} open</span>`
      : "—";
    const open = expandedLeaders.has(l.wallet);
    const detail = open
      ? `<tr class="lp-row"><td colspan="${LEADER_COLS}" class="left">${leaderPanelHtml(l.wallet)}</td></tr>`
      : "";
    return `<tr class="leader-row${open ? " open" : ""}" data-wallet="${l.wallet}"
        role="button" tabindex="0" aria-expanded="${open}">
      <td class="left"><span class="caret">${open ? "▾" : "▸"}</span>
        <span class="mono-id">${shortAddr(l.wallet)}</span></td>
      <td>${l.score.toFixed(3)}</td>
      <td>${fmtUsd(l.exposure_usd)}</td>
      <td>${copied}</td>
      <td>${rate}</td>
      <td>${pnl}</td>
      <td class="left"><a class="leader-link" href="${l.profile_url}" target="_blank"
        rel="noopener noreferrer" title="Open ${l.wallet} on Polymarket">Polymarket ↗</a></td>
    </tr>${detail}`;
  }).join("");
}

const relayout = () => renderLeaders(lastState ? lastState.leaders : []);

async function loadLeaderPart(wallet, tab) {
  const store = tab === "book" ? leaderBooks : leaderTapes;
  const path = tab === "book" ? "positions" : "trades";
  store.set(wallet, { loading: true });
  relayout();
  try {
    const res = await fetch(`/api/leader/${wallet}/${path}`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    store.set(wallet, { data: await res.json() });
  } catch (err) {
    store.set(wallet, { error: err.message });
  }
  relayout();
}

function toggleLeader(wallet) {
  if (expandedLeaders.has(wallet)) {
    expandedLeaders.delete(wallet);
    relayout();
    return;
  }
  expandedLeaders.add(wallet);
  // Always refetch: a leader trades faster than our 20s poll.
  loadLeaderPart(wallet, tabOf(wallet));
}

function switchLeaderTab(wallet, tab) {
  if (tabOf(wallet) === tab) return;
  leaderTab.set(wallet, tab);
  loadLeaderPart(wallet, tab);
}

/* ---- open positions -------------------------------------------------- */

/* Two orderings over the same rows: when the bot copied the trade, and when
   the market is due to resolve. Both open newest-first (the page's default is
   the latest copy at the top); clicking the active one flips the direction. */
const posSort = { key: "copied", dir: "desc" };

const POS_SORT_KEYS = { copied: "opened_ts", resolves: "resolves_at" };

const msOf = (iso) => {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : t;
};

function sortPositions(positions) {
  const field = POS_SORT_KEYS[posSort.key];
  const dir = posSort.dir === "desc" ? -1 : 1;
  return positions.slice().sort((a, b) => {
    const x = msOf(a[field]);
    const y = msOf(b[field]);
    // A row with no timestamp sinks to the bottom whichever way the sort
    // points — it isn't the oldest thing on the page, it's unknown.
    if (x === null && y === null) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    return (x - y) * dir;
  });
}

function paintSortButtons() {
  for (const btn of document.querySelectorAll("#pos-sorts .sort-btn")) {
    const on = btn.dataset.sort === posSort.key;
    btn.classList.toggle("on", on);
    btn.querySelector(".sort-arrow").textContent = on
      ? (posSort.dir === "desc" ? "↓" : "↑") : "";
    btn.setAttribute("aria-pressed", String(on));
  }
}

const POS_SORT_LABEL = {
  "copied:desc": "newest copy first",
  "copied:asc": "oldest copy first",
  "resolves:desc": "latest to resolve first",
  "resolves:asc": "soonest to resolve first",
};

/* Compact distance in time: 45m, 3h 10m, 2d 4h. */
function fmtSpan(ms) {
  const a = Math.abs(ms);
  const m = Math.floor(a / 60000) % 60;
  const h = Math.floor(a / 3600000) % 24;
  const d = Math.floor(a / 86400000);
  if (d) return h ? `${d}d ${h}h` : `${d}d`;
  if (h) return m ? `${h}h ${m}m` : `${h}h`;
  return `${Math.max(m, 1)}m`;
}

function copiedCell(iso) {
  if (!iso) return `<span class="dim">—</span>`;
  const ago = Date.now() - new Date(iso).getTime();
  return `${ago < 60000 ? "just now" : `${fmtSpan(ago)} ago`}` +
    `<span class="rowsub">${fmtTime(iso)}</span>`;
}

/* A market that is past its end date but still on our books hasn't paid out
   yet. That is normal — Polymarket can take days to publish the outcome — so
   the row says what's actually happening instead of counting down past zero. */
function resolvesCell(iso) {
  if (!iso) return `<span class="dim">unknown</span>` +
    `<span class="rowsub">no date published</span>`;
  const left = new Date(iso).getTime() - Date.now();
  if (left <= 0) {
    return `<span class="due">awaiting settlement</span>` +
      `<span class="rowsub">ended ${fmtTime(iso)}</span>`;
  }
  return `in ${fmtSpan(left)}<span class="rowsub">${fmtTime(iso)}</span>`;
}

function renderPositions(positions) {
  const tb = $("#positions tbody");
  paintSortButtons();
  $("#pos-sub").textContent = positions.length
    ? `${positions.length} open · ${POS_SORT_LABEL[`${posSort.key}:${posSort.dir}`]}`
    : "";
  if (!positions.length) {
    tb.innerHTML = `<tr><td colspan="7" class="left empty">No open positions.</td></tr>`;
    return;
  }
  tb.innerHTML = sortPositions(positions).map((p) => {
    const name = p.question ||
      `<span class="mono-id">${p.market_id.slice(0, 18)}…</span>`;
    const flag = p.anomaly ? `<span class="flag">SHORT / CHECK</span>` : "";
    const sub = `${fmtShares(p.shares)} shares @ ${fmtPrice(p.avg_price)}` +
      (p.mid !== null ? ` · now ${fmtPrice(p.mid)}` : " · no quote");
    return `<tr>
      <td class="left market" title="${p.question || p.market_id}">${marketLink(name, p.url)}${flag}
        <span class="rowsub">${sub}</span></td>
      <td class="left">${p.outcome}</td>
      <td class="left when">${copiedCell(p.opened_ts)}</td>
      <td class="left when">${resolvesCell(p.resolves_at)}</td>
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
  const tab = e.target.closest(".lp-tab");
  if (tab) {
    switchLeaderTab(tab.dataset.wallet, tab.dataset.tab);
    return;  // a tab click is not a click on the row: don't collapse the panel
  }
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

/* Re-sorts from the state already in hand, so the order changes on the click
   rather than on the next poll. */
$("#pos-sorts").addEventListener("click", (e) => {
  const btn = e.target.closest(".sort-btn");
  if (!btn) return;
  const key = btn.dataset.sort;
  if (posSort.key === key) {
    posSort.dir = posSort.dir === "desc" ? "asc" : "desc";
  } else {
    posSort.key = key;
    posSort.dir = "desc";
  }
  renderPositions(lastState ? lastState.positions : []);
});

$("#cal-prev").addEventListener("click", () => shiftMonth(-1));
$("#cal-next").addEventListener("click", () => shiftMonth(1));

/* Delegated: the grid is rebuilt every poll. */
$("#cal-grid").addEventListener("click", (e) => {
  const cell = e.target.closest(".cal-cell.has");
  if (cell) toggleDay(cell.dataset.day);
});

$("#cal-grid").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const cell = e.target.closest(".cal-cell.has");
  if (!cell) return;
  e.preventDefault();
  toggleDay(cell.dataset.day);
});

$("#cal-day").addEventListener("click", (e) => {
  if (e.target.closest("#cal-day-close")) toggleDay(calDay);
});

renderUpdates(loadUpdates());
refresh();
setInterval(refresh, POLL_MS);
