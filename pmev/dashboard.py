#!/usr/bin/env python3
"""
Local EV dashboard (multi-book).

Serves a local HTML page at http://127.0.0.1:5055 showing the top 25 EV bets
with a per-book badge and client-side book + market-type filter chips. The
scan/match/evaluate pipeline lives in engine.py; the background scanner is
event-triggered (engine.scan() re-runs on a new snapshot write — mtime advance
polled every SCAN_TRIGGER_POLL_SEC — with a DASHBOARD_REFRESH_SEC fallback) and
places each (side_row, ladder) the engine returns via paper_tracker / real_tracker.

Run: python3 ev_dashboard.py
Stop: Ctrl-C
"""
import threading
import time
import traceback
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string

from pmev import engine
from pmev.engine import TOP_N
# Re-exported so imports of these constants from ev_dashboard keep resolving.
from pmev.matching.ev import MAX_PIN_SNAPSHOT_AGE_SEC, MAX_SOFT_SNAPSHOT_AGE_SEC
from pmev.core import io as data_utils
from pmev.execution import paper as paper_tracker
from pmev.execution import real as real_tracker
from pmev import config

REFRESH_SEC = config.DASHBOARD_REFRESH_SEC
SCAN_TRIGGER_POLL_SEC = config.SCAN_TRIGGER_POLL_SEC

app = Flask(__name__)

_state = {
    "last_updated": None,
    "pin_age": None,
    "book_ages": {},
    "cross_book_skew_sec": None,
    "stats": {},
    "rows": [],
    "prop_rows": [],
    "error": None,
}
_lock = threading.Lock()


def _place_all(placements):
    """Hand each (side_row, ladder) from the engine to both trackers. Per-row
    exceptions are swallowed so one bad placement never aborts the cycle. The
    engine is side-effect free; placement is the dashboard caller's job (#2)."""
    for side_row, ladder in placements:
        try:
            paper_tracker.maybe_place(side_row, ladder)
        except Exception:
            traceback.print_exc()
        try:
            real_tracker.maybe_place(side_row, ladder)
        except Exception:
            traceback.print_exc()


def _apply_result(result):
    with _lock:
        _state["last_updated"] = datetime.now(timezone.utc).isoformat()
        _state["pin_age"] = result["pin_age"]
        _state["book_ages"] = result["book_ages"]
        _state["cross_book_skew_sec"] = result["cross_book_skew_sec"]
        _state["stats"] = result["stats"]
        _state["rows"] = result["rows"]
        _state["prop_rows"] = result["prop_rows"]
        _state["error"] = None


