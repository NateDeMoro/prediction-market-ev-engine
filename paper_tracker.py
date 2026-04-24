#!/usr/bin/env python3
"""
Multi-book paper-trading tracker.

Places a simulated bet the first time a (book, market_id) pair appears
in the 0.5-3h pre-game in_window bucket, sizes with fractional Kelly
(f = 0.25) off a compounding bankroll shared across books, and polls
the adapter's settlement endpoint every 30 minutes to resolve open
positions.

Fees are modeled per-book via the adapter's `fee_on_win_per_share` and
`fee_on_stake_per_share`:
  - Kalshi: fee only on a winning outcome (5% of profit).
  - Polymarket US: curved notional fee, paid upfront at placement
    (0.05 * p * (1-p) per share).

Placement and settlement records carry a `book` field. Legacy rows
without one replay as Kalshi to preserve pre-refactor bankroll.

State is reconstructed on import by replaying:
  data/paper_trades.jsonl        (append-only placements)
  data/paper_settlements.jsonl   (append-only settlements)

Public API:
  maybe_place(row, ladder, now)  -> record | None
  start_settlement_thread()      -> Thread
  snapshot()                     -> dict for /api/paper
"""
import fcntl
import json
import math
import os
import threading
import time
import traceback
from datetime import datetime, timezone

from adapters import adapter_for
from adapters.common import fuzzy_match
from devig_utils import devig_multiplicative

INITIAL_BANKROLL = 5000.0
KELLY_FRACTION = 0.25
MIN_EDGE_PCT = 2.0             # skip placement if expected_profit/stake < this
# Sanity ceilings on edge. Realistic +EV on liquid soft books is < 15% for
# team markets and < 25% for props — anything higher is almost certainly a
# matcher mistake (wrong Pinnacle game, cross-sport cross-match, etc.).
# These guard paper-tracker placement so ambient matcher bugs can't pollute
# CLV; rejected rows are logged to data/sanity_rejected.jsonl for triage.
SANITY_MAX_EDGE_PCT = float(os.getenv("SANITY_MAX_EDGE", "15.0"))
SANITY_MAX_EDGE_PCT_PROP = float(os.getenv("SANITY_MAX_EDGE_PROP", "25.0"))
# Player-prop edge gate is higher than team markets (Pinnacle's prop max-stake
# is ~$250 vs $7.5k+ for team totals/MLs; quotes are noisier). Also gated by
# INCLUDE_PROPS below so props stay out of paper trading until explicitly
# enabled — first week of prop data should land on the dashboard only so CLV
# for team markets isn't contaminated.
PROP_MIN_EDGE_PCT = float(os.getenv("PROP_MIN_EDGE", "4.0"))
INCLUDE_PROPS = os.getenv("PAPER_INCLUDE_PROPS") == "1"
SETTLEMENT_POLL_SEC = 30 * 60
# Closing-line-value capture cadence. Runs frequently so we hit the ~60-s
# window around startTime before the Pinnacle snapshot that contains the
# closing line rolls out of the pollers' 60-file retention.
CLOSE_CAPTURE_POLL_SEC = 30
CLOSE_CAPTURE_LEAD_SEC = 60        # start attempting capture this long before startTime
CLOSE_CAPTURE_TRAIL_SEC = 15 * 60  # stop attempting capture this long after startTime

PIN_PERIOD_LABEL_TO_INT = {"FULL": 0, "1H": 1, "2H": 2}

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DIR, "data")
TRADES_PATH = os.path.join(DATA_DIR, "paper_trades.jsonl")
SETTLEMENTS_PATH = os.path.join(DATA_DIR, "paper_settlements.jsonl")
CLOSES_PATH = os.path.join(DATA_DIR, "paper_closes.jsonl")
SANITY_REJECTED_PATH = os.path.join(DATA_DIR, "sanity_rejected.jsonl")
PIN_SNAPSHOT_DIR = os.path.join(DATA_DIR, "snapshots")

_lock = threading.Lock()
_placed_keys = set()       # f"{book}:{market_id}"
_open_positions = {}       # key -> placement record
_settled_records = []
_placements = []
_closes_by_key = {}        # key -> close record
_bankroll = INITIAL_BANKROLL


def _key(book, market_id):
    return f"{book}:{market_id}"


def _record_key(record):
    book = record.get("book") or "kalshi"
    mid = record.get("market_id") or record.get("ticker")
    return _key(book, mid) if mid else None


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append_jsonl(path, obj):
    # fcntl.flock serializes concurrent appends across threads (and processes,
    # e.g. void_paper_bet.py running while the settlement loop fires). Without
    # this, interleaved writes can truncate/corrupt lines on the JSONL log.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(obj) + "\n")


