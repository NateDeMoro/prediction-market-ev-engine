"""
Real-money trading tracker.

Mirrors paper_tracker's flow (same edge thresholds, same Kelly, same fee
model) but transmits orders through book-specific trade adapters
(adapters/kalshi_trade.py, adapters/polymarket_trade.py).

Differs from paper in two ways:

  1. Combined $1000 bankroll across two books, but per-book balance
     accounting ($500 deposited each). Bets clamp to per-book available.

  2. Auto-placement via authenticated trade adapters. Gated by
     REAL_TRADING_ENABLED=1 — when off, sizing/threshold logic still
     runs and intents are logged as `dry_run`, but no order is sent.

Safety gates beyond the paper-tracker set:
  - $30 hard cap per single bet
  - -$100 daily realized-PnL halt (UTC day; persisted in real_halt.json)
  - Last-look ladder check before each Kalshi placement
  - Polymarket trade adapter is a stub until POC lands; polymarket
    placements log `pending_adapter` and skip transmission.

State files (data/):
  real_trades.jsonl       intent + initial placement response
  real_fills.jsonl        per-poll fill snapshots (status changes)
  real_settlements.jsonl  final settled rows (mirrors paper)
  real_closes.jsonl       fair_prob_close captures (mirrors paper)
  real_halt.json          when the daily-loss halt is active
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

import paper_tracker as pt
from adapters import adapter_for, kalshi_trade, polymarket_trade

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INITIAL_BANKROLL = 1000.0
INITIAL_KALSHI_BALANCE = 500.0
INITIAL_POLYMARKET_BALANCE = 500.0
KELLY_FRACTION = 0.25
PER_MATCH_STAKE_CAP_PCT = 0.03
PER_MATCH_BET_CAP = 2

# Threshold formula identical to paper_tracker. Reuse paper_tracker._min_edge_pct
# directly so the two trackers stay in lockstep.
MIN_EDGE_PCT = pt.MIN_EDGE_PCT
PROP_MIN_EDGE_PCT = pt.PROP_MIN_EDGE_PCT
SANITY_MAX_EDGE_PCT = pt.SANITY_MAX_EDGE_PCT
SANITY_MAX_EDGE_PCT_PROP = pt.SANITY_MAX_EDGE_PCT_PROP

PER_BET_HARD_CAP_USD = float(os.getenv("REAL_PER_BET_HARD_CAP_USD", "30.0"))
DAILY_LOSS_HALT_USD = float(os.getenv("REAL_DAILY_LOSS_HALT_USD", "-100.0"))
INCLUDE_PROPS = os.getenv("REAL_INCLUDE_PROPS") == "1"
REAL_TRADING_ENABLED = os.getenv("REAL_TRADING_ENABLED") == "1"

SETTLEMENT_POLL_SEC = 30 * 60
ORDER_POLL_SEC = 5
CLOSE_CAPTURE_POLL_SEC = pt.CLOSE_CAPTURE_POLL_SEC

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DIR, "data")
TRADES_PATH = os.path.join(DATA_DIR, "real_trades.jsonl")
FILLS_PATH = os.path.join(DATA_DIR, "real_fills.jsonl")
SETTLEMENTS_PATH = os.path.join(DATA_DIR, "real_settlements.jsonl")
CLOSES_PATH = os.path.join(DATA_DIR, "real_closes.jsonl")
HALT_PATH = os.path.join(DATA_DIR, "real_halt.json")

_TRADE_ADAPTERS = {
    "kalshi": kalshi_trade,
    "polymarket": polymarket_trade,
}

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_placements = []          # all attempted placements (incl. dry-run / errors)
_placed_keys = set()      # (book, market_id, side) tuples we've already tried
_open_positions = {}      # key -> placement record (status in pending/partial/filled awaiting settlement)
_settled_records = []
_closes_by_key = {}
_fills = []
_kalshi_balance = INITIAL_KALSHI_BALANCE
_polymarket_balance = INITIAL_POLYMARKET_BALANCE
_settle_defer_warned = set()  # keys we've already logged a deferred-settle warning for


def _key(book, market_id, side):
    return pt._key(book, market_id, side)


def _record_key(record):
    return pt._record_key(record)


# ---------------------------------------------------------------------------
# Halt state
# ---------------------------------------------------------------------------

def _utc_today():
    return datetime.now(timezone.utc).date().isoformat()


def _read_halt():
    if not os.path.exists(HALT_PATH):
        return None
    try:
        with open(HALT_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_halt(reason, pnl):
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "halt_date": _utc_today(),
        "halted_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "pnl_at_halt": round(pnl, 4),
    }
    with open(HALT_PATH, "w") as f:
        json.dump(payload, f)
    return payload


def _halt_active():
    h = _read_halt()
    if not h:
        return False
    return h.get("halt_date") == _utc_today()


def _today_realized_pnl():
    today = _utc_today()
    total = 0.0
    for s in _settled_records:
        ts = s.get("settled_at") or ""
        if ts.startswith(today):
            pnl = s.get("net_pnl")
            if isinstance(pnl, (int, float)):
                total += pnl
    return total


# ---------------------------------------------------------------------------
# State replay on import
# ---------------------------------------------------------------------------

def _replay_state():
    """Rebuild balances + open positions from disk on import.

    Per-book balance = initial_deposit + sum(net_pnl of settled rows) -
    sum(stake of still-open rows). That matches the live invariant where
    placement deducts `stake` and settlement credits `gross_return`
    (net change = net_pnl per round-trip).
    """
    global _kalshi_balance, _polymarket_balance
    trades = pt._read_jsonl(TRADES_PATH)
    settlements = pt._read_jsonl(SETTLEMENTS_PATH)
    closes = pt._read_jsonl(CLOSES_PATH)
    fills = pt._read_jsonl(FILLS_PATH)

    _placements.clear()
    _placed_keys.clear()
    _open_positions.clear()
    _settled_records.clear()
    _closes_by_key.clear()
    _fills.clear()

    for c in closes:
        k = _record_key(c)
        if k:
            _closes_by_key[k] = c

    settled_keys = set()
    for s in settlements:
        _settled_records.append(s)
        k = _record_key(s)
        if k:
            settled_keys.add(k)

    latest_fill = {}
    for f in fills:
        _fills.append(f)
        k = _record_key(f)
        if k:
            latest_fill[k] = f

    for t in trades:
        _placements.append(t)
        k = _record_key(t)
        if not k:
            continue
        _placed_keys.add(k)
        # Apply latest fill state to the placement record in-memory
        f = latest_fill.get(k)
        if f:
            t["status"] = f.get("status") or t.get("status")
            t["filled_count"] = f.get("filled_count") or t.get("filled_count")
            if isinstance(f.get("avg_fill_price"), (int, float)):
                t["avg_fill_price"] = f["avg_fill_price"]
        if k in settled_keys:
            continue
        if t.get("status") in ("pending", "partial", "filled"):
            _open_positions[k] = t

    _kalshi_balance = INITIAL_KALSHI_BALANCE
    _polymarket_balance = INITIAL_POLYMARKET_BALANCE
    for s in _settled_records:
        pnl = s.get("net_pnl") or 0
        if s.get("book") == "kalshi":
            _kalshi_balance += pnl
        elif s.get("book") == "polymarket":
            _polymarket_balance += pnl
    for p in _open_positions.values():
        cost = p.get("stake") or 0
        if p.get("book") == "kalshi":
            _kalshi_balance -= cost
        elif p.get("book") == "polymarket":
            _polymarket_balance -= cost


_replay_state()


# ---------------------------------------------------------------------------
# Per-book balance helpers
# ---------------------------------------------------------------------------

def _book_balance(book):
    if book == "kalshi":
        return _kalshi_balance
    if book == "polymarket":
        return _polymarket_balance
    return 0.0


def _deduct_book(book, amount):
    global _kalshi_balance, _polymarket_balance
    if book == "kalshi":
        _kalshi_balance -= amount
    elif book == "polymarket":
        _polymarket_balance -= amount


def _credit_book(book, amount):
    global _kalshi_balance, _polymarket_balance
    if book == "kalshi":
        _kalshi_balance += amount
    elif book == "polymarket":
        _polymarket_balance += amount


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _place_kalshi_order(record):
    """Send a real limit order to Kalshi. Returns the placement response dict
    or {"error": ...}. Mutates `record` with order_id / status / fill info."""
    ticker = record["market_id"]
    side = record["side"]
    count = record["shares"]
    limit_cents = int(round(record["avg_fill_price"] * 100))
    if not (1 <= limit_cents <= 99):
        return {"error": f"limit_cents {limit_cents} out of range"}

    if not kalshi_trade.last_look_ok(ticker, side, limit_cents, count):
        return {"error": "last_look_failed"}

    resp = kalshi_trade.place_limit_order(ticker, side, count, limit_cents,
                                          client_order_id=record["client_order_id"])
    if resp.get("error"):
        return resp
    record["order_id"] = resp.get("order_id")
    record["status"] = resp.get("status") or "pending"
    record["filled_count"] = resp.get("filled_count") or 0
    record["remaining_count"] = resp.get("remaining_count") or count
    return resp


def _place_polymarket_order(record):
    """Send a real limit order to Polymarket US. Translates the (market_id,
    side) pair from our internal {slug}:{long|short} convention into the
    Polymarket API's (slug, outcomeSide) form."""
    from adapters.polymarket import _parse_market_id
    market_id = record["market_id"]
    side = record["side"]
    count = record["shares"]
    limit_price = round(record["avg_fill_price"], 2)
    slug, long_or_short = _parse_market_id(market_id)
    if not slug:
        return {"error": f"unparseable polymarket market_id: {market_id}"}
    is_long = (long_or_short == "long")
    # Bet "yes" on the long side = YES of underlying. Bet "yes" on the short
    # side = NO of underlying. XOR the two to get the API outcomeSide.
    api_side = "yes" if (is_long == (side == "yes")) else "no"

    if not polymarket_trade.last_look_ok(slug, api_side, limit_price, count):
        return {"error": "last_look_failed"}

    resp = polymarket_trade.place_limit_order(slug, api_side, count, limit_price)
    if resp.get("error"):
        return resp
    record["order_id"] = resp.get("order_id")
    record["status"] = resp.get("status") or "pending"
    record["filled_count"] = resp.get("filled_count") or 0
    record["remaining_count"] = resp.get("remaining_count") or count
    return resp


