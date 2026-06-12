#!/usr/bin/env python3
"""
Local EV dashboard (multi-book).

Runs a background scanner every 60s that loads the latest Pinnacle snapshot
plus one snapshot per registered soft-book adapter, normalizes each book's
rows, matches markets, pulls the live YES-ask ladder via the adapter, and
computes EV per share under the book's own fee model. Serves a local HTML
page at http://127.0.0.1:5055 that shows the top 25 EV bets with a per-book
badge and client-side book + market-type filter chips.

Run: python3 ev_dashboard.py
Stop: Ctrl-C
"""
import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string

from adapters import adapter_for, all_adapters
from data_utils import read_latest_snapshot_meta
from find_ev_bet import (
    SNAP_PIN,
    MAX_SNAPSHOT_AGE_SEC,
    american_to_decimal,
    breakeven_fair,
    evaluate,
    find_matches,
    load_latest_snapshot,
    price_to_american,
)
import paper_tracker
import real_tracker
import config

REFRESH_SEC = config.DASHBOARD_REFRESH_SEC
LADDER_FETCH_TIMEOUT_SEC = config.LADDER_FETCH_TIMEOUT_SEC
_LADDER_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ladder")
# Superset matcher produces candidates across ML / spread / total / team_total
# and across every registered book. A larger slice lets the client-side filter
# chips surface meaningful rows per slice without a server round-trip.
TOP_N = 25

app = Flask(__name__)

_state = {
    "last_updated": None,
    "pin_age": None,
    "book_ages": {},
    "stats": {},
    "rows": [],
    "prop_rows": [],
    "error": None,
}
_lock = threading.Lock()


def _load_soft_markets():
    """Normalize the latest snapshot from every registered adapter."""
    soft = []
    ages = {}
    for adapter in all_adapters():
        rows, age = load_latest_snapshot(adapter.SNAPSHOT_DIR)
        ages[adapter.BOOK] = age
        if rows is None:
            continue
        for raw in rows:
            nm = adapter.normalize_market(raw)
            if nm is not None:
                soft.append(nm)
    return soft, ages