def _replay_state():
    global _bankroll
    trades = _read_jsonl(TRADES_PATH)
    settlements = _read_jsonl(SETTLEMENTS_PATH)
    closes = _read_jsonl(CLOSES_PATH)

    _placements.clear()
    _placed_keys.clear()
    _open_positions.clear()
    _settled_records.clear()
    _closes_by_key.clear()

    for c in closes:
        key = _record_key(c)
        if key:
            _closes_by_key[key] = c

    placements_by_key = {}
    for t in trades:
        key = _record_key(t)
        if not key:
            continue
        _placements.append(t)
        _placed_keys.add(key)
        _open_positions[key] = t
        placements_by_key[key] = t

    # Backfill display fields on legacy settlements that predate the
    # matchup/selection persistence by joining to their placement record.
    _enrich_fields = (
        "pin_matchup", "pin_sport", "pin_start_time",
        "market_type", "period_label", "line",
        "selection", "yes_side_label",
    )

    _bankroll = INITIAL_BANKROLL
    for s in settlements:
        key = _record_key(s)
        if key and key in placements_by_key:
            src = placements_by_key[key]
            for fld in _enrich_fields:
                if s.get(fld) is None and src.get(fld) is not None:
                    s[fld] = src.get(fld)
        if key and key in _closes_by_key and s.get("fair_prob_close") is None:
            close = _closes_by_key[key]
            s["fair_prob_close"] = close.get("fair_prob_close")
        _settled_records.append(s)
        if key:
            _open_positions.pop(key, None)
        pnl = s.get("net_pnl")
        if isinstance(pnl, (int, float)):
            _bankroll += pnl


_replay_state()


# ---------------------------------------------------------------------------
# Ladder walking with per-book fee
# ---------------------------------------------------------------------------

def _walk_positive_ev(ladder, fair_prob, adapter):
    """Filter ladder to levels with strictly positive EV/share under adapter fee."""
    kept = []
    total_shares = 0
    total_stake = 0.0
    for price, qty in ladder:
        gross = 1.0 - price
        fee = adapter.taker_fee_per_share(price, fair_prob)
        ev = fair_prob * gross - (1 - fair_prob) * price - fee
        if ev <= 0:
            break
        kept.append((price, qty))
        total_shares += qty
        total_stake += qty * price
    return kept, total_shares, total_stake


def _fill_shares(levels, target_shares):
    filled = 0
    stake = 0.0
    detail = []
    for price, qty in levels:
        if filled >= target_shares:
            break
        take = min(qty, target_shares - filled)
        if take <= 0:
            continue
        filled += take
        stake += take * price
        detail.append([price, take])
    return filled, stake, detail


def size_bet(ladder, fair_prob, bankroll, adapter):
    """Kelly-sized fill under the adapter's fee model.

    Net payoff per share on win:   1 - p - fee_on_win(p)
    Effective stake per share:     p + fee_on_stake(p)
    Kelly b = net_win / effective_stake, f_full = (b*q - (1-q)) / b.
    """
    pos_levels, pos_shares, pos_stake = _walk_positive_ev(ladder, fair_prob, adapter)
    if pos_shares <= 0 or pos_stake <= 0:
        return None

    vwap = pos_stake / pos_shares
    if vwap <= 0 or vwap >= 1:
        return None

    net_win = (1 - vwap) - adapter.fee_on_win_per_share(vwap)
    eff_stake = vwap + adapter.fee_on_stake_per_share(vwap)
    if net_win <= 0 or eff_stake <= 0:
        return None

    b = net_win / eff_stake
    f_full = fair_prob - (1 - fair_prob) / b
    if f_full <= 0:
        return None

    kelly_stake_budget = bankroll * f_full * KELLY_FRACTION
    if kelly_stake_budget <= 0:
        return None

    # Budget is a dollar ceiling on effective stake. Target shares = budget / eff_stake.
    target_shares = int(math.floor(kelly_stake_budget / eff_stake))
    shares = min(target_shares, pos_shares)
    if shares <= 0:
        return None

    filled, price_stake, detail = _fill_shares(pos_levels, shares)
    if filled <= 0:
        return None
    avg_fill = price_stake / filled

    fee_upfront = filled * adapter.fee_on_stake_per_share(avg_fill)
    total_stake = price_stake + fee_upfront

    # Expected profit on this fill under adapter fee model.
    expected_fee = adapter.taker_fee_per_share(avg_fill, fair_prob)
    ev_per_share = (fair_prob * (1 - avg_fill)
                    - (1 - fair_prob) * avg_fill
                    - expected_fee)
    expected_profit = ev_per_share * filled

    return {
        "shares": filled,
        "stake": round(total_stake, 4),
        "price_stake": round(price_stake, 4),
        "fee_upfront": round(fee_upfront, 4),
        "avg_fill_price": round(avg_fill, 6),
        "kelly_fraction_full": round(f_full, 6),
        "kelly_fraction_applied": KELLY_FRACTION,
        "expected_profit": round(expected_profit, 4),
        "levels": detail,
    }


