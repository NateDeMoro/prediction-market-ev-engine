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
import config
from adapters import adapter_for, kalshi_trade, polymarket_trade

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
_kalshi_balance = config.INITIAL_KALSHI_BALANCE
_polymarket_balance = config.INITIAL_POLYMARKET_BALANCE
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
    if not os.path.exists(config.REAL_HALT_PATH):
        return None
    try:
        with open(config.REAL_HALT_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_halt(reason, pnl):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    payload = {
        "halt_date": _utc_today(),
        "halted_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "pnl_at_halt": round(pnl, 4),
    }
    with open(config.REAL_HALT_PATH, "w") as f:
        json.dump(payload, f)
    return payload


def _halt_active():
    # In-memory check first: settled-today P&L below threshold halts immediately,
    # without waiting for the halt file write inside _settle_one to complete.
    if _today_realized_pnl() <= config.DAILY_LOSS_HALT_USD:
        return True
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

    Fill events are a pure overlay: the latest fill event for a key is the
    authoritative post-reconcile snapshot for that order. Fields present in
    the fill event (status, filled_count, avg_fill_price, fee_upfront, stake,
    cost_reconciled, broker_status) overwrite the placement record's values.
    Fields absent from the fill event (older partial events predate reconcile
    persistence) are left as-is from the placement record.

    Because the reconciled `stake` is overlaid from the fill event, the
    open-position deduction loop needs no special-case formula — it reads
    the correct actual cost directly from record["stake"]. Full-cancel
    records replay with status "canceled" and are excluded from open
    positions (stake not deducted, matching the live full refund).
    Partial-cancel records replay with effective status "filled" and the
    reconciled stake, matching the live state.
    """
    global _kalshi_balance, _polymarket_balance
    trades = pt._read_jsonl(config.REAL_TRADES_PATH)
    settlements = pt._read_jsonl(config.REAL_SETTLEMENTS_PATH)
    closes = pt._read_jsonl(config.REAL_CLOSES_PATH)
    fills = pt._read_jsonl(config.REAL_FILLS_PATH)

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
        # Pure overlay: apply the latest fill event fields onto the placement
        # record. Each field is only overlaid when present in the fill event
        # so that older partial-fill events (which predate reconcile persistence)
        # don't accidentally zero out fields that live reconcile populated.
        f = latest_fill.get(k)
        if f:
            if f.get("status"):
                t["status"] = f["status"]
            if f.get("filled_count") is not None:
                t["filled_count"] = f["filled_count"]
            if isinstance(f.get("avg_fill_price"), (int, float)):
                t["avg_fill_price"] = f["avg_fill_price"]
            # Reconcile-persisted fields (present only in post-reconcile events).
            for field in ("fee_upfront", "stake", "cost_reconciled", "broker_status"):
                if f.get(field) is not None:
                    t[field] = f[field]
        if k in settled_keys:
            continue
        if t.get("status") in ("pending", "partial", "filled", "ambiguous"):
            _open_positions[k] = t

    _kalshi_balance = config.INITIAL_KALSHI_BALANCE
    _polymarket_balance = config.INITIAL_POLYMARKET_BALANCE
    for s in _settled_records:
        pnl = s.get("net_pnl") or 0
        if s.get("book") == "kalshi":
            _kalshi_balance += pnl
        elif s.get("book") == "polymarket":
            _polymarket_balance += pnl
    # Deduct open-position cost basis. `stake` is the reconciled actual cost for
    # positions that have been through _reconcile_filled_cost, and the original
    # modeled stake for positions that haven't yet been polled (pending/partial).
    # Both cases correctly reflect cash committed to the broker.
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
# Fill reconciliation
# ---------------------------------------------------------------------------

def _reconcile_filled_cost(record, avg_px, filled_count, actual_fee=None):
    """Reconcile the local book balance against a known fill price.

    Caller must already hold `_lock` — non-reentrant, do not re-acquire
    inside this function (same convention as paper_tracker._stake_on_matchup).

    Idempotent: returns immediately if record["cost_reconciled"] is already set
    or avg_px is not numeric. Always sets cost_reconciled=True before returning.

    Fee resolution (priority order):
      1. actual_fee (real fee from broker response) — use when numeric.
      2. Modeled fallback:
           - Full fill (filled_count == shares): reuse record["fee_upfront"].
           - Partial fill: prorate record["fee_upfront"] by filled_count/shares.
         The prorated value is written back to record["fee_upfront"] so that
         _settle_one's independent `total_stake = avg_fill * filled_count + fee_upfront`
         stays consistent.

    Sets record["stake"] = actual_cost so snapshot() and replay deduct the
    correct amount. Calls _credit_book for the refund only when abs(refund) > 0.01
    to avoid noise from rounding (flag is set regardless).
    """
    if record.get("cost_reconciled"):
        return
    if not isinstance(avg_px, (int, float)):
        return

    shares = record.get("shares") or 1  # guard against zero-div; malformed record
    book = record.get("book")

    if isinstance(actual_fee, (int, float)):
        effective_fee = actual_fee
    else:
        modeled_fee = record.get("fee_upfront") or 0.0
        if filled_count < shares:
            effective_fee = modeled_fee * (filled_count / shares) if shares else 0.0
        else:
            effective_fee = modeled_fee

    # Write back so _settle_one reads the reconciled fee without special-casing.
    record["fee_upfront"] = effective_fee

    actual_cost = avg_px * filled_count + effective_fee
    refund = record.get("stake", 0.0) - actual_cost
    record["stake"] = actual_cost
    record["cost_reconciled"] = True

    if abs(refund) > 0.01:
        _credit_book(book, refund)


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
    # Capture actual fill price when the order fills immediately (Kalshi
    # `executed` → status `filled`). Resting orders return None here, which
    # correctly leaves the modeled VWAP in place until the poll loop fills them.
    if isinstance(resp.get("avg_fill_price"), (int, float)):
        record["avg_fill_price"] = resp["avg_fill_price"]
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

    resp = polymarket_trade.place_limit_order(slug, api_side, count, limit_price,
                                              client_order_id=record["client_order_id"])
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
    if is_prop and not config.REAL_INCLUDE_PROPS:
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

    if pin_matchup_id is not None and open_bets_on_match >= config.PER_MATCH_BET_CAP:
        return None

    per_match_cap = config.REAL_INITIAL_BANKROLL * config.PER_MATCH_STAKE_CAP_PCT  # $30
    available_match_stake = max(0.0, per_match_cap - already_on_match)
    if available_match_stake <= 0:
        return None

    # Hard caps: per-bet ceiling, per-book balance
    max_stake = min(available_match_stake, config.PER_BET_HARD_CAP_USD, book_balance)
    if max_stake <= 0:
        return None

    adapter = adapter_for(book)
    sized = pt.size_bet(ladder, fair_prob, bankroll_now, adapter, max_stake=max_stake)
    if sized is None:
        return None

    edge_pct = (sized["expected_profit"] / sized["stake"] * 100.0
                if sized["stake"] > 0 else 0.0)
    min_edge = config.min_edge_pct(book, row.get("market_type"), sized["avg_fill_price"])
    if edge_pct < min_edge:
        return None

    max_edge = config.SANITY_MAX_EDGE_PCT_PROP if is_prop else config.SANITY_MAX_EDGE_PCT
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
        "pin_poll_sec": row.get("pin_poll_sec"),
        "book_poll_sec": row.get("book_poll_sec"),
        "total_poll_sec": row.get("total_poll_sec"),
        "status": "dry_run",
        "order_id": None,
        "filled_count": 0,
        "remaining_count": sized["shares"],
    }

    if not config.REAL_TRADING_ENABLED:
        record["status"] = "dry_run"
        record["dry_run_reason"] = "REAL_TRADING_ENABLED!=1"
        with _lock:
            if key in _placed_keys:
                return None
            _placed_keys.add(key)
            pt._append_jsonl(config.REAL_TRADES_PATH, record)
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
        # Adapter exceptions are typed as ambiguous for POST so this catch
        # only fires for non-network bugs (signing failure, programmer
        # error). Treat as definitive failure — request was not sent.
        traceback.print_exc()
        record["status"] = "error"
        record["error"] = f"{type(e).__name__}: {e}"
        resp = {"error": record["error"]}

    if resp.get("error"):
        if resp.get("ambiguous"):
            # Request may have reached the broker (read-timeout, dropped
            # connection, 5xx). Do NOT refund the local stake — the order
            # may have filled. Keep the dedup key so we don't re-place. The
            # operator must reconcile against the broker UI and either run
            # scripts/void_paper_bet.py-equivalent if not filled, or update
            # status to filled if it was.
            record["status"] = "ambiguous"
            record["error"] = resp["error"]
            if resp.get("client_order_id"):
                record["client_order_id"] = resp["client_order_id"]
        else:
            # Definitive failure: request was not delivered (4xx, connect
            # refused, last_look_failed, missing adapter). Safe to refund.
            with _lock:
                _credit_book(book, record["stake"])
            if resp["error"] == "pending_adapter":
                record["status"] = "pending_adapter"
            else:
                record["status"] = "error"
            record["error"] = resp["error"]
    record["placement_response"] = resp

    with _lock:
        # Terminal status from a 2xx with no fill (rejected/expired/canceled):
        # stake was deducted pre-placement, refund it. _open_positions admission
        # below already excludes these statuses, so no further handling needed.
        if (record["status"] in ("rejected", "expired", "canceled")
                and (record.get("filled_count") or 0) == 0
                and not resp.get("error")):
            _credit_book(book, record["stake"])
            record["cost_reconciled"] = True
        # Immediate-fill reconciliation: reconcile before writing the trade row so
        # the persisted record carries the actual cost. Partial fills on Kalshi
        # present as status `pending` (Kalshi `resting` with fill_count > 0), not
        # `filled`, so they are not reconciled here — the poll loop handles them
        # on their next status transition.
        elif (record["status"] == "filled"
                and isinstance(record.get("avg_fill_price"), (int, float))):
            actual_fee = resp.get("taker_fees")
            _reconcile_filled_cost(
                record, record["avg_fill_price"], record["filled_count"],
                actual_fee=actual_fee if isinstance(actual_fee, (int, float)) else None,
            )
        pt._append_jsonl(config.REAL_TRADES_PATH, record)
        _placements.append(record)
        if record["status"] in ("pending", "partial", "filled", "ambiguous"):
            _open_positions[key] = record

    return record


# ---------------------------------------------------------------------------
# Order polling — observe Kalshi fills and update state
# ---------------------------------------------------------------------------

def _poll_open_orders_once():
    # Admit pending/partial orders and — defensively — any filled order that
    # hasn't yet been reconciled (e.g. avg_fill_price was None on the first fill
    # observation; the comment in the old code promising "the next poll will retry"
    # was incorrect because the filter excluded filled orders entirely).
    with _lock:
        live = [
            r for r in _open_positions.values()
            if r.get("order_id") and (
                r.get("status") in ("pending", "partial")
                or (r.get("status") == "filled" and not r.get("cost_reconciled"))
            )
        ]
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
            avg_px = None  # don't fall back to the record's (possibly modeled) price
        # Polymarket's avgPx is always in long-side (YES) perspective. For NO
        # buys (api_side="no") the actual per-share cost paid is 1 - avgPx,
        # so flip back into the internal side perspective before storing /
        # reconciling balance. Kalshi already returns price in the side bought.
        if (book == "polymarket" and isinstance(avg_px, (int, float))
                and avg_px > 0):
            from adapters.polymarket import _parse_market_id
            slug, long_or_short = _parse_market_id(record["market_id"])
            if slug:
                is_long = (long_or_short == "long")
                api_side = "yes" if (is_long == (record["side"] == "yes")) else "no"
                if api_side == "no":
                    avg_px = round(1.0 - avg_px, 6)

        is_defensive_retry = (record.get("status") == "filled"
                              and not record.get("cost_reconciled"))
        state_changed = (new_status != record["status"]
                         or new_filled != (record.get("filled_count") or 0))
        # Skip if nothing changed, unless this is a defensive retry that now has
        # a usable fill price (reconcile it even without a status transition).
        if not state_changed and not (is_defensive_retry and isinstance(avg_px, (int, float))):
            continue

        with _lock:
            # Update record state from the latest broker observation.
            if isinstance(avg_px, (int, float)):
                record["avg_fill_price"] = avg_px
            record["filled_count"] = new_filled
            # Reconcile / terminal handling BEFORE emitting the fill event so the
            # persisted event reflects the post-reconcile state (fee_upfront, stake,
            # cost_reconciled). The old order emitted the event first, persisting
            # the pre-reconcile modeled fee — that is the bug being fixed here.
            actual_fee = info.get("taker_fees")
            if not isinstance(actual_fee, (int, float)):
                actual_fee = None

            if new_status in ("canceled", "rejected", "expired"):
                if new_filled == 0:
                    # Full terminal: refund the entire deducted stake, mark
                    # reconciled, remove from open positions. Status is kept
                    # as the broker terminal value — settlement already skips
                    # non-filled positions, so no extra gate is needed.
                    _credit_book(book, record.get("stake", 0.0))
                    record["cost_reconciled"] = True
                    record["status"] = new_status
                    _open_positions.pop(_record_key(record), None)
                else:
                    # Partial fill then canceled: reconcile the filled portion
                    # (refunds the unfilled remainder; prorates or uses real fee),
                    # then promote to "filled" so _settle_one resolves the partial.
                    # Store the broker's terminal status for audit purposes.
                    _reconcile_filled_cost(
                        record, avg_px if isinstance(avg_px, (int, float)) else record.get("avg_fill_price"),
                        new_filled, actual_fee=actual_fee,
                    )
                    record["broker_status"] = new_status
                    record["status"] = "filled"
            elif new_status == "filled":
                record["status"] = new_status
                _reconcile_filled_cost(
                    record, avg_px if isinstance(avg_px, (int, float)) else record.get("avg_fill_price"),
                    new_filled, actual_fee=actual_fee,
                )
            else:
                record["status"] = new_status

            # Emit the fill event AFTER reconciliation so it carries the
            # authoritative post-reconcile snapshot. Replay overlays these
            # fields onto the placement record (see _replay_state).
            fill_event = {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "book": record["book"],
                "market_id": record["market_id"],
                "side": record["side"],
                "order_id": record["order_id"],
                "status": record["status"],
                "filled_count": record.get("filled_count"),
                "remaining_count": info.get("remaining_count") or 0,
                "avg_fill_price": (round(record["avg_fill_price"], 6)
                                   if isinstance(record.get("avg_fill_price"), (int, float))
                                   else None),
                "fee_upfront": record.get("fee_upfront"),
                "stake": record.get("stake"),
                "cost_reconciled": record.get("cost_reconciled"),
            }
            if record.get("broker_status"):
                fill_event["broker_status"] = record["broker_status"]
            pt._append_jsonl(config.REAL_FILLS_PATH, fill_event)
            _fills.append(fill_event)


def _order_polling_loop():
    while True:
        try:
            _poll_open_orders_once()
        except Exception:
            traceback.print_exc()
        time.sleep(config.ORDER_POLL_SEC)


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
        pt._append_jsonl(config.REAL_SETTLEMENTS_PATH, settlement)
        _settled_records.append(settlement)

        # Daily loss halt check
        if _today_realized_pnl() <= config.DAILY_LOSS_HALT_USD and not _halt_active():
            _write_halt(f"realized PnL <= ${config.DAILY_LOSS_HALT_USD}", _today_realized_pnl())
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
        time.sleep(config.SETTLEMENT_POLL_SEC)


def start_settlement_thread():
    t = threading.Thread(target=_settlement_loop, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Close-line capture — one shared routine with paper_tracker (no drift). real
# supplies its own state via CloseCaptureCtx; the gate (#20), go-live lock (#21)
# and moved-line interpolation (#22) all come from paper_tracker.run_close_capture.
# ---------------------------------------------------------------------------

_REAL_CTX = pt.CloseCaptureCtx(_lock, _open_positions, _closes_by_key,
                               "REAL_CLOSES_PATH", "real_tracker")


def _capture_closes_once():
    pt.run_close_capture(_REAL_CTX)


def _close_capture_loop():
    while True:
        try:
            _capture_closes_once()
        except Exception:
            traceback.print_exc()
        time.sleep(config.CLOSE_CAPTURE_POLL_SEC)


def start_close_capture_thread():
    t = threading.Thread(target=_close_capture_loop, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Periodic broker-balance snapshot
# ---------------------------------------------------------------------------

def _snapshot_balances_once():
    """Query both brokers for actual cash balance, snapshot in-memory open
    positions, and append the result to data/balance_snapshots.jsonl. Also
    prints a one-line summary so it surfaces in dashboard.stdout.log."""
    kalshi_dollars = None
    poly_dollars = None
    kalshi_err = None
    poly_err = None
    try:
        kb = kalshi_trade.get_balance()
        if kb.get("error"):
            kalshi_err = kb["error"]
        else:
            cents = kb.get("balance_cents")
            if isinstance(cents, (int, float)):
                kalshi_dollars = round(cents / 100.0, 4)
    except Exception as e:
        kalshi_err = f"{type(e).__name__}: {e}"
    try:
        pb = polymarket_trade.get_balance()
        if pb.get("error"):
            poly_err = pb["error"]
        else:
            val = pb.get("balance_dollars")
            if isinstance(val, (int, float)):
                poly_dollars = round(val, 4)
    except Exception as e:
        poly_err = f"{type(e).__name__}: {e}"

    open_positions = []
    open_value_kalshi = 0.0
    open_value_polymarket = 0.0
    with _lock:
        for r in _open_positions.values():
            stake = r.get("stake") or 0.0
            book = r.get("book")
            if book == "kalshi":
                open_value_kalshi += stake
            elif book == "polymarket":
                open_value_polymarket += stake
            open_positions.append({
                "book": book,
                "market_id": r.get("market_id"),
                "side": r.get("side"),
                "selection": r.get("selection"),
                "shares": r.get("shares"),
                "filled_count": r.get("filled_count"),
                "avg_fill_price": r.get("avg_fill_price"),
                "stake": stake,
                "status": r.get("status"),
                "pin_start_time": r.get("pin_start_time"),
            })
        local_kalshi = round(_kalshi_balance, 4)
        local_polymarket = round(_polymarket_balance, 4)

    open_value_kalshi = round(open_value_kalshi, 4)
    open_value_polymarket = round(open_value_polymarket, 4)
    open_value_combined = round(open_value_kalshi + open_value_polymarket, 4)

    # Total portfolio value = broker cash + cost basis of open positions.
    # Falls back to None per book when the broker call errored, so the JSONL
    # consumer can tell the difference between "$0 cash" and "balance unknown".
    total_kalshi = (round(kalshi_dollars + open_value_kalshi, 4)
                    if kalshi_dollars is not None else None)
    total_poly = (round(poly_dollars + open_value_polymarket, 4)
                  if poly_dollars is not None else None)
    if total_kalshi is not None and total_poly is not None:
        total_combined = round(total_kalshi + total_poly, 4)
    else:
        total_combined = None

    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "broker_balances": {
            "kalshi_dollars": kalshi_dollars,
            "polymarket_dollars": poly_dollars,
            "kalshi_error": kalshi_err,
            "polymarket_error": poly_err,
        },
        "local_balances": {
            "kalshi_dollars": local_kalshi,
            "polymarket_dollars": local_polymarket,
        },
        "open_position_value": {
            "kalshi_dollars": open_value_kalshi,
            "polymarket_dollars": open_value_polymarket,
            "combined_dollars": open_value_combined,
        },
        "total_value": {
            "kalshi_dollars": total_kalshi,
            "polymarket_dollars": total_poly,
            "combined_dollars": total_combined,
        },
        "open_positions": open_positions,
        "open_position_count": len(open_positions),
    }
    pt._append_jsonl(config.BALANCE_SNAPSHOT_PATH, snapshot)

    kalshi_cash_str = f"${kalshi_dollars:.2f}" if kalshi_dollars is not None else f"ERR({kalshi_err})"
    poly_cash_str = f"${poly_dollars:.2f}" if poly_dollars is not None else f"ERR({poly_err})"
    kalshi_total_str = f"${total_kalshi:.2f}" if total_kalshi is not None else "n/a"
    poly_total_str = f"${total_poly:.2f}" if total_poly is not None else "n/a"
    combined_str = f"${total_combined:.2f}" if total_combined is not None else "n/a"
    print(f"[real_tracker] balance snapshot: "
          f"kalshi cash={kalshi_cash_str} total={kalshi_total_str} | "
          f"polymarket cash={poly_cash_str} total={poly_total_str} | "
          f"combined_total={combined_str} | "
          f"open_positions={len(open_positions)} "
          f"(value k=${open_value_kalshi:.2f} p=${open_value_polymarket:.2f})")


def _balance_logging_loop():
    while True:
        try:
            _snapshot_balances_once()
        except Exception:
            traceback.print_exc()
        time.sleep(config.BALANCE_LOG_POLL_SEC)


def start_balance_logging_thread():
    t = threading.Thread(target=_balance_logging_loop, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Snapshot for /api/real
# ---------------------------------------------------------------------------

def snapshot():
    with _lock:
        open_list = list(_open_positions.values())
        settled = list(_settled_records)
        kalshi_cash = _kalshi_balance
        poly_cash = _polymarket_balance
        placements = list(_placements)

    # Add open-position cost basis back into each book's displayed balance so
    # the dashboard shows portfolio value (cash + locked-in stakes), not just
    # cash. Without this, every placement looks like a paper loss until the
    # bet settles. Stakes were already deducted from the cash balance at
    # placement time, so adding them back reconstructs total worth.
    open_value_kalshi = 0.0
    open_value_polymarket = 0.0
    for r in open_list:
        stake = r.get("stake") or 0.0
        if r.get("book") == "kalshi":
            open_value_kalshi += stake
        elif r.get("book") == "polymarket":
            open_value_polymarket += stake

    kalshi_bal = kalshi_cash + open_value_kalshi
    poly_bal = poly_cash + open_value_polymarket
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

    # Two EV bases, mirroring paper_tracker.snapshot(): net_ev = placement basis,
    # net_ev_close = close basis (sample-gated). Voids contribute to neither.
    settled_live = [s for s in settled if s.get("result") != "void"]
    net_ev = round(sum(s.get("expected_profit", 0.0) or 0.0 for s in settled_live), 4)
    close_ev_vals = [pt._expected_profit_at(s, s["fair_prob_close"])
                     for s in settled_live if isinstance(s.get("fair_prob_close"), (int, float))]
    close_ev_vals = [v for v in close_ev_vals if v is not None]
    net_ev_close = round(sum(close_ev_vals), 4)
    net_ev_close_samples = len(close_ev_vals)

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
        "real_trading_enabled": config.REAL_TRADING_ENABLED,
        "bankroll": round(bankroll, 4),
        "kalshi_balance": round(kalshi_bal, 4),
        "polymarket_balance": round(poly_bal, 4),
        "kalshi_cash": round(kalshi_cash, 4),
        "polymarket_cash": round(poly_cash, 4),
        "open_position_value_kalshi": round(open_value_kalshi, 4),
        "open_position_value_polymarket": round(open_value_polymarket, 4),
        "initial_bankroll": config.REAL_INITIAL_BANKROLL,
        "kelly_fraction": config.KELLY_FRACTION,
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
            "net_ev": net_ev,
            "net_ev_close": net_ev_close,
            "net_ev_close_samples": net_ev_close_samples,
            "roi_pct": round(roi_pct, 2),
            "avg_clv": avg_clv,
            "avg_fair_delta": avg_fair_delta,
            "today_realized_pnl": round(_today_realized_pnl(), 4),
            "daily_loss_halt_usd": config.DAILY_LOSS_HALT_USD,
        },
    }