def _run_scan_cycle():
    """One scan + place + state-bind, with errors surfaced into _state."""
    try:
        result = engine.scan()
        _place_all(result["placements"])
        _apply_result(result)
    except Exception as e:
        with _lock:
            _state["last_updated"] = datetime.now(timezone.utc).isoformat()
            _state["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()


def _should_scan(mtime, last_mtime, elapsed, refresh_sec):
    """#1: scan when a poller wrote a new snapshot (mtime advanced) or the
    refresh fallback elapsed — the fallback guarantees a dead poller still
    refreshes the stale-snapshot error instead of freezing the dashboard."""
    if mtime is not None and mtime != last_mtime:
        return True
    return elapsed >= refresh_sec


def scanner_loop():
    """Event-triggered scan: poll the snapshot dirs every SCAN_TRIGGER_POLL_SEC
    and recompute on a new write (or the DASHBOARD_REFRESH_SEC fallback), instead
    of a fixed 60s timer (#1)."""
    last_mtime = None
    last_scan = None
    while True:
        mtime = data_utils.latest_snapshot_mtime(engine.snapshot_dirs())
        now = time.monotonic()
        elapsed = float("inf") if last_scan is None else now - last_scan
        if _should_scan(mtime, last_mtime, elapsed, REFRESH_SEC):
            _run_scan_cycle()
            last_mtime = mtime
            last_scan = now
        time.sleep(SCAN_TRIGGER_POLL_SEC)


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>+EV Dashboard</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
         background: #0e0f12; color: #e8ecf1; margin: 0; padding: 24px; }
  h1 { margin: 0 0 4px 0; font-size: 20px; }
  .meta { color: #9aa3b2; font-size: 12px; margin-bottom: 16px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #23262d; }
  th { color: #9aa3b2; font-weight: 500; background: #15171c; position: sticky; top: 0; }
  tr:hover td { background: #171a20; }
  .pos { color: #4ade80; font-weight: 600; }
  .neg { color: #f87171; }
  .muted { color: #7a8291; }
  .badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px;
           background: #2a2d34; color: #9aa3b2; margin-left: 6px; vertical-align: middle; }
  .mtype { text-transform: uppercase; letter-spacing: 0.04em; font-size: 10px;
           padding: 2px 6px; border-radius: 3px; font-weight: 600; }
  .mtype.moneyline   { background: #1e3a5f; color: #93c5fd; }
  .mtype.spread      { background: #3a2d6a; color: #c4b5fd; }
  .mtype.total       { background: #0f4a3a; color: #6ee7b7; }
  .mtype.team_total  { background: #5a3a1e; color: #fcd34d; }
  .mtype.player_prop { background: #4c1d3d; color: #f9a8d4; }
  .book { text-transform: uppercase; letter-spacing: 0.04em; font-size: 10px;
          padding: 2px 6px; border-radius: 3px; font-weight: 600; margin-left: 4px; }
  .book.kalshi     { background: #1a3524; color: #86efac; }
  .book.polymarket { background: #33194d; color: #d8b4fe; }
  .chip { display: inline-block; padding: 4px 10px; border-radius: 14px; font-size: 12px;
          background: #23262d; color: #9aa3b2; cursor: pointer; user-select: none;
          margin-right: 6px; border: 1px solid transparent; }
  .chip.active { background: #2a3a58; color: #bfdbfe; border-color: #3b5b8a; }
  .chips { margin-bottom: 8px; }
  .chips-label { color: #7a8291; font-size: 11px; text-transform: uppercase;
                 letter-spacing: 0.06em; margin-right: 8px; }
  tr.oow td { opacity: 0.55; }
  tr.hidden { display: none; }
  .mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  .err { background: #3b1010; padding: 10px 14px; border-radius: 6px; color: #fca5a5;
         margin-bottom: 12px; font-family: ui-monospace, monospace; font-size: 12px; }
  a { color: #7dd3fc; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .version { position: fixed; top: 14px; right: 18px; z-index: 10;
             font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px;
             color: #9aa3b2; background: #15171c; border: 1px solid #23262d;
             padding: 3px 8px; border-radius: 4px; }
</style>
</head>
<body>
  <div class="version">v{{version}}</div>
  <h1>+EV Dashboard <span class="muted" style="font-size:12px;">top {{top_n}} by EV/share, per-book fee model</span></h1>
  <div class="meta"><a href="/">Live EV</a> &nbsp;·&nbsp; <a href="/paper">Paper</a> &nbsp;·&nbsp; <a href="/real">Real Trading</a></div>
  <div class="meta" id="meta">loading…</div>
  <div class="chips" id="book-chips">
    <span class="chips-label">book</span>
    <span class="chip active" data-bf="all">all</span>
    <span class="chip active" data-bf="kalshi">kalshi</span>
    <span class="chip active" data-bf="polymarket">polymarket</span>
  </div>
  <div class="chips" id="chips">
    <span class="chips-label">type</span>
    <span class="chip active" data-f="all">all</span>
    <span class="chip active" data-f="moneyline">ML</span>
    <span class="chip active" data-f="spread">spread</span>
    <span class="chip active" data-f="total">total</span>
    <span class="chip active" data-f="team_total">team total</span>
    <span class="chip active" data-f="player_prop">player prop</span>
  </div>
  <div class="chips" id="side-chips">
    <span class="chips-label">side</span>
    <span class="chip active" data-sf="all">all</span>
    <span class="chip active" data-sf="yes">YES</span>
    <span class="chip active" data-sf="no">NO</span>
  </div>
  <div id="err"></div>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Market</th>
        <th>Matchup</th>
        <th>Selection</th>
        <th>Pin 2-way</th>
        <th>Fair %</th>
        <th>Book ask</th>
        <th>Start</th>
        <th>EV/share</th>
        <th>+EV size</th>
        <th>Exp. profit</th>
        <th>Market ID</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>

  <h2 style="margin:28px 0 8px 0;font-size:15px;color:#c4cbd6;">Top 5 Player Props <span class="muted" style="font-size:11px;font-weight:normal;">(tracking confirmation)</span></h2>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Market</th>
        <th>Matchup</th>
        <th>Player / Stat</th>
        <th>Selection</th>
        <th>Pin 2-way</th>
        <th>Fair %</th>
        <th>Book ask</th>
        <th>Start</th>
        <th>EV/share</th>
        <th>Market ID</th>
      </tr>
    </thead>
    <tbody id="prop-rows"></tbody>
  </table>

<script>
function pct(x) { return (x * 100).toFixed(2) + '%'; }
function money(x) { return '$' + x.toFixed(2); }
function sign(n) { return (n >= 0 ? '+' : '') + n.toFixed(0); }
function fmtStart(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const now = new Date();
  const hrs = (d - now) / 3_600_000;
  const when = d.toLocaleString(undefined,
      { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  const delta = hrs >= 0 ? ('in ' + hrs.toFixed(1) + 'h') : (Math.abs(hrs).toFixed(1) + 'h ago');
  return when + ' <span class="muted">(' + delta + ')</span>';
}

function render(data) {
  const meta = document.getElementById('meta');
  const err = document.getElementById('err');
  const tbody = document.getElementById('rows');
  err.innerHTML = '';
  if (data.error) {
    err.innerHTML = '<div class="err">' + data.error + '</div>';
  }
  const updated = data.last_updated ? new Date(data.last_updated).toLocaleTimeString() : '—';
  const s = data.stats || {};
  const bookAges = data.book_ages || {};
  const bookAgeStr = Object.keys(bookAges).sort().map(b =>
      b + ' ' + (bookAges[b] != null ? bookAges[b].toFixed(0) + 's' : '?')).join(' · ');
  const skew = (typeof data.cross_book_skew_sec === 'number')
      ? '  ·  skew ' + data.cross_book_skew_sec.toFixed(0) + 's' : '';
  meta.textContent =
      'updated ' + updated +
      '  ·  pinnacle ' + (data.pin_age ? data.pin_age.toFixed(0) : '?') + 's' +
      (bookAgeStr ? '  ·  ' + bookAgeStr : '') +
      skew +
      '  ·  in-scope ' + (s.soft_in_scope || 0) +
      '  ·  matched ' + (s.matched || 0) +
      ' (ML ' + (s.matched_moneyline || 0) +
      ' / SP ' + (s.matched_spread || 0) +
      ' / TO ' + (s.matched_total || 0) +
      ' / TT ' + (s.matched_team_total || 0) +
      ' / PROP ' + (s.matched_player_prop || 0) + ')';

  tbody.innerHTML = '';
  (data.rows || []).forEach((r, i) => {
    const tr = document.createElement('tr');
    tr.dataset.mtype = r.market_type || 'moneyline';
    tr.dataset.book = r.book || '';
    tr.dataset.side = r.side || 'yes';
    if (!r.in_window) tr.classList.add('oow');
    const evClass = r.ev_per_share > 0 ? 'pos' : 'neg';
    const winBadge = r.in_window ? '' : '<span class="badge">outside 0.5–3h</span>';
    const mtype = r.market_type || 'moneyline';
    const mtypeLabel = mtype === 'moneyline' ? 'ML' :
                       mtype === 'team_total' ? 'TT' :
                       mtype === 'player_prop' ? 'PROP' :
                       mtype.slice(0,3).toUpperCase();
    const bookBadge = r.book ?
        '<span class="book ' + r.book + '">' + r.book + '</span>' : '';
    const periodTag = (r.period_label && r.period_label !== 'FULL') ?
                      ' <span class="muted">' + r.period_label + '</span>' : '';
    const evCell = (r.ev_per_share >= 0 ? '+' : '') + (r.ev_per_share).toFixed(4) +
                   ' <span class="muted">(' +
                   (r.ev_pct_at_best >= 0 ? '+' : '') + r.ev_pct_at_best.toFixed(2) + '%)</span>';
    const posSize = r.pos_shares > 0
        ? r.pos_shares.toLocaleString() + ' sh <span class="muted">@ ' + money(r.pos_stake) + '</span>'
        : '<span class="muted">—</span>';
    const expProfit = r.pos_expected_profit > 0
        ? '<span class="pos">' + money(r.pos_expected_profit) + '</span>'
        : '<span class="muted">—</span>';
    const url = r.market_url || '#';
    tr.innerHTML =
        '<td>' + (i+1) + '</td>' +
        '<td><span class="mtype ' + mtype + '">' + mtypeLabel + '</span>' + bookBadge + periodTag + '</td>' +
        '<td>' + r.pin_matchup + winBadge + '<br><span class="muted" style="font-size:11px;">' + r.title + '</span></td>' +
        '<td>' + r.selection + '</td>' +
        '<td class="mono">' + sign(r.yes_side_price) + '/' + sign(r.opposite_side_price) +
           ' <span class="muted">(vig ' + r.vig_pct.toFixed(1) + '%)</span></td>' +
        '<td class="mono">' + (r.fair_prob * 100).toFixed(2) + '%</td>' +
        '<td class="mono">' + sign(r.book_ask_american) + ' (' + (r.book_ask * 100).toFixed(1) + '¢)' +
           ' <span class="muted">x' + r.book_depth.toLocaleString() + '</span></td>' +
        '<td class="mono">' + fmtStart(r.pin_start_time) + '</td>' +
        '<td class="mono ' + evClass + '">' + evCell + '</td>' +
        '<td class="mono">' + posSize + '</td>' +
        '<td class="mono">' + expProfit + '</td>' +
        '<td class="mono" style="font-size:11px;"><a href="' + url + '" target="_blank">' + r.market_id + '</a></td>';
    tbody.appendChild(tr);
  });
  if (!data.rows || data.rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="12" class="muted" style="padding:24px;text-align:center;">no matched markets</td></tr>';
  }

  const propBody = document.getElementById('prop-rows');
  propBody.innerHTML = '';
  (data.prop_rows || []).forEach((r, i) => {
    const tr = document.createElement('tr');
    if (!r.in_window) tr.classList.add('oow');
    const evClass = r.ev_per_share > 0 ? 'pos' : 'neg';
    const winBadge = r.in_window ? '' : '<span class="badge">outside 0.5–3h</span>';
    const bookBadge = r.book ?
        '<span class="book ' + r.book + '">' + r.book + '</span>' : '';
    const periodTag = (r.period_label && r.period_label !== 'FULL') ?
                      ' <span class="muted">' + r.period_label + '</span>' : '';
    const evCell = (r.ev_per_share >= 0 ? '+' : '') + (r.ev_per_share).toFixed(4) +
                   ' <span class="muted">(' +
                   (r.ev_pct_at_best >= 0 ? '+' : '') + r.ev_pct_at_best.toFixed(2) + '%)</span>';
    const playerStat = (r.player || '—') + ' <span class="muted">· ' + (r.stat || '—') + '</span>';
    const url = r.market_url || '#';
    tr.innerHTML =
        '<td>' + (i+1) + '</td>' +
        '<td><span class="mtype player_prop">PROP</span>' + bookBadge + periodTag + '</td>' +
        '<td>' + r.pin_matchup + winBadge + '</td>' +
        '<td>' + playerStat + '</td>' +
        '<td>' + r.selection + '</td>' +
        '<td class="mono">' + sign(r.yes_side_price) + '/' + sign(r.opposite_side_price) +
           ' <span class="muted">(vig ' + r.vig_pct.toFixed(1) + '%)</span></td>' +
        '<td class="mono">' + (r.fair_prob * 100).toFixed(2) + '%</td>' +
        '<td class="mono">' + sign(r.book_ask_american) + ' (' + (r.book_ask * 100).toFixed(1) + '¢)' +
           ' <span class="muted">x' + r.book_depth.toLocaleString() + '</span></td>' +
        '<td class="mono">' + fmtStart(r.pin_start_time) + '</td>' +
        '<td class="mono ' + evClass + '">' + evCell + '</td>' +
        '<td class="mono" style="font-size:11px;"><a href="' + url + '" target="_blank">' + r.market_id + '</a></td>';
    propBody.appendChild(tr);
  });
  if (!(data.prop_rows || []).length) {
    propBody.innerHTML = '<tr><td colspan="11" class="muted" style="padding:24px;text-align:center;">no player props matched</td></tr>';
  }

  applyFilters();
}

function applyFilters() {
  const types = new Set();
  document.querySelectorAll('#chips .chip.active').forEach(c => types.add(c.dataset.f));
  const books = new Set();
  document.querySelectorAll('#book-chips .chip.active').forEach(c => books.add(c.dataset.bf));
  const sides = new Set();
  document.querySelectorAll('#side-chips .chip.active').forEach(c => sides.add(c.dataset.sf));
  const allTypes = types.has('all');
  const allBooks = books.has('all');
  const allSides = sides.has('all');
  document.querySelectorAll('#rows tr').forEach(tr => {
    const typeOk = allTypes || types.has(tr.dataset.mtype);
    const bookOk = allBooks || books.has(tr.dataset.book);
    const sideOk = allSides || sides.has(tr.dataset.side);
    if (typeOk && bookOk && sideOk) tr.classList.remove('hidden');
    else tr.classList.add('hidden');
  });
}

function wireChipGroup(containerId, allKey, activeAttr) {
  document.getElementById(containerId).addEventListener('click', (e) => {
    if (!e.target.classList.contains('chip')) return;
    const chip = e.target;
    const selector = '#' + containerId + ' .chip';
    if (chip.dataset[activeAttr] === 'all') {
      const willActivate = !chip.classList.contains('active');
      document.querySelectorAll(selector).forEach(c =>
        willActivate ? c.classList.add('active') : c.classList.remove('active'));
    } else {
      chip.classList.toggle('active');
      const others = Array.from(document.querySelectorAll(selector))
        .filter(c => c.dataset[activeAttr] !== 'all');
      const allActive = others.every(c => c.classList.contains('active'));
      document.querySelector('#' + containerId + ' .chip[data-' + allKey + '="all"]')
        .classList.toggle('active', allActive);
    }
    applyFilters();
  });
}
wireChipGroup('chips', 'f', 'f');
wireChipGroup('book-chips', 'bf', 'bf');
wireChipGroup('side-chips', 'sf', 'sf');

async function tick() {
  try {
    const r = await fetch('/api/ev');
    const d = await r.json();
    render(d);
  } catch (e) {
    document.getElementById('err').innerHTML = '<div class="err">fetch failed: ' + e + '</div>';
  }
}

tick();
setInterval(tick, 5000);
</script>
</body>
</html>
"""


PAPER_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Paper Tracker</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
         background: #0e0f12; color: #e8ecf1; margin: 0; padding: 24px; }
  h1 { margin: 0 0 4px 0; font-size: 20px; }
  h2 { margin: 24px 0 8px 0; font-size: 15px; color: #c4cbd6; }
  .meta { color: #9aa3b2; font-size: 12px; margin-bottom: 12px; }
  .stats { display: flex; gap: 24px; margin: 12px 0 20px 0; flex-wrap: wrap; }
  .stat { background: #15171c; padding: 10px 14px; border-radius: 6px; min-width: 120px; }
  .stat .lbl { color: #9aa3b2; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  .stat .val { font-size: 18px; font-weight: 600; margin-top: 2px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #23262d; }
  th { color: #9aa3b2; font-weight: 500; background: #15171c; position: sticky; top: 0; }
  tr:hover td { background: #171a20; }
  .pos { color: #4ade80; font-weight: 600; }
  .neg { color: #f87171; font-weight: 600; }
  .muted { color: #7a8291; }
  .mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  .mtype { text-transform: uppercase; letter-spacing: 0.04em; font-size: 10px;
           padding: 2px 6px; border-radius: 3px; font-weight: 600; }
  .mtype.moneyline   { background: #1e3a5f; color: #93c5fd; }
  .mtype.spread      { background: #3a2d6a; color: #c4b5fd; }
  .mtype.total       { background: #0f4a3a; color: #6ee7b7; }
  .mtype.team_total  { background: #5a3a1e; color: #fcd34d; }
  .mtype.player_prop { background: #4c1d3d; color: #f9a8d4; }
  .book { text-transform: uppercase; letter-spacing: 0.04em; font-size: 10px;
          padding: 2px 6px; border-radius: 3px; font-weight: 600; margin-left: 4px; }
  .book.kalshi     { background: #1a3524; color: #86efac; }
  .book.polymarket { background: #33194d; color: #d8b4fe; }
  a { color: #7dd3fc; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .version { position: fixed; top: 14px; right: 18px; z-index: 10;
             font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px;
             color: #9aa3b2; background: #15171c; border: 1px solid #23262d;
             padding: 3px 8px; border-radius: 4px; }
</style>
</head>
<body>
  <div class="version">v{{version}}</div>
  <h1>Paper Tracker <span class="muted" style="font-size:12px;">Kelly 0.25 · $5,000 compounding · per-book fee · min edge 2%</span></h1>
  <div class="meta"><a href="/">Live EV</a> &nbsp;·&nbsp; <a href="/paper">Paper</a> &nbsp;·&nbsp; <a href="/real">Real Trading</a></div>
  <div class="stats" id="stats"></div>
  <h2>Open positions</h2>
  <table>
    <thead><tr>
      <th>Placed</th><th>Market</th><th>Matchup</th><th>Selection</th>
      <th>Fair %</th><th>Edge %</th><th>Fill</th><th>Shares</th><th>Stake</th>
      <th>Exp. profit</th><th>Start</th><th>Poll time</th>
    </tr></thead>
    <tbody id="open"></tbody>
  </table>
  <h2>Settled</h2>
  <table>
    <thead><tr>
      <th>Settled</th><th>Market</th><th>Matchup</th><th>Selection</th>
      <th>Fair %</th><th>Close %</th><th>Fair Δ</th><th>CLV</th><th>Edge %</th><th>Fill</th><th>Shares</th><th>Stake</th>
      <th>Result</th><th>Net P&amp;L</th><th>Bankroll</th><th>Poll time</th>
    </tr></thead>
    <tbody id="settled"></tbody>
  </table>
<script>
function money(x) { return '$' + (x || 0).toFixed(2); }
function pct(x) { return ((x || 0) * 100).toFixed(1) + '%'; }
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}
function fmtStart(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const hrs = (d - new Date()) / 3_600_000;
  const when = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  const delta = hrs >= 0 ? ('in ' + hrs.toFixed(1) + 'h') : (Math.abs(hrs).toFixed(1) + 'h ago');
  return when + ' <span class="muted">(' + delta + ')</span>';
}
function rowBook(r) { return r.book || 'kalshi'; }
function bookBadge(r) {
  const b = rowBook(r);
  return '<span class="book ' + b + '">' + b + '</span>';
}
function mtypeCell(r) {
  const m = r.market_type || 'moneyline';
  const label = m === 'moneyline' ? 'ML'
              : m === 'team_total' ? 'TT'
              : m === 'player_prop' ? 'PROP'
              : m.slice(0,3).toUpperCase();
  const period = (r.period_label && r.period_label !== 'FULL') ?
      ' <span class="muted">' + r.period_label + '</span>' : '';
  return '<span class="mtype ' + m + '">' + label + '</span>' + bookBadge(r) + period;
}

function fmtPoll(r) {
  if (typeof r.total_poll_sec !== 'number') return '—';
  const pin = typeof r.pin_poll_sec === 'number' ? r.pin_poll_sec.toFixed(1) + 's pin' : '?s pin';
  const bk = typeof r.book_poll_sec === 'number' ? r.book_poll_sec.toFixed(1) + 's ' + (r.book || '') : '?s book';
  return pin + ' + ' + bk + ' = ' + r.total_poll_sec.toFixed(1) + 's';
}

function render(data) {
  const s = data.summary || {};
  const pnlClass = (s.total_pnl || 0) >= 0 ? 'pos' : 'neg';
  const evClass = (s.net_ev || 0) >= 0 ? 'pos' : 'neg';
  const evCloseClass = (s.net_ev_close || 0) >= 0 ? 'pos' : 'neg';
  let clvTile = '<div class="stat"><div class="lbl">Avg CLV</div><div class="val">—</div></div>';
  if (typeof s.avg_clv === 'number') {
    const bps = s.avg_clv * 100;
    const cls = bps >= 0 ? 'pos' : 'neg';
    const sign = bps >= 0 ? '+' : '';
    const sub = (s.clv_samples || 0) + ' samples · ' + (s.clv_positive || 0) + ' positive';
    clvTile = '<div class="stat"><div class="lbl">Avg CLV</div>' +
              '<div class="val ' + cls + '">' + sign + bps.toFixed(2) + ' pp</div>' +
              '<div class="muted" style="font-size:11px;">' + sub + '</div></div>';
  }
  let fairDeltaTile = '<div class="stat"><div class="lbl">Avg Fair Δ</div><div class="val">—</div></div>';
  if (typeof s.avg_fair_delta === 'number') {
    const bps = s.avg_fair_delta * 100;
    const cls = bps >= 0 ? 'pos' : 'neg';
    const sign = bps >= 0 ? '+' : '';
    const sub = (s.fair_delta_samples || 0) + ' samples · ' + (s.fair_delta_positive || 0) + ' positive';
    fairDeltaTile = '<div class="stat"><div class="lbl">Avg Fair Δ</div>' +
                    '<div class="val ' + cls + '">' + sign + bps.toFixed(2) + ' pp</div>' +
                    '<div class="muted" style="font-size:11px;">' + sub + '</div></div>';
  }
  document.getElementById('stats').innerHTML =
      '<div class="stat"><div class="lbl">Bankroll</div><div class="val">' + money(data.bankroll) + '</div></div>' +
      '<div class="stat"><div class="lbl">Net P&L</div><div class="val ' + pnlClass + '">' + money(s.total_pnl || 0) + '</div></div>' +
      '<div class="stat"><div class="lbl">Net EV (placement)</div><div class="val ' + evClass + '">' + money(s.net_ev || 0) + '</div><div class="muted" style="font-size:11px;">' + (s.net_ev_close_samples || 0) + ' samples</div></div>' +
      '<div class="stat"><div class="lbl">Net EV (close)</div><div class="val ' + evCloseClass + '">' + money(s.net_ev_close || 0) + '</div><div class="muted" style="font-size:11px;">' + (s.net_ev_close_samples || 0) + ' samples</div></div>' +
      clvTile +
      fairDeltaTile +
      '<div class="stat"><div class="lbl">ROI</div><div class="val">' + (s.roi_pct || 0).toFixed(2) + '%</div></div>' +
      '<div class="stat"><div class="lbl">Placed</div><div class="val">' + (s.total_placed || 0) + '</div></div>' +
      '<div class="stat"><div class="lbl">Open</div><div class="val">' + (s.open || 0) + '</div></div>' +
      '<div class="stat"><div class="lbl">Settled</div><div class="val">' + (s.total_settled || 0) + '</div></div>';

  const openBody = document.getElementById('open');
  openBody.innerHTML = '';
  (data.open_positions || []).slice().reverse().forEach(r => {
    const tr = document.createElement('tr');
    const edgePct = (typeof r.edge_pct === 'number')
        ? r.edge_pct
        : ((r.stake || 0) > 0 ? (r.expected_profit || 0) / r.stake * 100 : 0);
    tr.innerHTML =
        '<td class="mono">' + fmtDate(r.placed_at) + '</td>' +
        '<td>' + mtypeCell(r) + '</td>' +
        '<td>' + (r.pin_matchup || '—') + '</td>' +
        '<td>' + (r.selection || '—') + '</td>' +
        '<td class="mono">' + ((r.fair_prob || 0) * 100).toFixed(2) + '%</td>' +
        '<td class="mono pos">' + edgePct.toFixed(2) + '%</td>' +
        '<td class="mono">' + ((r.avg_fill_price || 0) * 100).toFixed(1) + '¢</td>' +
        '<td class="mono">' + (r.shares || 0).toLocaleString() + '</td>' +
        '<td class="mono">' + money(r.stake) + '</td>' +
        '<td class="mono pos">' + money(r.expected_profit || 0) + '</td>' +
        '<td class="mono">' + fmtStart(r.pin_start_time) + '</td>' +
        '<td class="mono muted">' + fmtPoll(r) + '</td>';
    openBody.appendChild(tr);
  });
  if (!(data.open_positions || []).length) {
    openBody.innerHTML = '<tr><td colspan="12" class="muted" style="padding:24px;text-align:center;">no open positions</td></tr>';
  }

  const settledBody = document.getElementById('settled');
  settledBody.innerHTML = '';
  (data.settled || []).slice().reverse().slice(0, 100).forEach(r => {
    const tr = document.createElement('tr');
    // Scalar (fractional) settlements have no clean winner; color by P&L sign.
    const resultClass = r.result === 'scalar'
        ? ((r.net_pnl || 0) > 0 ? 'pos' : 'neg')
        : (r.result === 'yes' ? 'pos' : 'neg');
    const pnlClass = (r.net_pnl || 0) >= 0 ? 'pos' : 'neg';
    const hasEdge = (typeof r.edge_pct === 'number') ||
                    ((typeof r.expected_profit === 'number') && (r.stake || 0) > 0);
    const edgePct = (typeof r.edge_pct === 'number')
        ? r.edge_pct
        : ((r.stake || 0) > 0 ? (r.expected_profit || 0) / r.stake * 100 : 0);
    const fairCell = (typeof r.fair_prob === 'number')
        ? (r.fair_prob * 100).toFixed(2) + '%' : '—';
    const edgeCell = hasEdge ? edgePct.toFixed(2) + '%' : '—';
    const closeCell = (typeof r.fair_prob_close === 'number')
        ? (r.fair_prob_close * 100).toFixed(2) + '%' : '—';
    let fairDeltaCell = '—';
    let fairDeltaCls = 'mono';
    if (typeof r.fair_prob_close === 'number' && typeof r.fair_prob === 'number') {
      const dpp = (r.fair_prob_close - r.fair_prob) * 100;
      fairDeltaCls = 'mono ' + (dpp >= 0 ? 'pos' : 'neg');
      fairDeltaCell = (dpp >= 0 ? '+' : '') + dpp.toFixed(2) + ' pp';
    }
    let clvCell = '—';
    let clvCls = 'mono';
    if (typeof r.clv === 'number') {
      const bps = r.clv * 100;
      clvCls = 'mono ' + (bps >= 0 ? 'pos' : 'neg');
      clvCell = (bps >= 0 ? '+' : '') + bps.toFixed(2) + ' pp';
    }
    tr.innerHTML =
        '<td class="mono">' + fmtDate(r.settled_at) + '</td>' +
        '<td>' + mtypeCell(r) + '</td>' +
        '<td>' + (r.pin_matchup || '—') + '</td>' +
        '<td>' + (r.selection || '—') + '</td>' +
        '<td class="mono">' + fairCell + '</td>' +
        '<td class="mono">' + closeCell + '</td>' +
        '<td class="' + fairDeltaCls + '">' + fairDeltaCell + '</td>' +
        '<td class="' + clvCls + '">' + clvCell + '</td>' +
        '<td class="mono pos">' + edgeCell + '</td>' +
        '<td class="mono">' + ((r.avg_fill_price || 0) * 100).toFixed(1) + '¢</td>' +
        '<td class="mono">' + (r.shares || 0).toLocaleString() + '</td>' +
        '<td class="mono">' + money(r.stake) + '</td>' +
        '<td class="' + resultClass + '">' + (r.result || '—').toUpperCase() + '</td>' +
        '<td class="mono ' + pnlClass + '">' + money(r.net_pnl || 0) + '</td>' +
        '<td class="mono">' + money(r.bankroll_after || 0) + '</td>' +
        '<td class="mono muted">' + fmtPoll(r) + '</td>';
    settledBody.appendChild(tr);
  });
  if (!(data.settled || []).length) {
    settledBody.innerHTML = '<tr><td colspan="16" class="muted" style="padding:24px;text-align:center;">no settled bets yet</td></tr>';
  }
}

async function tick() {
  try {
    const r = await fetch('/api/paper');
    render(await r.json());
  } catch (e) {}
}
tick();
setInterval(tick, 5000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE, top_n=TOP_N, version=config.APP_VERSION)


@app.route("/api/ev")
def api_ev():
    with _lock:
        return jsonify(dict(_state))


@app.route("/paper")
def paper_page():
    return render_template_string(PAPER_PAGE, version=config.APP_VERSION)


@app.route("/api/paper")
def api_paper():
    return jsonify(paper_tracker.snapshot())


REAL_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Real Trading</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
         background: #0e0f12; color: #e8ecf1; margin: 0; padding: 24px; }
  h1 { margin: 0 0 4px 0; font-size: 20px; }
  h2 { margin: 24px 0 8px 0; font-size: 15px; color: #c4cbd6; }
  .meta { color: #9aa3b2; font-size: 12px; margin-bottom: 12px; }
  .stats { display: flex; gap: 24px; margin: 12px 0 20px 0; flex-wrap: wrap; }
  .stat { background: #15171c; padding: 10px 14px; border-radius: 6px; min-width: 120px; }
  .stat .lbl { color: #9aa3b2; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  .stat .val { font-size: 18px; font-weight: 600; margin-top: 2px; }
  .banner { padding: 12px 16px; border-radius: 6px; margin: 12px 0; font-size: 13px; }
  .banner.warn { background: #3a2710; color: #fcd34d; border-left: 4px solid #f59e0b; }
  .banner.halt { background: #3b1010; color: #fca5a5; border-left: 4px solid #ef4444; }
  .banner.live { background: #0f3a24; color: #6ee7b7; border-left: 4px solid #10b981; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #23262d; }
  th { color: #9aa3b2; font-weight: 500; background: #15171c; position: sticky; top: 0; }
  tr:hover td { background: #171a20; }
  .pos { color: #4ade80; font-weight: 600; }
  .neg { color: #f87171; font-weight: 600; }
  .muted { color: #7a8291; }
  .mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  .mtype { text-transform: uppercase; letter-spacing: 0.04em; font-size: 10px;
           padding: 2px 6px; border-radius: 3px; font-weight: 600; }
  .mtype.moneyline   { background: #1e3a5f; color: #93c5fd; }
  .mtype.spread      { background: #3a2d6a; color: #c4b5fd; }
  .mtype.total       { background: #0f4a3a; color: #6ee7b7; }
  .mtype.team_total  { background: #5a3a1e; color: #fcd34d; }
  .mtype.player_prop { background: #4c1d3d; color: #f9a8d4; }
  .book { text-transform: uppercase; letter-spacing: 0.04em; font-size: 10px;
          padding: 2px 6px; border-radius: 3px; font-weight: 600; margin-left: 4px; }
  .book.kalshi     { background: #1a3524; color: #86efac; }
  .book.polymarket { background: #33194d; color: #d8b4fe; }
  .stat-pill { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px;
               font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
  .stat-pill.dry_run { background: #2a3a58; color: #bfdbfe; }
  .stat-pill.pending { background: #3a2710; color: #fcd34d; }
  .stat-pill.partial { background: #3a2710; color: #fcd34d; }
  .stat-pill.filled { background: #0f3a24; color: #6ee7b7; }
  .stat-pill.error { background: #3b1010; color: #fca5a5; }
  .stat-pill.pending_adapter { background: #2a2d34; color: #9aa3b2; }
  a { color: #7dd3fc; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .version { position: fixed; top: 14px; right: 18px; z-index: 10;
             font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px;
             color: #9aa3b2; background: #15171c; border: 1px solid #23262d;
             padding: 3px 8px; border-radius: 4px; }
</style>
</head>
<body>
  <div class="version">v{{version}}</div>
  <h1>Real Trading <span class="muted" style="font-size:12px;">Kelly 0.25 · $1,000 combined bankroll · same thresholds as paper · per-bet $30 cap · -$100 daily halt</span></h1>
  <div class="meta"><a href="/">Live EV</a> &nbsp;·&nbsp; <a href="/paper">Paper</a> &nbsp;·&nbsp; <a href="/real">Real Trading</a></div>
  <div id="banner"></div>
  <div class="stats" id="stats"></div>
  <h2>Open positions</h2>
  <table>
    <thead><tr>
      <th>Placed</th><th>Status</th><th>Market</th><th>Matchup</th><th>Selection</th>
      <th>Fair %</th><th>Edge %</th><th>Fill</th><th>Shares</th><th>Stake</th>
      <th>Exp. profit</th><th>Start</th><th>Poll time</th>
    </tr></thead>
    <tbody id="open"></tbody>
  </table>
  <h2>Settled</h2>
  <table>
    <thead><tr>
      <th>Settled</th><th>Market</th><th>Matchup</th><th>Selection</th>
      <th>Fair %</th><th>Close %</th><th>Fair Δ</th><th>CLV</th>
      <th>Edge %</th><th>Fill</th><th>Shares</th><th>Stake</th>
      <th>Result</th><th>Net P&amp;L</th><th>Poll time</th>
    </tr></thead>
    <tbody id="settled"></tbody>
  </table>
<script>
function money(x) { return '$' + (x || 0).toFixed(2); }
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}
function fmtStart(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const hrs = (d - new Date()) / 3_600_000;
  const when = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  const delta = hrs >= 0 ? ('in ' + hrs.toFixed(1) + 'h') : (Math.abs(hrs).toFixed(1) + 'h ago');
  return when + ' <span class="muted">(' + delta + ')</span>';
}
function bookBadge(r) {
  const b = r.book || 'kalshi';
  return '<span class="book ' + b + '">' + b + '</span>';
}
function mtypeCell(r) {
  const m = r.market_type || 'moneyline';
  const label = m === 'moneyline' ? 'ML'
              : m === 'team_total' ? 'TT'
              : m === 'player_prop' ? 'PROP'
              : m.slice(0,3).toUpperCase();
  const period = (r.period_label && r.period_label !== 'FULL') ?
      ' <span class="muted">' + r.period_label + '</span>' : '';
  return '<span class="mtype ' + m + '">' + label + '</span>' + bookBadge(r) + period;
}

function fmtPoll(r) {
  if (typeof r.total_poll_sec !== 'number') return '—';
  const pin = typeof r.pin_poll_sec === 'number' ? r.pin_poll_sec.toFixed(1) + 's pin' : '?s pin';
  const bk = typeof r.book_poll_sec === 'number' ? r.book_poll_sec.toFixed(1) + 's ' + (r.book || '') : '?s book';
  return pin + ' + ' + bk + ' = ' + r.total_poll_sec.toFixed(1) + 's';
}

function render(data) {
  const s = data.summary || {};
  const banner = document.getElementById('banner');
  if (data.halt) {
    banner.className = 'banner halt';
    banner.innerHTML = '<b>HALTED</b> &nbsp;' + (data.halt.reason || '') +
      ' &nbsp;·&nbsp; today PnL ' + money(s.today_realized_pnl || 0) +
      ' &nbsp;·&nbsp; resumes at next UTC midnight';
  } else if (!data.real_trading_enabled) {
    banner.className = 'banner warn';
    banner.innerHTML = 'Dry-run mode — REAL_TRADING_ENABLED is not set. Intents are logged but no orders are sent.';
  } else {
    banner.className = 'banner live';
    banner.innerHTML = 'Live trading active. Today PnL: ' + money(s.today_realized_pnl || 0) +
      ' &nbsp;·&nbsp; halt at ' + money(s.daily_loss_halt_usd || 0);
  }

  const pnlClass = (s.total_pnl || 0) >= 0 ? 'pos' : 'neg';
  const evClass = (s.net_ev || 0) >= 0 ? 'pos' : 'neg';
  const evCloseClass = (s.net_ev_close || 0) >= 0 ? 'pos' : 'neg';
  let fdTile = '<div class="stat"><div class="lbl">Fair Δ</div><div class="val">—</div></div>';
  if (typeof s.avg_fair_delta === 'number') {
    const bps = s.avg_fair_delta * 100;
    const cls = bps >= 0 ? 'pos' : 'neg';
    const sign = bps >= 0 ? '+' : '';
    fdTile = '<div class="stat"><div class="lbl">Fair Δ</div>' +
             '<div class="val ' + cls + '">' + sign + bps.toFixed(2) + ' pp</div></div>';
  }
  let clvTile = '<div class="stat"><div class="lbl">Avg CLV</div><div class="val">—</div></div>';
  if (typeof s.avg_clv === 'number') {
    const bps = s.avg_clv * 100;
    const cls = bps >= 0 ? 'pos' : 'neg';
    const sign = bps >= 0 ? '+' : '';
    clvTile = '<div class="stat"><div class="lbl">Avg CLV</div>' +
              '<div class="val ' + cls + '">' + sign + bps.toFixed(2) + ' pp</div></div>';
  }
  document.getElementById('stats').innerHTML =
      '<div class="stat"><div class="lbl">Bankroll</div><div class="val">' + money(data.bankroll) + '</div></div>' +
      '<div class="stat"><div class="lbl">Kalshi</div><div class="val">' + money(data.kalshi_balance) + '</div></div>' +
      '<div class="stat"><div class="lbl">Polymarket</div><div class="val">' + money(data.polymarket_balance) + '</div></div>' +
      '<div class="stat"><div class="lbl">Net P&L</div><div class="val ' + pnlClass + '">' + money(s.total_pnl || 0) + '</div></div>' +
      '<div class="stat"><div class="lbl">Net EV (placement)</div><div class="val ' + evClass + '">' + money(s.net_ev || 0) + '</div><div class="muted" style="font-size:11px;">' + (s.net_ev_close_samples || 0) + ' samples</div></div>' +
      '<div class="stat"><div class="lbl">Net EV (close)</div><div class="val ' + evCloseClass + '">' + money(s.net_ev_close || 0) + '</div><div class="muted" style="font-size:11px;">' + (s.net_ev_close_samples || 0) + ' samples</div></div>' +
      '<div class="stat"><div class="lbl">ROI</div><div class="val">' + (s.roi_pct || 0).toFixed(2) + '%</div></div>' +
      clvTile + fdTile +
      '<div class="stat"><div class="lbl">Placed</div><div class="val">' + (s.total_placed || 0) +
        ' <span class="muted" style="font-size:11px;">' +
        ((s.dry_run||0) ? (s.dry_run + ' dry · ') : '') +
        ((s.errors||0) ? (s.errors + ' err · ') : '') +
        ((s.pending_adapter||0) ? (s.pending_adapter + ' stub') : '') +
        '</span></div></div>' +
      '<div class="stat"><div class="lbl">Open</div><div class="val">' + (s.open || 0) + '</div></div>' +
      '<div class="stat"><div class="lbl">Settled</div><div class="val">' + (s.total_settled || 0) +
        ' <span class="muted" style="font-size:11px;">' + (s.wins || 0) + 'W / ' + (s.losses || 0) + 'L</span></div></div>';

  const openBody = document.getElementById('open');
  openBody.innerHTML = '';
  (data.open_positions || []).slice().reverse().forEach(r => {
    const tr = document.createElement('tr');
    const edgePct = (typeof r.edge_pct === 'number')
        ? r.edge_pct
        : ((r.stake || 0) > 0 ? (r.expected_profit || 0) / r.stake * 100 : 0);
    const status = r.status || 'pending';
    tr.innerHTML =
        '<td class="mono">' + fmtDate(r.placed_at) + '</td>' +
        '<td><span class="stat-pill ' + status + '">' + status + '</span></td>' +
        '<td>' + mtypeCell(r) + '</td>' +
        '<td>' + (r.pin_matchup || '—') + '</td>' +
        '<td>' + (r.selection || '—') + '</td>' +
        '<td class="mono">' + ((r.fair_prob || 0) * 100).toFixed(2) + '%</td>' +
        '<td class="mono pos">' + edgePct.toFixed(2) + '%</td>' +
        '<td class="mono">' + ((r.avg_fill_price || 0) * 100).toFixed(1) + '¢</td>' +
        '<td class="mono">' + (r.shares || 0).toLocaleString() + '</td>' +
        '<td class="mono">' + money(r.stake) + '</td>' +
        '<td class="mono pos">' + money(r.expected_profit || 0) + '</td>' +
        '<td class="mono">' + fmtStart(r.pin_start_time) + '</td>' +
        '<td class="mono muted">' + fmtPoll(r) + '</td>';
    openBody.appendChild(tr);
  });
  if (!(data.open_positions || []).length) {
    openBody.innerHTML = '<tr><td colspan="13" class="muted" style="padding:24px;text-align:center;">no open positions</td></tr>';
  }

  const settledBody = document.getElementById('settled');
  settledBody.innerHTML = '';
  (data.settled || []).slice().reverse().slice(0, 100).forEach(r => {
    const tr = document.createElement('tr');
    // Scalar (fractional) settlements have no clean winner; color by P&L sign.
    const isScalar = r.result === 'scalar';
    const won = r.result === r.side;
    const resultClass = isScalar ? ((r.net_pnl || 0) > 0 ? 'pos' : 'neg') : (won ? 'pos' : 'neg');
    const pnlClass = (r.net_pnl || 0) >= 0 ? 'pos' : 'neg';
    const edgePct = (typeof r.edge_pct === 'number')
        ? r.edge_pct
        : ((r.stake || 0) > 0 ? (r.expected_profit || 0) / r.stake * 100 : 0);
    let deltaCell = '—';
    let deltaCls = 'mono';
    if (typeof r.fair_prob_close === 'number' && typeof r.fair_prob === 'number') {
      const dpp = (r.fair_prob_close - r.fair_prob) * 100;
      deltaCls = 'mono ' + (dpp >= 0 ? 'pos' : 'neg');
      deltaCell = (dpp >= 0 ? '+' : '') + dpp.toFixed(2) + ' pp';
    }
    let clvCell = '—';
    let clvCls = 'mono';
    if (typeof r.clv === 'number') {
      const bps = r.clv * 100;
      clvCls = 'mono ' + (bps >= 0 ? 'pos' : 'neg');
      clvCell = (bps >= 0 ? '+' : '') + bps.toFixed(2) + ' pp';
    }
    tr.innerHTML =
        '<td class="mono">' + fmtDate(r.settled_at) + '</td>' +
        '<td>' + mtypeCell(r) + '</td>' +
        '<td>' + (r.pin_matchup || '—') + '</td>' +
        '<td>' + (r.selection || '—') + '</td>' +
        '<td class="mono">' + ((r.fair_prob || 0) * 100).toFixed(2) + '%</td>' +
        '<td class="mono">' + (typeof r.fair_prob_close === 'number' ? (r.fair_prob_close * 100).toFixed(2) + '%' : '—') + '</td>' +
        '<td class="' + deltaCls + '">' + deltaCell + '</td>' +
        '<td class="' + clvCls + '">' + clvCell + '</td>' +
        '<td class="mono pos">' + edgePct.toFixed(2) + '%</td>' +
        '<td class="mono">' + ((r.avg_fill_price || 0) * 100).toFixed(1) + '¢</td>' +
        '<td class="mono">' + (r.shares || 0).toLocaleString() + '</td>' +
        '<td class="mono">' + money(r.stake) + '</td>' +
        '<td class="' + resultClass + '">' + (isScalar ? 'SCALAR' : (won ? 'WIN' : 'LOSS')) + '</td>' +
        '<td class="mono ' + pnlClass + '">' + money(r.net_pnl || 0) + '</td>' +
        '<td class="mono muted">' + fmtPoll(r) + '</td>';
    settledBody.appendChild(tr);
  });
  if (!(data.settled || []).length) {
    settledBody.innerHTML = '<tr><td colspan="15" class="muted" style="padding:24px;text-align:center;">no settled bets yet</td></tr>';
  }
}

async function tick() {
  try {
    const r = await fetch('/api/real');
    render(await r.json());
  } catch (e) {}
}
tick();
setInterval(tick, 5000);
</script>
</body>
</html>
"""


@app.route("/real")
def real_page():
    return render_template_string(REAL_PAGE, version=config.APP_VERSION)


@app.route("/api/real")
def api_real():
    return jsonify(real_tracker.snapshot())


def main():
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    paper_tracker.start_settlement_thread()
    paper_tracker.start_close_capture_thread()
    real_tracker.start_settlement_thread()
    real_tracker.start_close_capture_thread()
    real_tracker.start_order_polling_thread()
    real_tracker.start_balance_logging_thread()
    host = config.EV_DASHBOARD_HOST
    print(f"dashboard starting on http://{host}:5055")
    app.run(host=host, port=5055, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