def maybe_place(row, ladder, now=None):
    """Place a paper bet if row is in-window, (book, market_id) is new, Kelly > 0."""
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
    key = _key(book, market_id)

    with _lock:
        if key in _placed_keys:
            return None
        bankroll_now = _bankroll

    adapter = adapter_for(book)
    sized = size_bet(ladder, row["fair_prob"], bankroll_now, adapter)
    if sized is None:
        return None

    edge_pct = (sized["expected_profit"] / sized["stake"] * 100.0
                if sized["stake"] > 0 else 0.0)
    min_edge = PROP_MIN_EDGE_PCT if is_prop else MIN_EDGE_PCT
    if edge_pct < min_edge:
        return None

    max_edge = SANITY_MAX_EDGE_PCT_PROP if is_prop else SANITY_MAX_EDGE_PCT
    if edge_pct > max_edge:
        _append_jsonl(SANITY_REJECTED_PATH, {
            "rejected_at": (now or datetime.now(timezone.utc)).isoformat(),
            "book": book,
            "market_id": market_id,
            "pin_matchup": row.get("pin_matchup"),
            "pin_sport": row.get("pin_sport"),
            "market_type": row.get("market_type"),
            "selection": row.get("selection"),
            "fair_prob": row.get("fair_prob"),
            "avg_fill_price": sized["avg_fill_price"],
            "edge_pct": round(edge_pct, 4),
            "reason": f"edge_pct>{max_edge} (likely matcher mismatch)",
        })
        return None

    when = (now or datetime.now(timezone.utc)).isoformat()
    record = {
        "placed_at": when,
        "book": book,
        "market_id": market_id,
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
        "fair_prob": row.get("fair_prob"),
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
    }

    with _lock:
        if key in _placed_keys:
            return None
        _append_jsonl(TRADES_PATH, record)
        _placed_keys.add(key)
        _open_positions[key] = record
        _placements.append(record)

    return record


# ---------------------------------------------------------------------------
# Closing-line-value capture
# ---------------------------------------------------------------------------

def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _load_latest_pin_snapshot():
    try:
        files = sorted(
            os.path.join(PIN_SNAPSHOT_DIR, f)
            for f in os.listdir(PIN_SNAPSHOT_DIR) if f.endswith(".jsonl")
        )
    except OSError:
        return None
    if not files:
        return None
    try:
        with open(files[-1]) as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return None


def _team_side_from_record(record):
    """Resolve which side ('home'/'away') a team_total record targets, by
    fuzzy-matching the YES-side label against the stored pin home/away names."""
    label = record.get("yes_side_label") or ""
    home = record.get("pin_home_name") or ""
    away = record.get("pin_away_name") or ""
    # yes_side_label for team-total looks like "Team Over X.5"; strip the
    # trailing 'Over/Under N.N' so fuzzy_match sees just the team name.
    team = label
    for marker in (" Over ", " Under "):
        if marker in team:
            team = team.split(marker)[0]
            break
    if fuzzy_match(team, home):
        return "home"
    if fuzzy_match(team, away):
        return "away"
    return None