def _place_real_order(record):
    book = record.get("book")
    if book == "kalshi":
        return _place_kalshi_order(record)
    if book == "polymarket":
        return _place_polymarket_order(record)
    return {"error": f"unknown book: {book}"}


# ---------------------------------------------------------------------------
# Public API: maybe_place
# ---------------------------------------------------------------------------

def maybe_place(row, ladder, now=None):
    if not row.get("in_window"):
        return None
    nm = row.get("market")
    if nm is None:
        return None
    book = nm.book
    market_id = nm.market_id
    if not market_id:
        return None
    is_prop = row.get("market_type") == "player_prop"
    if is_prop and not INCLUDE_PROPS:
        return None
    side = row.get("side") or "yes"
    key = _key(book, market_id, side)

    pin_matchup_id = row.get("pin_matchup_id")
    fair_prob = row.get("fair_prob")
    if not isinstance(fair_prob, (int, float)):
        return None

    with _lock:
        if key in _placed_keys:
            return None
        if _halt_active():
            return None
        bankroll_now = _kalshi_balance + _polymarket_balance
        already_on_match = sum(
            r.get("stake", 0.0) for r in _open_positions.values()
            if r.get("pin_matchup_id") == pin_matchup_id
        ) if pin_matchup_id is not None else 0.0
        open_bets_on_match = sum(
            1 for r in _open_positions.values()
            if r.get("pin_matchup_id") == pin_matchup_id
        ) if pin_matchup_id is not None else 0
        book_balance = _book_balance(book)

    if pin_matchup_id is not None and open_bets_on_match >= PER_MATCH_BET_CAP:
        return None

    per_match_cap = INITIAL_BANKROLL * PER_MATCH_STAKE_CAP_PCT  # $30, paper-cap-equivalent
    available_match_stake = max(0.0, per_match_cap - already_on_match)
    if available_match_stake <= 0:
        return None

    # Hard caps: per-bet ceiling, per-book balance
    max_stake = min(available_match_stake, PER_BET_HARD_CAP_USD, book_balance)
    if max_stake <= 0:
        return None

    adapter = adapter_for(book)
    sized = pt.size_bet(ladder, fair_prob, bankroll_now, adapter, max_stake=max_stake)
    if sized is None:
        return None

    edge_pct = (sized["expected_profit"] / sized["stake"] * 100.0
                if sized["stake"] > 0 else 0.0)
    min_edge = pt._min_edge_pct(book, sized["avg_fill_price"], is_prop)
    if edge_pct < min_edge:
        return None

    max_edge = SANITY_MAX_EDGE_PCT_PROP if is_prop else SANITY_MAX_EDGE_PCT
    if edge_pct > max_edge:
        return None  # silently — paper tracker logs these; we don't double-log

    when = (now or datetime.now(timezone.utc)).isoformat()
    record = {
        "placed_at": when,
        "client_order_id": str(uuid.uuid4()),
        "book": book,
        "market_id": market_id,
        "side": side,
        "pin_matchup": row.get("pin_matchup"),
        "pin_matchup_id": row.get("pin_matchup_id"),
        "pin_home_name": row.get("pin_home_name"),
        "pin_away_name": row.get("pin_away_name"),
        "pin_start_time": row.get("pin_start_time"),
        "pin_sport": row.get("pin_sport"),
        "market_type": row.get("market_type"),
        "period_label": row.get("period_label"),
        "line": row.get("line"),
        "player": row.get("player"),
        "stat": row.get("stat"),
        "prop_matchup_id": row.get("pin_prop_matchup_id"),
        "selection": row.get("selection"),
        "yes_side_label": row.get("yes_side_label"),
        "yes_designation": row.get("yes_designation"),
        "opposite_designation": row.get("opposite_designation"),
        "fair_prob": fair_prob,
        "avg_fill_price": sized["avg_fill_price"],
        "shares": sized["shares"],
        "stake": sized["stake"],
        "price_stake": sized["price_stake"],
        "fee_upfront": sized["fee_upfront"],
        "expected_profit": sized["expected_profit"],
        "edge_pct": round(edge_pct, 4),
        "bankroll_at_placement": round(bankroll_now, 4),
        "kelly_fraction_full": sized["kelly_fraction_full"],
        "kelly_fraction_applied": sized["kelly_fraction_applied"],
        "levels": sized["levels"],
        "status": "dry_run",
        "order_id": None,
        "filled_count": 0,
        "remaining_count": sized["shares"],
    }

    if not REAL_TRADING_ENABLED:
        record["status"] = "dry_run"
        record["dry_run_reason"] = "REAL_TRADING_ENABLED!=1"
        with _lock:
            if key in _placed_keys:
                return None
            _placed_keys.add(key)
            pt._append_jsonl(TRADES_PATH, record)
            _placements.append(record)
        return record

    # Real trading: claim balance and dedup key inside the same lock window so
    # concurrent placements on the same (book, market_id, side) can't both
    # debit and both place an order.
    with _lock:
        if key in _placed_keys:
            return None
        if _book_balance(book) < record["stake"]:
            return None
        _deduct_book(book, record["stake"])
        _placed_keys.add(key)

    try:
        resp = _place_real_order(record)
    except Exception as e:
        traceback.print_exc()
        record["status"] = "error"
        record["error"] = f"{type(e).__name__}: {e}"
        resp = {"error": record["error"]}

    if resp.get("error"):
        # Single refund path covers both exception and adapter-rejected errors.
        with _lock:
            _credit_book(book, record["stake"])
        if resp["error"] == "pending_adapter":
            record["status"] = "pending_adapter"
        else:
            record["status"] = "error"
        record["error"] = resp["error"]
    record["placement_response"] = resp

    with _lock:
        pt._append_jsonl(TRADES_PATH, record)
        _placements.append(record)
        if record["status"] in ("pending", "partial", "filled"):
            _open_positions[key] = record

    return record