def scan_once():
    pin_rows, pin_age = load_latest_snapshot(SNAP_PIN)
    if pin_rows is None:
        raise RuntimeError("missing pinnacle snapshot")
    soft_markets, book_ages = _load_soft_markets()
    if not soft_markets:
        raise RuntimeError("no soft-book snapshots")
    if pin_age > MAX_SNAPSHOT_AGE_SEC:
        raise RuntimeError(f"stale pinnacle snapshot pin={pin_age:.0f}s")
    for book, age in book_ages.items():
        if age is None or age > MAX_SNAPSHOT_AGE_SEC:
            age_str = f"{age:.0f}s" if age is not None else "MISSING"
            raise RuntimeError(f"stale {book} snapshot {age_str}")

    pin_meta = read_latest_snapshot_meta(SNAP_PIN)
    pin_poll_sec = (pin_meta or {}).get("cycle_elapsed_sec")
    book_poll_sec_map = {}
    for adapter in all_adapters():
        meta = read_latest_snapshot_meta(adapter.SNAPSHOT_DIR)
        if meta:
            book_poll_sec_map[adapter.BOOK] = meta.get("cycle_elapsed_sec")

    candidates, stats = find_matches(pin_rows, soft_markets)

    rows = []
    for c in candidates:
        nm = c["market"]
        book = c["book"]
        adapter = adapter_for(book)
        try:
            fut = _LADDER_EXECUTOR.submit(adapter.fetch_both_ladders, c["market_id"])
            yes_ladder, no_ladder = fut.result(timeout=LADDER_FETCH_TIMEOUT_SEC)
        except FutureTimeout:
            print(f"[scan_once] ladder fetch timeout {book}:{c['market_id']}")
            continue
        except Exception:
            continue

        sides = [("yes", yes_ladder, c["yes_fair"])]
        if getattr(adapter, "SUPPORTS_NO_SIDE", False) and no_ladder:
            sides.append(("no", no_ladder, c["opposite_fair"]))

        vig_pct = (
            1 / american_to_decimal(c["yes_side_price"])
            + 1 / american_to_decimal(c["opposite_side_price"])
            - 1
        ) * 100
        fee_fn = adapter.taker_fee_per_share

        for side, ladder, fair in sides:
            if not ladder:
                continue

            selection = c["selection"]
            if side == "no":
                # Prefix flags it in the UI without requiring a chip filter.
                selection = f"NO {selection}"

            # Paper tracker: simulate a Kelly-sized bet the first time this
            # (book, market_id, side) enters the in_window bucket.
            book_elapsed = book_poll_sec_map.get(book)
            total_elapsed = (
                pin_poll_sec + book_elapsed
                if pin_poll_sec is not None and book_elapsed is not None
                else None
            )
            side_row = {
                **c,
                "side": side,
                "fair_prob": fair,
                "selection": selection,
                "pin_poll_sec": pin_poll_sec,
                "book_poll_sec": book_elapsed,
                "total_poll_sec": total_elapsed,
            }
            try:
                paper_tracker.maybe_place(side_row, ladder)
            except Exception:
                traceback.print_exc()
            try:
                real_tracker.maybe_place(side_row, ladder)
            except Exception:
                traceback.print_exc()

            best_ask, best_qty = ladder[0]
            ev_per_share = (fair * (1 - best_ask)
                            - (1 - fair) * best_ask
                            - fee_fn(best_ask, fair))

            ev_result = evaluate(ladder, fair, fee_fn, book, c["market_type"])
            if ev_result is None:
                continue

            rows.append({
                "book": book,
                "market_id": c["market_id"],
                "side": side,
                "market_url": c["market_url"],
                "title": nm.title,
                "pin_matchup": c["pin_matchup"],
                "market_type": c["market_type"],
                "period_label": c["period_label"],
                "line": c.get("line"),
                "selection": selection,
                "yes_pin_name": c["yes_pin_name"],
                "yes_side_label": c["yes_side_label"],
                "opposite_side_label": c["opposite_side_label"],
                "yes_side_price": c["yes_side_price"],
                "opposite_side_price": c["opposite_side_price"],
                "yes_fair": c["yes_fair"],
                "opposite_fair": c["opposite_fair"],
                "fair_prob": fair,
                "vig_pct": vig_pct,
                "book_ask": best_ask,
                "book_ask_american": price_to_american(best_ask),
                "book_depth": best_qty,
                "ev_per_share": ev_per_share,
                "ev_pct_at_best": ev_per_share / best_ask * 100 if best_ask else 0.0,
                "breakeven_fair": breakeven_fair(best_ask, fee_fn),
                "pos_shares": ev_result["shares"],
                "pos_stake": ev_result["stake"],
                "pos_expected_profit": ev_result["exp_profit"],
                "pos_ev_pct": ev_result["ev_pct"],
                "pin_start_time": c.get("pin_start_time"),
                "in_window": c.get("in_window", False),
                "player": c.get("player"),
                "stat": c.get("stat"),
            })

    # Rank: in-window first, then by ev_per_share descending. Always surfaces
    # actionable rows ahead of out-of-window fallbacks so the table is never
    # empty.
    rows.sort(key=lambda r: (r["in_window"], r["ev_per_share"]), reverse=True)

    prop_rows = [r for r in rows if r["market_type"] == "player_prop"][:5]

    return {
        "pin_age": pin_age,
        "book_ages": book_ages,
        "stats": stats,
        "rows": rows[:TOP_N],
        "prop_rows": prop_rows,
    }


def scanner_loop():
    while True:
        try:
            result = scan_once()
            with _lock:
                _state["last_updated"] = datetime.now(timezone.utc).isoformat()
                _state["pin_age"] = result["pin_age"]
                _state["book_ages"] = result["book_ages"]
                _state["stats"] = result["stats"]
                _state["rows"] = result["rows"]
                _state["prop_rows"] = result["prop_rows"]
                _state["error"] = None
        except Exception as e:
            with _lock:
                _state["last_updated"] = datetime.now(timezone.utc).isoformat()
                _state["error"] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
        time.sleep(REFRESH_SEC)


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
</style>
</head>
<body>
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
  meta.textContent =
      'updated ' + updated +
      '  ·  pinnacle ' + (data.pin_age ? data.pin_age.toFixed(0) : '?') + 's' +
      (bookAgeStr ? '  ·  ' + bookAgeStr : '') +
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
</style>
</head>
<body>
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
      '<div class="stat"><div class="lbl">Net EV</div><div class="val ' + evClass + '">' + money(s.net_ev || 0) + '</div></div>' +
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
  (data.settled || []).slice().reverse().forEach(r => {
    const tr = document.createElement('tr');
    const resultClass = r.result === 'yes' ? 'pos' : 'neg';
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
    return render_template_string(PAGE, top_n=TOP_N)


@app.route("/api/ev")
def api_ev():
    with _lock:
        return jsonify(dict(_state))


@app.route("/paper")
def paper_page():
    return render_template_string(PAPER_PAGE)


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
</style>
</head>
<body>
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
  (data.settled || []).slice().reverse().forEach(r => {
    const tr = document.createElement('tr');
    const won = r.result === r.side;
    const resultClass = won ? 'pos' : 'neg';
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
        '<td class="' + resultClass + '">' + (won ? 'WIN' : 'LOSS') + '</td>' +
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
    return render_template_string(REAL_PAGE)


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
