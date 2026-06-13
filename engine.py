#!/usr/bin/env python3
"""Headless EV scan engine.

The scan -> match -> evaluate -> rank pipeline, extracted from
`ev_dashboard.scan_once` (#2) so it runs without Flask and so any caller (the
dashboard loop, a CLI, an event-trigger) owns the placement decision.

The engine is SIDE-EFFECT FREE: it never calls `paper_tracker.maybe_place` /
`real_tracker.maybe_place`. `scan()` returns the result dict plus a `placements`
list of `(side_row, ladder)` pairs; the caller decides whether to place them.
Per-candidate exceptions and the ladder-fetch timeout are swallowed so one bad
market never kills the scan.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from adapters import adapter_for, all_adapters
from data_utils import read_latest_snapshot_meta, stale_snapshot_reason
from find_ev_bet import (
    SNAP_PIN,
    MAX_PIN_SNAPSHOT_AGE_SEC,
    MAX_SOFT_SNAPSHOT_AGE_SEC,
    american_to_decimal,
    breakeven_fair,
    evaluate,
    find_matches,
    load_latest_snapshot,
    price_to_american,
)
import config

LADDER_FETCH_TIMEOUT_SEC = config.LADDER_FETCH_TIMEOUT_SEC
_LADDER_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ladder")
# Superset matcher produces candidates across ML / spread / total / team_total
# and across every registered book. A larger slice lets the client-side filter
# chips surface meaningful rows per slice without a server round-trip.
TOP_N = 25


def snapshot_dirs():
    """Every snapshot dir the scan reads: Pinnacle + each soft adapter's. Used by
    the #1 event trigger to watch for new writes."""
    return [SNAP_PIN] + [a.SNAPSHOT_DIR for a in all_adapters()]


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


def scan():
    """Run one scan and return the result dict + placements. No side effects.

    Returns a dict with: pin_age, book_ages, cross_book_skew_sec, stats, rows
    (top N), prop_rows, and placements (list of (side_row, ladder) for the
    caller to place). Raises RuntimeError on missing snapshots or a failed
    freshness gate, exactly as the old scan_once did."""
    pin_rows, pin_age = load_latest_snapshot(SNAP_PIN)
    if pin_rows is None:
        raise RuntimeError("missing pinnacle snapshot")
    soft_markets, book_ages = _load_soft_markets()
    if not soft_markets:
        raise RuntimeError("no soft-book snapshots")
    reason = stale_snapshot_reason(
        pin_age, book_ages,
        MAX_PIN_SNAPSHOT_AGE_SEC, MAX_SOFT_SNAPSHOT_AGE_SEC,
        missing_soft_is_stale=True,
    )
    if reason:
        raise RuntimeError(reason)

    pin_meta = read_latest_snapshot_meta(SNAP_PIN)
    pin_poll_sec = (pin_meta or {}).get("cycle_elapsed_sec")
    book_poll_sec_map = {}
    for adapter in all_adapters():
        meta = read_latest_snapshot_meta(adapter.SNAPSHOT_DIR)
        if meta:
            book_poll_sec_map[adapter.BOOK] = meta.get("cycle_elapsed_sec")

    candidates, stats = find_matches(pin_rows, soft_markets)

    rows = []
    placements = []
    for c in candidates:
        nm = c["market"]
        book = c["book"]
        adapter = adapter_for(book)
        try:
            fut = _LADDER_EXECUTOR.submit(adapter.fetch_both_ladders, c["market_id"])
            yes_ladder, no_ladder = fut.result(timeout=LADDER_FETCH_TIMEOUT_SEC)
        except FutureTimeout:
            print(f"[engine.scan] ladder fetch timeout {book}:{c['market_id']}")
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

            # The caller places a Kelly-sized bet from this (side_row, ladder)
            # pair the first time this (book, market_id, side) enters the
            # in_window bucket — the engine itself never places.
            book_elapsed = book_poll_sec_map.get(book)
            total_elapsed = (
                pin_poll_sec + book_elapsed
                if pin_poll_sec is not None and book_elapsed is not None
                else None
            )
            # #7: carry the side's raw (pre-haircut) devig so the placed record
            # and the haircut backtest can replay without double-haircutting.
            fair_raw = (c.get("yes_fair_raw") if side == "yes"
                        else c.get("opposite_fair_raw"))
            side_row = {
                **c,
                "side": side,
                "fair_prob": fair,
                "fair_prob_raw": fair_raw,
                "selection": selection,
                "pin_poll_sec": pin_poll_sec,
                "book_poll_sec": book_elapsed,
                "total_poll_sec": total_elapsed,
            }
            placements.append((side_row, ladder))

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

    # Cross-book capture skew: how far apart in wall-clock the Pinnacle snapshot
    # and the furthest soft snapshot were captured. age = now - captured_at for
    # both, so the skew is just |book_age - pin_age|. Surfaced, not gated — the
    # soft ladder is re-fetched live at decision time.
    soft_skews = [abs(a - pin_age) for a in book_ages.values() if a is not None]
    cross_book_skew_sec = max(soft_skews) if soft_skews else None

    return {
        "pin_age": pin_age,
        "book_ages": book_ages,
        "cross_book_skew_sec": cross_book_skew_sec,
        "stats": stats,
        "rows": rows[:TOP_N],
        "prop_rows": prop_rows,
        "placements": placements,
    }