# ---------------------------------------------------------------------------
# Order polling — observe Kalshi fills and update state
# ---------------------------------------------------------------------------

def _poll_open_orders_once():
    with _lock:
        live = [r for r in _open_positions.values()
                if r.get("status") in ("pending", "partial") and r.get("order_id")]
    for record in live:
        book = record.get("book")
        adapter_mod = _TRADE_ADAPTERS.get(book)
        if adapter_mod is None:
            continue
        try:
            info = adapter_mod.get_order(record["order_id"])
        except Exception:
            traceback.print_exc()
            continue
        if info.get("error"):
            continue
        new_status = info.get("status") or record["status"]
        new_filled = info.get("filled_count") or 0
        # Kalshi returns price in integer cents; Polymarket returns dollars float.
        avg_px_cents = info.get("avg_fill_price_cents")
        if isinstance(avg_px_cents, (int, float)):
            avg_px = avg_px_cents / 100.0
        elif isinstance(info.get("avg_fill_price"), (int, float)):
            avg_px = info["avg_fill_price"]
        else:
            avg_px = record["avg_fill_price"]
        # No state change?
        if (new_status == record["status"] and new_filled == (record.get("filled_count") or 0)):
            continue
        fill_event = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "book": record["book"],
            "market_id": record["market_id"],
            "side": record["side"],
            "order_id": record["order_id"],
            "status": new_status,
            "filled_count": new_filled,
            "remaining_count": info.get("remaining_count") or 0,
            "avg_fill_price": round(avg_px, 6) if isinstance(avg_px, (int, float)) else None,
        }
        with _lock:
            pt._append_jsonl(FILLS_PATH, fill_event)
            _fills.append(fill_event)
            record["status"] = new_status
            record["filled_count"] = new_filled
            record["avg_fill_price"] = avg_px if isinstance(avg_px, (int, float)) else record["avg_fill_price"]
            # Reconcile balance: actual cost may differ from intended.
            # Only run when avg_px is numeric — otherwise treating None as 0 cost
            # would refund the full stake even though real cash was committed.
            # Skip this cycle; the next poll will retry once the price arrives.
            if new_status in ("filled",) and isinstance(avg_px, (int, float)):
                actual_cost = avg_px * new_filled
                actual_cost += record.get("fee_upfront") or 0
                refund = (record["stake"] - actual_cost)
                if abs(refund) > 0.01:
                    _credit_book(record["book"], refund)


