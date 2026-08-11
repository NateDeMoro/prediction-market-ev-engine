#!/usr/bin/env python3
"""
Multi-book paper-trading tracker.

Places a simulated bet the first time a (book, market_id) pair appears
in the 0.5-3h pre-game in_window bucket, sizes with fractional Kelly
(f = 0.25) off a compounding bankroll shared across books, and polls
the adapter's settlement endpoint every 30 minutes to resolve open
positions.

Fees are modeled per-book via the adapter's `fee_on_win_per_share` and
`fee_on_stake_per_share`. Both books charge a curved notional taker fee
(rate * p * (1-p) per share) paid upfront at placement, with nothing
skimmed on settlement (win-fee = 0). Rates live in
`config.PER_BOOK_FEE_RATE` (the single source of truth); see the adapter
fee functions for the per-book detail.

Placement and settlement records carry a `book` field. Legacy rows
without one replay as Kalshi to preserve pre-refactor bankroll.

State is reconstructed on import by replaying:
  data/paper_trades.jsonl        (append-only placements)
  data/paper_settlements.jsonl   (append-only settlements)
  data/paper_reverify.jsonl      (append-only post-fill ladder re-checks)

Public API:
  maybe_place(row, ladder, now)  -> record | None
  start_settlement_thread()      -> Thread
  start_reverify_thread()        -> Thread
  snapshot()                     -> dict for /api/paper
"""
import collections
import fcntl
import json
import math
import os
import threading
import time
import traceback
from datetime import datetime, timezone

from pmev.adapters import adapter_for
from pmev.adapters.common import fuzzy_match
from pmev.core.devig import devig_multiplicative, synthesize_combined_american
from pmev import config

PIN_PERIOD_LABEL_TO_INT = {"FULL": 0, "1H": 1, "2H": 2, "F5": 1}

_lock = threading.Lock()
_placed_keys = set()       # f"{book}:{market_id}:{side}"
_open_positions = {}       # key -> placement record
_settled_records = []
_placements = []
_closes_by_key = {}        # key -> close record
_bankroll = config.PAPER_INITIAL_BANKROLL


def _new_pending_queue():
    """Bounded FIFO of placements awaiting their post-fill re-check.

    maxlen makes over-pressure drop the *oldest* entry, which is the right one to
    lose: it is the entry closest to aging out of tolerance anyway.
    """
    return collections.deque(maxlen=config.PAPER_REVERIFY_MAX_PENDING)


def _new_reverify_agg():
    return {"measured": 0, "skipped": 0, "claimed_shares": 0, "survived_shares": 0}


# Re-verification carries its own lock, deliberately not `_lock`: a diagnostic
# must never contend with the placement path it exists to observe.
_reverify_lock = threading.Lock()
_pending_reverify = _new_pending_queue()
_reverify_agg = _new_reverify_agg()


def _key(book, market_id, side="yes"):
    """Dedupe key. Legacy two-part keys replay as side='yes'."""
    return f"{book}:{market_id}:{side}"


def _stake_on_matchup(pin_matchup_id):
    """Sum of open-position stakes sharing this Pinnacle matchup id, across
    all books / market types / sides. Caller must hold `_lock`."""
    if pin_matchup_id is None:
        return 0.0
    return sum((r.get("stake") or 0.0) for r in _open_positions.values()
               if r.get("pin_matchup_id") == pin_matchup_id)


def _count_on_matchup(pin_matchup_id):
    """Number of open positions sharing this Pinnacle matchup id. Caller must
    hold `_lock`."""
    if pin_matchup_id is None:
        return 0
    return sum(1 for r in _open_positions.values()
               if r.get("pin_matchup_id") == pin_matchup_id)


def _record_key(record):
    book = record.get("book") or "kalshi"
    mid = record.get("market_id") or record.get("ticker")
    if not mid:
        return None
    side = record.get("side") or "yes"
    return _key(book, mid, side)


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


def _expected_profit_at(record, fair_prob):
    """Recompute expected_profit for a placement/settlement under a given fair
    prob, using the same fee model as size_bet. Returns None if inputs are
    missing or the adapter is unknown."""
    avg_fill = record.get("avg_fill_price")
    shares = record.get("shares")
    book = record.get("book") or "kalshi"
    if not isinstance(avg_fill, (int, float)) or not isinstance(shares, (int, float)):
        return None
    try:
        adapter = adapter_for(book)
    except Exception:
        return None
    fee = adapter.taker_fee_per_share(avg_fill, fair_prob)
    ev_per_share = fair_prob * (1 - avg_fill) - (1 - fair_prob) * avg_fill - fee
    return round(ev_per_share * shares, 4)