def _find_pin_prices(pin_rows, record):
    """Locate the YES/opposite American prices for this placement in a
    Pinnacle snapshot. Returns (yes_price, opp_price) or None."""
    matchup_id = record.get("pin_matchup_id")
    period = PIN_PERIOD_LABEL_TO_INT.get(record.get("period_label"))
    mtype = record.get("market_type")
    yes_d = record.get("yes_designation")
    opp_d = record.get("opposite_designation")
    line = record.get("line")
    if matchup_id is None or period is None or mtype is None:
        return None

    team_side = _team_side_from_record(record) if mtype == "team_total" else None
    prop_matchup_id = record.get("prop_matchup_id") if mtype == "player_prop" else None

    for r in pin_rows:
        if r.get("matchupId") != matchup_id:
            continue
        if r.get("type") != mtype:
            continue
        if mtype != "player_prop" and r.get("period") != period:
            continue
        if mtype == "team_total" and team_side is not None and r.get("side") != team_side:
            continue
        if mtype == "player_prop":
            # Narrow by prop-child matchupId when we have one stored; else by
            # (canonical stat, line) against this record.
            if prop_matchup_id is not None and r.get("prop_matchupId") != prop_matchup_id:
                continue
            if line is not None and abs((r.get("line") or 0) - line) >= 1e-9:
                continue
            prices = r.get("prices") or []
            if len(prices) != 2:
                continue
            yes_price = prices[0].get("price")  # Over
            opp_price = prices[1].get("price")  # Under
            if yes_price is not None and opp_price is not None:
                return yes_price, opp_price
            continue

        prices = r.get("prices") or []
        yes_price, opp_price = None, None
        for p in prices:
            d = p.get("designation")
            pts = p.get("points")
            if mtype == "moneyline":
                if d == yes_d:
                    yes_price = p.get("price")
                elif d == opp_d:
                    opp_price = p.get("price")
            elif mtype == "spread":
                target_yes_pts = -line if line is not None else None
                if (d == yes_d and pts is not None and target_yes_pts is not None
                        and abs(pts - target_yes_pts) < 1e-9):
                    yes_price = p.get("price")
                elif d == opp_d and pts is not None and line is not None and abs(pts - line) < 1e-9:
                    opp_price = p.get("price")
            else:  # total / team_total
                if pts is None or line is None or abs(pts - line) >= 1e-9:
                    continue
                if d == yes_d:
                    yes_price = p.get("price")
                elif d == opp_d:
                    opp_price = p.get("price")

        if yes_price is not None and opp_price is not None:
            return yes_price, opp_price
    return None


def _capture_close_for(record, pin_rows, now):
    """Attempt to capture closing Pinnacle fair prob for this open position.
    Appends to paper_closes.jsonl and populates _closes_by_key on success."""
    book = record.get("book") or "kalshi"
    market_id = record.get("market_id") or record.get("ticker")
    if not market_id:
        return None
    key = _key(book, market_id)
    if key in _closes_by_key:
        return None

    start = _parse_iso(record.get("pin_start_time"))
    if start is None:
        return None
    dt_to_start = (start - now).total_seconds()
    if dt_to_start > CLOSE_CAPTURE_LEAD_SEC:
        return None
    if dt_to_start < -CLOSE_CAPTURE_TRAIL_SEC:
        return None

    found = _find_pin_prices(pin_rows, record)
    if not found:
        return None
    yes_px, opp_px = found
    try:
        devigged = devig_multiplicative([yes_px, opp_px])
    except (ValueError, ZeroDivisionError):
        return None
    if devigged is None:
        return None
    yes_fair, _ = devigged

    close = {
        "captured_at": now.isoformat(),
        "book": book,
        "market_id": market_id,
        "pin_matchup": record.get("pin_matchup"),
        "pin_start_time": record.get("pin_start_time"),
        "market_type": record.get("market_type"),
        "period_label": record.get("period_label"),
        "line": record.get("line"),
        "fair_prob_close": round(yes_fair, 6),
        "yes_side_price_close": yes_px,
        "opposite_side_price_close": opp_px,
        "minutes_before_start": round(dt_to_start / 60.0, 2),
    }
    with _lock:
        if key in _closes_by_key:
            return None
        _append_jsonl(CLOSES_PATH, close)
        _closes_by_key[key] = close
    return close


def capture_closes_once():
    """Check each open position once; capture closing fair-prob if within window."""
    with _lock:
        pending = [r for r in _open_positions.values()
                   if _record_key(r) not in _closes_by_key]
    if not pending:
        return
    # Drop any whose startTime is already outside the capture window, so we
    # don't load a snapshot we won't use.
    now = datetime.now(timezone.utc)
    relevant = []
    for r in pending:
        start = _parse_iso(r.get("pin_start_time"))
        if start is None:
            continue
        dt = (start - now).total_seconds()
        if -CLOSE_CAPTURE_TRAIL_SEC <= dt <= CLOSE_CAPTURE_LEAD_SEC:
            relevant.append(r)
    if not relevant:
        return

    pin_rows = _load_latest_pin_snapshot()
    if pin_rows is None:
        return
    for r in relevant:
        try:
            _capture_close_for(r, pin_rows, now)
        except Exception:
            traceback.print_exc()


def _close_capture_loop():
    while True:
        try:
            capture_closes_once()
        except Exception:
            traceback.print_exc()
        time.sleep(CLOSE_CAPTURE_POLL_SEC)