def _order_polling_loop():
    while True:
        try:
            _poll_open_orders_once()
        except Exception:
            traceback.print_exc()
        time.sleep(ORDER_POLL_SEC)


def start_order_polling_thread():
    t = threading.Thread(target=_order_polling_loop, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Settlement — reuses paper_tracker._settle_one logic via a thin wrapper
# ---------------------------------------------------------------------------

def _settle_one(record):
    """Resolve via the underlying read adapter, mirror paper_tracker._settle_one
    but operate on real-tracker state."""
    book = record.get("book") or "kalshi"
    market_id = record.get("market_id")
    if not market_id:
        return None
    side = record.get("side") or "yes"
    if record.get("status") not in ("filled", "partial"):
        # Don't settle pending or errored orders
        return None
    adapter = adapter_for(book)
    try:
        result = adapter.fetch_settlement(market_id)
    except Exception as e:
        print(f"[real_tracker] settlement fetch failed {book}:{market_id}: {e}")
        return None
    if result not in ("yes", "no"):
        return None

    shares = record.get("filled_count") or record["shares"]
    avg_fill = record.get("avg_fill_price")
    if not isinstance(avg_fill, (int, float)):
        # Adapter never returned a numeric fill price (or replay loaded a record
        # without one). Defer settlement; the next poll cycle should populate
        # avg_fill_price. If it never does the position needs manual void.
        key = _key(book, market_id, side)
        if key not in _settle_defer_warned:
            _settle_defer_warned.add(key)
            print(f"[real_tracker] deferring settlement for {book}:{market_id}:{side} "
                  f"— avg_fill_price is {avg_fill!r}; manual void may be required")
        return None
    fee_upfront = record.get("fee_upfront", 0.0)
    total_stake = avg_fill * shares + fee_upfront

    won = (result == side)
    if won:
        fee_on_win = adapter.fee_on_win_per_share(avg_fill)
        gross_return = shares * (1.0 - fee_on_win)
        net_pnl = gross_return - total_stake
    else:
        gross_return = 0.0
        net_pnl = -total_stake

    key = _key(book, market_id, side)
    global _kalshi_balance, _polymarket_balance
    with _lock:
        if _open_positions.pop(key, None) is None:
            return None
        # Credit gross return back to book (cost was already deducted at placement)
        _credit_book(book, gross_return)
        settlement = {
            "settled_at": datetime.now(timezone.utc).isoformat(),
            "book": book,
            "market_id": market_id,
            "side": side,
            "pin_matchup": record.get("pin_matchup"),
            "pin_sport": record.get("pin_sport"),
            "pin_start_time": record.get("pin_start_time"),
            "market_type": record.get("market_type"),
            "period_label": record.get("period_label"),
            "line": record.get("line"),
            "selection": record.get("selection"),
            "yes_side_label": record.get("yes_side_label"),
            "result": result,
            "shares": shares,
            "stake": round(total_stake, 4),
            "avg_fill_price": avg_fill,
            "fee_upfront": fee_upfront,
            "fair_prob": record.get("fair_prob"),
            "expected_profit": record.get("expected_profit"),
            "edge_pct": record.get("edge_pct"),
            "gross_return": round(gross_return, 4),
            "net_pnl": round(net_pnl, 4),
            "kalshi_balance_after": round(_kalshi_balance, 4),
            "polymarket_balance_after": round(_polymarket_balance, 4),
        }
        close = _closes_by_key.get(key)
        if close is not None:
            fpc = close.get("fair_prob_close")
            settlement["fair_prob_close"] = fpc
            if isinstance(avg_fill, (int, float)) and isinstance(fpc, (int, float)):
                settlement["clv"] = round(fpc - avg_fill, 6)
        pt._append_jsonl(SETTLEMENTS_PATH, settlement)
        _settled_records.append(settlement)

        # Daily loss halt check
        if _today_realized_pnl() <= DAILY_LOSS_HALT_USD and not _halt_active():
            _write_halt(f"realized PnL <= ${DAILY_LOSS_HALT_USD}", _today_realized_pnl())
    return settlement


def poll_settlements_once():
    with _lock:
        pending = list(_open_positions.values())
    for r in pending:
        try:
            _settle_one(r)
        except Exception:
            traceback.print_exc()


def _settlement_loop():
    while True:
        try:
            poll_settlements_once()
        except Exception:
            traceback.print_exc()
        time.sleep(SETTLEMENT_POLL_SEC)


def start_settlement_thread():
    t = threading.Thread(target=_settlement_loop, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Close-line capture — reuse paper_tracker._capture_close_for over our state
# ---------------------------------------------------------------------------

def _capture_closes_once():
    with _lock:
        pending = list(_open_positions.values())
    if not pending:
        return
    now = datetime.now(timezone.utc)
    relevant = []
    for r in pending:
        start = pt._parse_iso(r.get("pin_start_time"))
        if start is None:
            continue
        dt = (start - now).total_seconds()
        if dt < -pt.CLOSE_CAPTURE_TRAIL_SEC:
            continue
        is_prop = r.get("market_type") == "player_prop"
        if not is_prop and dt > pt.CLOSE_CAPTURE_LEAD_SEC:
            continue
        relevant.append(r)
    if not relevant:
        return
    pin_rows = pt._load_latest_pin_snapshot()
    if pin_rows is None:
        return
    for r in relevant:
        try:
            # paper_tracker._capture_close_for writes to its own paths/state.
            # We re-implement minimally here against our CLOSES_PATH.
            _capture_close_for(r, pin_rows, now)
        except Exception:
            traceback.print_exc()


def _capture_close_for(record, pin_rows, now):
    book = record.get("book") or "kalshi"
    market_id = record.get("market_id")
    if not market_id:
        return None
    side = record.get("side") or "yes"
    key = _key(book, market_id, side)
    is_prop = record.get("market_type") == "player_prop"
    if key in _closes_by_key and not is_prop:
        return None
    found = pt._find_pin_prices(pin_rows, record)
    if not found:
        return None
    yes_px, opp_px = found
    try:
        devigged = pt.devig_multiplicative([yes_px, opp_px])
    except (ValueError, ZeroDivisionError):
        return None
    if devigged is None:
        return None
    yes_fair, _ = devigged
    fair_close_side = yes_fair if side == "yes" else round(1.0 - yes_fair, 6)
    close = {
        "captured_at": now.isoformat(),
        "book": book,
        "market_id": market_id,
        "side": side,
        "pin_matchup": record.get("pin_matchup"),
        "pin_start_time": record.get("pin_start_time"),
        "market_type": record.get("market_type"),
        "period_label": record.get("period_label"),
        "line": record.get("line"),
        "fair_prob_close": round(fair_close_side, 6),
        "yes_side_price_close": yes_px,
        "opposite_side_price_close": opp_px,
    }
    with _lock:
        pt._append_jsonl(CLOSES_PATH, close)
        _closes_by_key[key] = close
    return close


def _close_capture_loop():
    while True:
        try:
            _capture_closes_once()
        except Exception:
            traceback.print_exc()
        time.sleep(CLOSE_CAPTURE_POLL_SEC)


def start_close_capture_thread():
    t = threading.Thread(target=_close_capture_loop, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Snapshot for /api/real
# ---------------------------------------------------------------------------

def snapshot():
    with _lock:
        open_list = list(_open_positions.values())
        settled = list(_settled_records)
        kalshi_bal = _kalshi_balance
        poly_bal = _polymarket_balance
        placements = list(_placements)

    bankroll = kalshi_bal + poly_bal
    total_placed = len(placements)
    total_settled = len(settled)
    wins = sum(1 for s in settled if s.get("result") == s.get("side"))
    losses = total_settled - wins
    settled_stake = sum(s.get("stake", 0.0) for s in settled)
    settled_pnl = sum(s.get("net_pnl", 0.0) for s in settled)
    roi_pct = (settled_pnl / settled_stake * 100) if settled_stake > 0 else 0.0
    hit_rate = (wins / total_settled * 100) if total_settled else 0.0
    # Realized P/L only; open-position stakes are held against book balances
    # but are not yet wins or losses.
    total_pnl = round(settled_pnl, 4)

    clv_vals = [s["clv"] for s in settled if isinstance(s.get("clv"), (int, float))]
    avg_clv = round(sum(clv_vals) / len(clv_vals), 6) if clv_vals else None

    fair_deltas = [s["fair_prob_close"] - s["fair_prob"] for s in settled
                   if isinstance(s.get("fair_prob_close"), (int, float))
                   and isinstance(s.get("fair_prob"), (int, float))]
    avg_fair_delta = round(sum(fair_deltas) / len(fair_deltas), 6) if fair_deltas else None

    halt = _read_halt() if _halt_active() else None

    error_count = sum(1 for p in placements if p.get("status") == "error")
    pending_adapter_count = sum(1 for p in placements if p.get("status") == "pending_adapter")
    dry_run_count = sum(1 for p in placements if p.get("status") == "dry_run")

    return {
        "real_trading_enabled": REAL_TRADING_ENABLED,
        "bankroll": round(bankroll, 4),
        "kalshi_balance": round(kalshi_bal, 4),
        "polymarket_balance": round(poly_bal, 4),
        "initial_bankroll": INITIAL_BANKROLL,
        "kelly_fraction": KELLY_FRACTION,
        "halt": halt,
        "open_positions": open_list,
        "settled": settled,
        "summary": {
            "total_placed": total_placed,
            "total_settled": total_settled,
            "open": len(open_list),
            "errors": error_count,
            "pending_adapter": pending_adapter_count,
            "dry_run": dry_run_count,
            "wins": wins,
            "losses": losses,
            "hit_rate_pct": round(hit_rate, 2),
            "total_pnl": total_pnl,
            "roi_pct": round(roi_pct, 2),
            "avg_clv": avg_clv,
            "avg_fair_delta": avg_fair_delta,
            "today_realized_pnl": round(_today_realized_pnl(), 4),
            "daily_loss_halt_usd": DAILY_LOSS_HALT_USD,
        },
    }