def _replay_state():
    global _bankroll
    trades = _read_jsonl(config.PAPER_TRADES_PATH)
    settlements = _read_jsonl(config.PAPER_SETTLEMENTS_PATH)
    closes = _read_jsonl(config.PAPER_CLOSES_PATH)

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

    _bankroll = config.PAPER_INITIAL_BANKROLL
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
        # expected_profit is the placement-basis EV. Settlements written by older
        # code overwrote it to close-basis; restore the placement value from the
        # placement record so the basis is consistent. Close-basis EV is
        # recomputed on the fly in snapshot() as net_ev_close.
        if key and key in placements_by_key:
            pe = placements_by_key[key].get("expected_profit")
            if pe is not None:
                s["expected_profit"] = pe
        _settled_records.append(s)
        if key:
            _open_positions.pop(key, None)
        pnl = s.get("net_pnl")
        if isinstance(pnl, (int, float)):
            _bankroll += pnl

    # Re-verification is a per-placement observation, not position state, so it
    # replays into the aggregate only — nothing here reopens or re-grades a bet.
    _reverify_agg.update(_new_reverify_agg())
    for r in _read_jsonl(config.PAPER_REVERIFY_PATH):
        _fold_reverify(r)


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


def size_bet(ladder, fair_prob, bankroll, adapter, max_stake=None):
    """Kelly-sized fill under the adapter's fee model.

    Net payoff per share on win:   1 - p - fee_on_win(p)
    Effective stake per share:     p + fee_on_stake(p)
    Kelly b = net_win / effective_stake, f_full = (b*q - (1-q)) / b.

    `max_stake`, if set, clamps the Kelly stake budget — used by the per-match
    cap so correlated bets on one match can't exceed a bankroll fraction.
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

    kelly_stake_budget = bankroll * f_full * config.KELLY_FRACTION
    if max_stake is not None:
        kelly_stake_budget = min(kelly_stake_budget, max_stake)
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
        "kelly_fraction_applied": config.KELLY_FRACTION,
        "expected_profit": round(expected_profit, 4),
        "levels": detail,
    }


def maybe_place(row, ladder, now=None):
    """Place a paper bet if row is in-window, (book, market_id, side) is new, Kelly > 0."""
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
    if is_prop and not config.PAPER_INCLUDE_PROPS:
        return None
    side = row.get("side") or "yes"
    key = _key(book, market_id, side)

    pin_matchup_id = row.get("pin_matchup_id")
    with _lock:
        if key in _placed_keys:
            return None
        bankroll_now = _bankroll
        already_on_match = _stake_on_matchup(pin_matchup_id)
        open_bets_on_match = _count_on_matchup(pin_matchup_id)

    if pin_matchup_id is not None and open_bets_on_match >= config.PER_MATCH_BET_CAP:
        return None

    per_match_cap = bankroll_now * config.PER_MATCH_STAKE_CAP_PCT
    available_match_stake = max(0.0, per_match_cap - already_on_match)
    if available_match_stake <= 0:
        return None

    adapter = adapter_for(book)
    sized = size_bet(ladder, row["fair_prob"], bankroll_now, adapter,
                     max_stake=available_match_stake)
    if sized is None:
        return None

    edge_pct = (sized["expected_profit"] / sized["stake"] * 100.0
                if sized["stake"] > 0 else 0.0)
    min_edge = config.min_edge_pct(book, row.get("market_type"), sized["avg_fill_price"])
    if edge_pct < min_edge:
        return None

    max_edge = config.SANITY_MAX_EDGE_PCT_PROP if is_prop else config.SANITY_MAX_EDGE_PCT
    if edge_pct > max_edge:
        _append_jsonl(config.PAPER_SANITY_REJECTED_PATH, {
            "rejected_at": (now or datetime.now(timezone.utc)).isoformat(),
            "book": book,
            "market_id": market_id,
            "side": side,
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
        "fair_prob": row.get("fair_prob"),
        "fair_prob_raw": row.get("fair_prob_raw"),
        # Refreshed two-sided Pinnacle line the placement fair was devigged from,
        # so an alternate-devig (power/Shin) backtest can replay the exact line.
        # None when the fair was interpolated (no clean price pair).
        "yes_pin_price_used": row.get("yes_pin_price_used"),
        "opposite_pin_price_used": row.get("opposite_pin_price_used"),
        # #6b: provenance of the fair this bet was placed on — True when re-fetched
        # live at decision, with the snapshot->live shift. Lets CLV/tracking grade
        # only fully-fresh placements and pins the regression.
        "fair_refreshed": row.get("fair_refreshed"),
        "pin_refetch_delta": row.get("pin_refetch_delta"),
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
    }

    with _lock:
        if key in _placed_keys:
            return None
        _append_jsonl(config.PAPER_TRADES_PATH, record)
        _placed_keys.add(key)
        _open_positions[key] = record
        _placements.append(record)

    # Outside the lock, and only once the record is actually written — the
    # in-lock dedup re-check above returns None without enqueuing.
    _enqueue_reverify(record)

    return record


# ---------------------------------------------------------------------------
# Post-fill ladder re-verification
#
# The fill above is simulated against the ladder read at decision time. Nothing
# in that path proves the offer was still standing a moment later, which is the
# difference between a bet that could have been placed and one that only looks
# placeable on paper. Each placement is re-checked once, ~2s later, and the
# surviving share count is written to its own sidecar.
#
# Diagnostic only. It never blocks, alters, or feeds back into a placement.
# ---------------------------------------------------------------------------

_PRICE_EPS = 1e-9


def _survival(levels, observed):
    """Of the shares claimed at each price, how many could still be bought at
    that price or better?

    use when: grading a paper fill against a ladder re-read moments later. Pure —
    no network, no locks, no module state. `levels` is the placement's
    `[[price, shares]]` fill detail; `observed` is the freshly fetched
    side-correct ask ladder.

    Observed liquidity is consumed as it is matched, so one resting offer can
    never satisfy two claimed levels. A better (lower) ask counts as full
    survival — price improvement is not penalised; a worse one counts as none.
    """
    remaining = sorted(([float(p), int(q)] for p, q in (observed or [])),
                       key=lambda lv: lv[0])

    per_level = []
    claimed_total = 0
    survived_total = 0
    for price, shares in (levels or []):
        price = float(price)
        want = int(shares)
        claimed_total += want
        got = 0
        for lv in remaining:
            if got >= want:
                break
            if lv[0] > price + _PRICE_EPS:
                break          # ascending, so nothing further is affordable
            if lv[1] <= 0:
                continue       # already consumed by an earlier claimed level
            take = min(lv[1], want - got)
            lv[1] -= take
            got += take
        survived_total += got
        per_level.append([price, want, got])

    return {"claimed_shares": claimed_total,
            "survived_shares": survived_total,
            "per_level": per_level}


def _enqueue_reverify(record, now=None):
    """Queue a placement for its one post-fill re-check. Returns the entry, or
    None when disabled or when the record carries no fill detail to grade."""
    if not config.PAPER_REVERIFY_ENABLED:
        return None
    levels = record.get("levels") or []
    if not levels:
        return None
    at = time.time() if now is None else now
    entry = {
        "placed_at": record.get("placed_at"),
        "book": record.get("book"),
        "market_id": record.get("market_id"),
        "side": record.get("side"),
        "levels": [[float(p), int(q)] for p, q in levels],
        "enqueued_at": at,
        "due_at": at + config.PAPER_REVERIFY_DELAY_SEC,
    }
    with _reverify_lock:
        _pending_reverify.append(entry)
    return entry


def _fold_reverify(rec):
    """Accumulate one sidecar record into the in-memory aggregate. Shared by the
    live path and `_replay_state`, so the /paper figure survives a restart."""
    if rec.get("status") == "measured":
        _reverify_agg["measured"] += 1
        _reverify_agg["claimed_shares"] += int(rec.get("claimed_shares") or 0)
        _reverify_agg["survived_shares"] += int(rec.get("survived_shares") or 0)
    else:
        _reverify_agg["skipped"] += 1


def _write_reverify(rec):
    _append_jsonl(config.PAPER_REVERIFY_PATH, rec)
    with _reverify_lock:
        _fold_reverify(rec)
    return rec


def reverify_one(entry, now=None):
    """Re-read one placement's book and record how much of the claimed liquidity
    survived. Returns the sidecar record.

    Fail-open by the established rule: a fetch error, a missing side ladder, or a
    check that arrived too late is recorded with an explicit status rather than
    raised or silently counted as a valid observation.
    """
    at = time.time() if now is None else now
    rec = {
        "reverified_at": datetime.now(timezone.utc).isoformat(),
        "placed_at": entry.get("placed_at"),
        "book": entry.get("book"),
        "market_id": entry.get("market_id"),
        "side": entry.get("side"),
        "delay_sec": round(float(at - entry["enqueued_at"]), 4),
        "target_delay_sec": config.PAPER_REVERIFY_DELAY_SEC,
        "levels": entry.get("levels"),
        "claimed_shares": sum(int(q) for _, q in entry.get("levels") or []),
        "survived_shares": None,
        "survival_rate": None,
        "per_level": None,
        "observed_levels": None,
        "status": "measured",
        "error": None,
    }

    if at - entry["due_at"] > config.PAPER_REVERIFY_TOLERANCE_SEC:
        # This measured a different moment than the one being graded. Recording
        # it as an observation would let a starved worker inflate the survival
        # rate with checks that never happened on time.
        rec["status"] = "skipped_late"
        return _write_reverify(rec)

    try:
        yes_ladder, no_ladder = adapter_for(entry["book"]).fetch_both_ladders(
            entry["market_id"])
    except Exception as e:
        rec["status"] = "fetch_error"
        rec["error"] = f"{type(e).__name__}: {e}"
        return _write_reverify(rec)

    # Index the returned pair by the placement's side. Never re-derive the
    # yes/no mapping here — Kalshi's is deliberately inverted inside its adapter.
    observed = yes_ladder if (entry.get("side") or "yes") == "yes" else no_ladder
    if observed is None:
        # Adapters without a NO side return (yes_ladder, None).
        rec["status"] = "no_ladder"
        return _write_reverify(rec)

    out = _survival(entry.get("levels"), observed)
    rec["claimed_shares"] = out["claimed_shares"]
    rec["survived_shares"] = out["survived_shares"]
    rec["per_level"] = out["per_level"]
    rec["survival_rate"] = (
        round(out["survived_shares"] / out["claimed_shares"], 6)
        if out["claimed_shares"] > 0 else None)
    rec["observed_levels"] = [[float(p), int(q)] for p, q in observed]
    return _write_reverify(rec)


def run_reverify(now=None):
    """Drain every pending entry past its due time. Returns the records written.

    Unlike close capture this is not a sweep: entries are one-shot and leave the
    queue whether they were measured or skipped, so an old bet is never
    re-checked forever.
    """
    at = time.time() if now is None else now
    due = []
    with _reverify_lock:
        while _pending_reverify and _pending_reverify[0]["due_at"] <= at:
            due.append(_pending_reverify.popleft())

    written = []
    for entry in due:
        try:
            written.append(reverify_one(entry, now=at))
        except Exception:
            # reverify_one is already fail-open; this is the backstop that stops
            # one malformed entry from stranding the rest of the drained batch.
            traceback.print_exc()
    return written


def reverify_once():
    """Paper-bound wrapper (mirrors capture_closes_once)."""
    return run_reverify()


def _reverify_loop():
    while True:
        try:
            reverify_once()
        except Exception:
            traceback.print_exc()
        time.sleep(config.PAPER_REVERIFY_POLL_SEC)


def start_reverify_thread():
    t = threading.Thread(target=_reverify_loop, daemon=True)
    t.start()
    return t


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


def _load_latest_pin_snapshot_with_age():
    """Return (rows, age_sec) for the newest Pinnacle snapshot, or (None, None).

    age_sec is wall-clock seconds since the file was written (mtime); the close
    loop gates on it so a dead/stalled poller can't have its frozen snapshot
    recorded as a close."""
    try:
        files = sorted(
            os.path.join(config.PIN_SNAPSHOT_DIR, f)
            for f in os.listdir(config.PIN_SNAPSHOT_DIR) if f.endswith(".jsonl")
        )
    except OSError:
        return None, None
    if not files:
        return None, None
    path = files[-1]
    try:
        age = time.time() - os.path.getmtime(path)
        with open(path) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return rows, age
    except (OSError, json.JSONDecodeError):
        return None, None


def _load_latest_pin_snapshot():
    """Rows-only wrapper. Kept for real_tracker, which calls this directly."""
    rows, _ = _load_latest_pin_snapshot_with_age()
    return rows


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

    three_way_ml = (mtype == "moneyline"
                    and isinstance(opp_d, str) and opp_d.startswith("not_"))

    for r in pin_rows:
        # Team/non-prop closes lock at game start: only pregame rows count.
        # Once Pinnacle flips the matchup live, no pregame row matches, so the
        # last pre-live capture stays frozen. Props are never filtered (Pinnacle
        # drops them at start; they capture continuously until then).
        if mtype != "player_prop" and r.get("isLive"):
            continue
        if r.get("matchupId") != matchup_id:
            continue
        if r.get("type") != mtype:
            continue
        if mtype != "player_prop" and r.get("period") != period:
            continue
        if mtype == "team_total" and team_side is not None and r.get("side") != team_side:
            continue

        if three_way_ml:
            # Re-synthesize the combined NO at close time so CLV devig matches
            # the placement-time `opposite_side_price` construction.
            prices_r = r.get("prices") or []
            three_way = {p.get("designation"): p.get("price") for p in prices_r
                         if p.get("designation") in ("home", "away", "draw")}
            if set(three_way.keys()) != {"home", "away", "draw"} or yes_d not in three_way:
                continue
            yes_price = three_way[yes_d]
            others = [v for k, v in three_way.items() if k != yes_d]
            opp_price = synthesize_combined_american(others)
            if yes_price is not None and opp_price is not None:
                return yes_price, opp_price
            continue

        if mtype == "player_prop":
            # Narrow by prop-child matchupId when we have one stored; else by
            # (canonical stat, line) against this record.
            if prop_matchup_id is not None and r.get("prop_matchupId") != prop_matchup_id:
                continue
            if line is not None and abs((r.get("line") or 0) - line) >= 1e-9:
                continue
            prices = r.get("prices") or []
            over_price = None
            under_price = None
            for p in prices:
                d = p.get("designation")
                if d == "over":
                    over_price = p.get("price")
                elif d == "under":
                    under_price = p.get("price")
            if over_price is not None and under_price is not None:
                yes_price = over_price if yes_d == "over" else under_price
                opp_price = under_price if yes_d == "over" else over_price
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


class CloseCaptureCtx:
    """Per-tracker state for the shared close-capture routine, so paper and real
    run one implementation over their own positions / closes / lock without
    drifting. `closes_path_attr` is the config attribute name, read at call time
    so tests can monkeypatch the path. `label` prefixes log lines."""

    __slots__ = ("lock", "open_positions", "closes_by_key", "closes_path_attr",
                 "label", "pin_stale_logged", "interp_dropped_keys")

    def __init__(self, lock, open_positions, closes_by_key, closes_path_attr, label):
        self.lock = lock
        self.open_positions = open_positions
        self.closes_by_key = closes_by_key
        self.closes_path_attr = closes_path_attr
        self.label = label
        self.pin_stale_logged = False
        self.interp_dropped_keys = set()


# paper's own context; real_tracker builds its own and calls the same routine.
_PAPER_CTX = CloseCaptureCtx(_lock, _open_positions, _closes_by_key,
                             "PAPER_CLOSES_PATH", "paper_tracker")


def _note_close_drop(ctx, record):
    """Record (once per position) that a total close couldn't be captured — the
    exact line is gone and no bracketing alternate within the cap exists. Logged
    so a silent coverage loss is visible; the set is the countable surface."""
    key = _record_key(record)
    if key and key not in ctx.interp_dropped_keys:
        ctx.interp_dropped_keys.add(key)
        print(f"[{ctx.label}] close-capture: no usable Pinnacle total line for "
              f"{record.get('book')}:{record.get('market_id')} "
              f"(line {record.get('line')} outside offered range)")


def _interpolate_total_fair(pin_rows, record):
    """Interpolate a total's closing fair (in the record's yes-designation
    perspective) from the two pregame alternate lines bracketing the placement
    line, used when the exact line is absent. Returns the fair, or None when there
    is no bracket, the bracket is wider than CLOSE_CAPTURE_INTERP_MAX_POINTS, or the
    line is out of range — never extrapolates. Each line is devigged *before*
    interpolating (interpolating raw vigged prices is biased)."""
    matchup_id = record.get("pin_matchup_id")
    period = PIN_PERIOD_LABEL_TO_INT.get(record.get("period_label"))
    yes_d = record.get("yes_designation")
    line = record.get("line")
    if matchup_id is None or period is None or line is None:
        return None

    fair_by_line = {}
    for r in pin_rows:
        if r.get("isLive"):
            continue
        if (r.get("matchupId") != matchup_id or r.get("type") != "total"
                or r.get("period") != period):
            continue
        over_px = under_px = pts = None
        for p in (r.get("prices") or []):
            d = p.get("designation")
            if d == "over":
                over_px, pts = p.get("price"), p.get("points")
            elif d == "under":
                under_px, pts = p.get("price"), p.get("points")
        if over_px is None or under_px is None or pts is None:
            continue
        yes_px = over_px if yes_d == "over" else under_px
        opp_px = under_px if yes_d == "over" else over_px
        devigged = devig_multiplicative([yes_px, opp_px])
        if devigged is not None:
            fair_by_line[pts] = devigged[0]

    lo = max((L for L in fair_by_line if L < line), default=None)
    hi = min((L for L in fair_by_line if L > line), default=None)
    if lo is None or hi is None or (hi - lo) > config.CLOSE_CAPTURE_INTERP_MAX_POINTS:
        return None
    f_lo, f_hi = fair_by_line[lo], fair_by_line[hi]
    return f_lo + (line - lo) / (hi - lo) * (f_hi - f_lo)


def _interpolate_spread_fair(pin_rows, record):
    """Spread analogue of _interpolate_total_fair (#22): interpolate a spread's
    closing fair (in the record's yes-designation perspective) from the two
    pregame alternate spread lines bracketing the placement line, used when the
    exact line is absent. Returns the fair, or None when there is no bracket, the
    bracket exceeds CLOSE_CAPTURE_INTERP_MAX_POINTS_SPREAD, or the line is out of
    range — never extrapolates. Each line is devigged before interpolating.

    Coordinate: the record `line` maps to the pin yes-designation points as
    pts = -line (see _find_pin_prices), so each alternate row's record-equivalent
    line is -(yes-designation points)."""
    matchup_id = record.get("pin_matchup_id")
    period = PIN_PERIOD_LABEL_TO_INT.get(record.get("period_label"))
    yes_d = record.get("yes_designation")
    opp_d = record.get("opposite_designation")
    line = record.get("line")
    if (matchup_id is None or period is None or line is None
            or yes_d is None or opp_d is None):
        return None

    fair_by_line = {}
    for r in pin_rows:
        if r.get("isLive"):
            continue
        if (r.get("matchupId") != matchup_id or r.get("type") != "spread"
                or r.get("period") != period):
            continue
        yes_px = opp_px = yes_pts = None
        for p in (r.get("prices") or []):
            d = p.get("designation")
            if d == yes_d:
                yes_px, yes_pts = p.get("price"), p.get("points")
            elif d == opp_d:
                opp_px = p.get("price")
        if yes_px is None or opp_px is None or yes_pts is None:
            continue
        row_line = -yes_pts
        devigged = devig_multiplicative([yes_px, opp_px])
        if devigged is not None:
            fair_by_line[row_line] = devigged[0]

    lo = max((L for L in fair_by_line if L < line), default=None)
    hi = min((L for L in fair_by_line if L > line), default=None)
    if (lo is None or hi is None
            or (hi - lo) > config.CLOSE_CAPTURE_INTERP_MAX_POINTS_SPREAD):
        return None
    f_lo, f_hi = fair_by_line[lo], fair_by_line[hi]
    return f_lo + (line - lo) / (hi - lo) * (f_hi - f_lo)


def capture_close_for(ctx, record, pin_rows, now):
    """Attempt to capture closing Pinnacle fair prob for this open position.
    Appends to ctx's closes JSONL and populates ctx.closes_by_key on success.
    Shared by paper and real trackers via their respective CloseCaptureCtx.

    Team/non-prop markets are last-write-wins until the matchup goes live: each
    pregame capture overwrites the previous, so the recorded close is the last
    line Pinnacle showed before kickoff. The lock is implicit — once the matchup
    is live, _find_pin_prices (which skips isLive rows for non-prop) returns None
    and the last pre-live value stays frozen. Player props capture continuously
    (last-seen wins) from placement onward, since Pinnacle removes props at game
    start and the close-window snapshot typically no longer contains the line."""
    book = record.get("book") or "kalshi"
    market_id = record.get("market_id") or record.get("ticker")
    if not market_id:
        return None
    side = record.get("side") or "yes"
    key = _key(book, market_id, side)
    is_prop = record.get("market_type") == "player_prop"

    start = _parse_iso(record.get("pin_start_time"))
    if start is None:
        return None
    dt_to_start = (start - now).total_seconds()
    if not is_prop and dt_to_start > config.CLOSE_CAPTURE_LEAD_SEC:
        return None
    if dt_to_start < -config.CLOSE_CAPTURE_TRAIL_SEC:
        return None

    found = _find_pin_prices(pin_rows, record)
    was_interpolated = False
    if found:
        yes_px, opp_px = found
        try:
            devigged = devig_multiplicative([yes_px, opp_px])
        except (ValueError, ZeroDivisionError):
            return None
        if devigged is None:
            return None
        yes_fair, _ = devigged
    elif record.get("market_type") in ("total", "spread"):
        # Exact line gone: interpolate from bracketing alternates (#22). Drop
        # (and count) when no usable bracket exists rather than extrapolating.
        if record.get("market_type") == "total":
            yes_fair = _interpolate_total_fair(pin_rows, record)
        else:
            yes_fair = _interpolate_spread_fair(pin_rows, record)
        if yes_fair is None:
            _note_close_drop(ctx, record)
            return None
        yes_px = opp_px = None
        was_interpolated = True
    else:
        return None

    # Store fair_prob_close in the record's side-perspective so downstream
    # CLV / fair-delta math reads directly, without branching on side.
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
        "was_interpolated": was_interpolated,
        "minutes_before_start": round(dt_to_start / 60.0, 2),
    }
    with ctx.lock:
        # Last-write-wins for both props and teams, throttled to actual moves:
        # only append when fair_prob_close changes. Replay is last-write-wins on
        # the JSONL, so this cuts write noise without affecting the captured
        # value. Teams are not locked here — the lock comes from _find_pin_prices
        # returning None once the matchup is live, so this block is unreachable
        # for a team after kickoff.
        existing = ctx.closes_by_key.get(key)
        if existing is not None and existing.get("fair_prob_close") == close["fair_prob_close"]:
            return None
        _append_jsonl(getattr(config, ctx.closes_path_attr), close)
        ctx.closes_by_key[key] = close
    return close


def _capture_close_for(record, pin_rows, now):
    """Paper-bound wrapper (preserves the existing call surface)."""
    return capture_close_for(_PAPER_CTX, record, pin_rows, now)


def _log_pin_stale(ctx, age):
    """Log once when close-capture starts skipping due to a stale/missing
    Pinnacle snapshot; reset by _note_pin_fresh so a dead poller doesn't spam
    one line per 30s tick."""
    if not ctx.pin_stale_logged:
        shown = "MISSING" if age is None else f"{age:.0f}s"
        print(f"[{ctx.label}] close-capture skipped: pinnacle snapshot stale "
              f"({shown} > {config.CLOSE_CAPTURE_MAX_PIN_AGE_SEC}s)")
        ctx.pin_stale_logged = True


def _note_pin_fresh(ctx):
    ctx.pin_stale_logged = False


def run_close_capture(ctx):
    """Check each open position once; capture closing fair-prob if within window.
    Shared by paper and real trackers (each passes its own CloseCaptureCtx).

    All open positions are re-checked every tick: props keep updating their
    last-seen fair prob until Pinnacle drops the line, and teams keep updating
    (last-write-wins) until the matchup goes live and the value locks. Skips the
    whole tick when the latest Pinnacle snapshot is stale (poller down)."""
    with ctx.lock:
        pending = list(ctx.open_positions.values())
    if not pending:
        return
    # Drop any whose startTime is already outside the capture window, so we
    # don't load a snapshot we won't use. Props use no lead cutoff so we
    # capture continuously from placement onward.
    now = datetime.now(timezone.utc)
    relevant = []
    for r in pending:
        start = _parse_iso(r.get("pin_start_time"))
        if start is None:
            continue
        dt = (start - now).total_seconds()
        if dt < -config.CLOSE_CAPTURE_TRAIL_SEC:
            continue
        is_prop = r.get("market_type") == "player_prop"
        if not is_prop and dt > config.CLOSE_CAPTURE_LEAD_SEC:
            continue
        relevant.append(r)
    if not relevant:
        return

    pin_rows, pin_age = _load_latest_pin_snapshot_with_age()
    if pin_rows is None:
        return
    if pin_age is None or pin_age > config.CLOSE_CAPTURE_MAX_PIN_AGE_SEC:
        _log_pin_stale(ctx, pin_age)
        return
    _note_pin_fresh(ctx)
    for r in relevant:
        try:
            capture_close_for(ctx, r, pin_rows, now)
        except Exception:
            traceback.print_exc()


def capture_closes_once():
    """Paper-bound wrapper (preserves the existing call surface)."""
    run_close_capture(_PAPER_CTX)


def _close_capture_loop():
    while True:
        try:
            capture_closes_once()
        except Exception:
            traceback.print_exc()
        time.sleep(config.CLOSE_CAPTURE_POLL_SEC)


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
    side = record.get("side") or "yes"
    adapter = adapter_for(book)
    try:
        result = adapter.fetch_settlement(market_id)
    except Exception as e:
        print(f"[paper_tracker] settlement fetch failed {book}:{market_id}: {e}")
        return None
    # Binary markets return "yes"/"no"; scalar markets return a float YES-payout.
    is_scalar = isinstance(result, float)
    if not is_scalar and result not in ("yes", "no"):
        return None

    shares = record["shares"]
    avg_fill = record["avg_fill_price"]
    total_stake = record.get("stake", 0.0)

    if is_scalar:
        # Scalar resolution: each YES share pays `result`, each NO share pays
        # 1-result. Fee is upfront (fee_on_win=0), so payout is the full share.
        fee_on_win = adapter.fee_on_win_per_share(avg_fill)
        my_payout = result if side == "yes" else 1.0 - result
        gross_return = shares * my_payout * (1.0 - fee_on_win)
        net_pnl = gross_return - total_stake
    else:
        # NO contracts win when the underlying settles "no"; YES contracts win
        # when it settles "yes". Winning contracts always pay $1/share minus fee.
        won = (result == side)
        if won:
            fee_on_win = adapter.fee_on_win_per_share(avg_fill)
            gross_return = shares * (1.0 - fee_on_win)
            net_pnl = gross_return - total_stake
        else:
            gross_return = 0.0
            net_pnl = -total_stake

    key = _key(book, market_id, side)
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
            "side": side,
            "pin_matchup": record.get("pin_matchup"),
            "pin_sport": record.get("pin_sport"),
            "pin_start_time": record.get("pin_start_time"),
            "market_type": record.get("market_type"),
            "period_label": record.get("period_label"),
            "line": record.get("line"),
            "selection": record.get("selection"),
            "yes_side_label": record.get("yes_side_label"),
            "result": "scalar" if is_scalar else result,
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
        if is_scalar:
            settlement["settlement_value"] = round(my_payout, 4)
        close = _closes_by_key.get(key)
        if close is not None:
            fpc = close.get("fair_prob_close")
            settlement["fair_prob_close"] = fpc
            if isinstance(avg_fill, (int, float)) and isinstance(fpc, (int, float)):
                settlement["clv"] = round(fpc - avg_fill, 6)
            # expected_profit stays placement-basis; close-basis EV is computed on
            # the fly in snapshot() as net_ev_close.
        _append_jsonl(config.PAPER_SETTLEMENTS_PATH, settlement)
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
        time.sleep(config.SETTLEMENT_POLL_SEC)


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
    total_pnl = round(bankroll - config.PAPER_INITIAL_BANKROLL, 4)
    settled_stake = sum(s.get("stake", 0.0) for s in settled)
    settled_pnl = sum(s.get("net_pnl", 0.0) for s in settled)
    roi_pct = (settled_pnl / settled_stake * 100) if settled_stake > 0 else 0.0

    # Voids contribute to none of the edge/CLV diagnostics below.
    settled_live = [s for s in settled if s.get("result") != "void"]

    # net_ev_close: close basis (edge vs raw devigged closing fair), CLV-consistent,
    # summed over settled bets that captured a Pinnacle close. Voids excluded.
    net_ev_close = 0.0
    net_ev_close_samples = 0
    for s in settled_live:
        fpc = s.get("fair_prob_close")
        place_ev = s.get("expected_profit")
        if not isinstance(fpc, (int, float)) or not isinstance(place_ev, (int, float)):
            continue
        close_ev = _expected_profit_at(s, fpc)
        if close_ev is None:
            continue
        net_ev_close += close_ev
        net_ev_close_samples += 1
    net_ev_close = round(net_ev_close, 4)

    # CLV is value vs the price paid, so it uses the raw closing fair (the true
    # market line, as stored at settlement), not the haircut one. Drawn from
    # settled_live for void-consistency with the EV bases.
    clv_vals = [s["clv"] for s in settled_live if isinstance(s.get("clv"), (int, float))]
    avg_clv = round(sum(clv_vals) / len(clv_vals), 6) if clv_vals else None
    clv_positive = sum(1 for c in clv_vals if c > 0)
    clv_samples = len(clv_vals)

    # Fair Δ measures line movement between placement and close, so both fairs must
    # share a basis: the placement fair is haircut, so haircut the close fair too.
    fair_deltas = [config.haircut_fair(s["fair_prob_close"]) - s["fair_prob"]
                   for s in settled_live
                   if isinstance(s.get("fair_prob_close"), (int, float))
                   and isinstance(s.get("fair_prob"), (int, float))]
    avg_fair_delta = round(sum(fair_deltas) / len(fair_deltas), 6) if fair_deltas else None
    fair_delta_positive = sum(1 for d in fair_deltas if d > 0)
    fair_delta_samples = len(fair_deltas)

    # Post-fill ladder survival. The headline rate counts only in-tolerance
    # measured checks; `skipped` sits beside it so a high failure rate cannot
    # hide behind a healthy-looking percentage.
    with _reverify_lock:
        reverify = dict(_reverify_agg)
    reverify["survival_pct"] = (
        round(reverify["survived_shares"] / reverify["claimed_shares"] * 100, 2)
        if reverify["claimed_shares"] > 0 else None)

    return {
        "bankroll": round(bankroll, 4),
        "initial_bankroll": config.PAPER_INITIAL_BANKROLL,
        "kelly_fraction": config.KELLY_FRACTION,
        "open_positions": open_list,
        "settled": settled,
        "reverify": reverify,
        "summary": {
            "total_placed": total_placed,
            "total_settled": total_settled,
            "open": len(open_list),
            "total_pnl": total_pnl,
            "net_ev_close": net_ev_close,
            "net_ev_close_samples": net_ev_close_samples,
            "roi_pct": round(roi_pct, 2),
            "avg_clv": avg_clv,
            "clv_samples": clv_samples,
            "clv_positive": clv_positive,
            "avg_fair_delta": avg_fair_delta,
            "fair_delta_samples": fair_delta_samples,
            "fair_delta_positive": fair_delta_positive,
        },
    }