def start_close_capture_thread():
    t = threading.Thread(target=_close_capture_loop, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def _settle_one(record):
    """Resolve the position via the adapter's settlement endpoint. Returns the
    settlement record or None if the market is not yet resolved."""
    book = record.get("book") or "kalshi"
    market_id = record.get("market_id") or record.get("ticker")
    if not market_id:
        return None
    adapter = adapter_for(book)
    try:
        result = adapter.fetch_settlement(market_id)
    except Exception as e:
        print(f"[paper_tracker] settlement fetch failed {book}:{market_id}: {e}")
        return None
    if result not in ("yes", "no"):
        return None

    shares = record["shares"]
    avg_fill = record["avg_fill_price"]
    total_stake = record.get("stake", 0.0)

    if result == "yes":
        fee_on_win = adapter.fee_on_win_per_share(avg_fill)
        gross_return = shares * (1.0 - fee_on_win)
        net_pnl = gross_return - total_stake
    else:
        gross_return = 0.0
        net_pnl = -total_stake

    key = _key(book, market_id)
    global _bankroll
    with _lock:
        # Atomic idempotency: pop before writing so a concurrent settle (another
        # poll tick, or void_paper_bet.py) returns early instead of double-
        # crediting the bankroll and appending a duplicate settlement row.
        if _open_positions.pop(key, None) is None:
            return None
        _bankroll += net_pnl
        settlement = {
            "settled_at": datetime.now(timezone.utc).isoformat(),
            "book": book,
            "market_id": market_id,
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
            "stake": total_stake,
            "avg_fill_price": avg_fill,
            "fair_prob": record.get("fair_prob"),
            "expected_profit": record.get("expected_profit"),
            "edge_pct": record.get("edge_pct"),
            "gross_return": round(gross_return, 4),
            "net_pnl": round(net_pnl, 4),
            "bankroll_after": round(_bankroll, 4),
        }
        close = _closes_by_key.get(key)
        if close is not None:
            fpc = close.get("fair_prob_close")
            settlement["fair_prob_close"] = fpc
            if isinstance(avg_fill, (int, float)) and isinstance(fpc, (int, float)):
                settlement["clv"] = round(fpc - avg_fill, 6)
        _append_jsonl(SETTLEMENTS_PATH, settlement)
        _settled_records.append(settlement)

    return settlement


def poll_settlements_once():
    """Check each open position once. Logs and swallows per-ticker errors."""
    with _lock:
        pending = list(_open_positions.values())
    for record in pending:
        try:
            _settle_one(record)
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


def snapshot():
    """JSON-serializable snapshot for the /api/paper endpoint."""
    with _lock:
        open_list = list(_open_positions.values())
        settled = list(_settled_records)
        bankroll = _bankroll

    total_placed = len(_placements)
    total_settled = len(settled)
    wins = sum(1 for s in settled if s.get("result") == "yes")
    losses = sum(1 for s in settled if s.get("result") == "no")
    total_pnl = round(bankroll - INITIAL_BANKROLL, 4)
    settled_stake = sum(s.get("stake", 0.0) for s in settled)
    settled_pnl = sum(s.get("net_pnl", 0.0) for s in settled)
    roi_pct = (settled_pnl / settled_stake * 100) if settled_stake > 0 else 0.0
    hit_rate = (wins / total_settled * 100) if total_settled else 0.0
    # Modeled expected profit, credited only when a bet settles. Voids
    # never contribute, so voiding an open bet zeros out its EV.
    net_ev = round(
        sum(s.get("expected_profit", 0.0) or 0.0
            for s in settled if s.get("result") != "void"),
        4,
    )

    clv_vals = [s["clv"] for s in settled if isinstance(s.get("clv"), (int, float))]
    avg_clv = round(sum(clv_vals) / len(clv_vals), 6) if clv_vals else None
    clv_positive = sum(1 for c in clv_vals if c > 0)
    clv_samples = len(clv_vals)

    return {
        "bankroll": round(bankroll, 4),
        "initial_bankroll": INITIAL_BANKROLL,
        "kelly_fraction": KELLY_FRACTION,
        "open_positions": open_list,
        "settled": settled,
        "summary": {
            "total_placed": total_placed,
            "total_settled": total_settled,
            "open": len(open_list),
            "wins": wins,
            "losses": losses,
            "hit_rate_pct": round(hit_rate, 2),
            "total_pnl": total_pnl,
            "net_ev": net_ev,
            "roi_pct": round(roi_pct, 2),
            "avg_clv": avg_clv,
            "clv_samples": clv_samples,
            "clv_positive": clv_positive,
        },
    }
