#!/usr/bin/env python3
"""
Single +EV bet finder (multi-book).

Reads the latest Pinnacle snapshot plus one snapshot per registered
soft-book adapter, normalizes every soft-book row to a
`NormalizedMarket`, runs `match_all_markets` to pair each with its
Pinnacle 2-way counterpart across moneyline / spread / total /
team-total (FULL + 1H/2H half-point lines), devigs per-line, pulls the
live YES-ask ladder from each book via the adapter, and walks the
ladder under the book's own fee model. Prints the single best +EV
opportunity found across all books.

Run: python3 find_ev_bet.py
"""
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

from adapters import all_adapters, adapter_for
from devig_utils import american_to_decimal, devig_multiplicative
from market_matcher import match_all_markets, parse_iso
import config

# Re-export from config so ev_dashboard can still import these from here.
SNAP_PIN           = config.PIN_SNAPSHOT_DIR
MAX_SNAPSHOT_AGE_SEC = config.MAX_SNAPSHOT_AGE_SEC
MIN_HOURS_TO_START = config.MIN_HOURS_TO_START
MAX_HOURS_TO_START = config.MAX_HOURS_TO_START
PROP_MIN_EDGE_PCT  = config.PROP_MIN_EDGE_PCT


# ---------------------------------------------------------------------------
# Price helpers (book-agnostic)
# ---------------------------------------------------------------------------

def price_to_american(price):
    """YES-share price in dollars (0<p<1) -> American odds int."""
    if price <= 0 or price >= 1:
        return 0
    if price <= 0.5:
        return int(round(100 * (1 - price) / price))
    return int(round(-100 * price / (1 - price)))


def fmt_book_price(price):
    am = price_to_american(price)
    return f"{am:+d} ({price*100:.1f}¢)"


def load_latest_snapshot(dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, "*.jsonl")))
    if not files:
        return None, None
    path = files[-1]
    age = time.time() - os.path.getmtime(path)
    with open(path) as f:
        rows = [json.loads(l) for l in f]
    return rows, age


# ---------------------------------------------------------------------------
# Ladder walk (book fee model plugged in via fee_fn)
# ---------------------------------------------------------------------------

def walk_ladder(ladder, fair_prob, fee_fn):
    """Consume levels while expected EV/share > 0. Returns per-level + totals.

    `fee_fn(price, fair_prob)` returns the expected per-share fee in dollars
    at the given fill price.
    """
    levels = []
    total_shares, total_stake, total_ev = 0, 0.0, 0.0
    for price, qty in ladder:
        gross = 1.0 - price
        ev = fair_prob * gross - (1 - fair_prob) * price - fee_fn(price, fair_prob)
        if ev <= 0:
            break
        levels.append((price, qty, ev))
        total_shares += qty
        total_stake += qty * price
        total_ev += qty * ev
    return total_shares, total_stake, total_ev, levels


def breakeven_fair(price, fee_fn):
    """Numerically solve for the fair probability at which EV == 0 at this price.

    EV(q) = q*(1-p) - (1-q)*p - fee_fn(p, q) is monotonically increasing in q,
    so bisection on [0, 1] converges quickly.
    """
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        ev = mid * (1 - price) - (1 - mid) * price - fee_fn(price, mid)
        if ev > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# Match + candidate pipeline
# ---------------------------------------------------------------------------

def _load_soft_markets():
    """Load latest snapshot from every adapter and normalize rows.

    Returns (soft_markets, ages_dict). Missing snapshots surface as None.
    """
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


def find_matches(pin_rows, soft_markets):
    """Pair every soft-book YES market with its Pinnacle 2-way line.

    Delegates matching (including 1H/2H half markets and alternate-line
    selection) to `market_matcher.match_all_markets`, then applies EV gates:
    drop games already started, soft-tag the 0.5-3h window, devig per line.
    """
    report = match_all_markets(pin_rows, soft_markets)
    ml_stats = report.moneyline_report.stats
    nonml_stats = report.stats

    stats = {
        "soft_moneyline_scope": ml_stats.get("soft_moneyline_scope", 0),
        "soft_nonml_scope": nonml_stats.get("nonml_total", 0),
        "soft_in_scope": ml_stats.get("soft_moneyline_scope", 0)
                         + nonml_stats.get("nonml_total", 0),
        "pin_moneyline_scope": ml_stats.get("pin_moneyline_scope", 0),
        "matched_moneyline": nonml_stats["matched_by_type"].get("moneyline", 0),
        "matched_spread": nonml_stats["matched_by_type"].get("spread", 0),
        "matched_total": nonml_stats["matched_by_type"].get("total", 0),
        "matched_team_total": nonml_stats["matched_by_type"].get("team_total", 0),
        "matched_player_prop": nonml_stats["matched_by_type"].get("player_prop", 0),
        "three_way_matched": nonml_stats.get("moneyline_three_way_matched", 0),
        "yes_side_unknown": nonml_stats.get("moneyline_yes_side_unresolved", 0),
        "team_total_side_missing": nonml_stats.get("team_total_side_missing", 0),
        "nonml_no_event_match": nonml_stats.get("no_event_match", 0),
        "nonml_no_pin_market": nonml_stats.get("no_pin_market", 0),
        "nonml_no_line_match": nonml_stats.get("no_line_match", 0),
        "prop_unmatched_reasons": nonml_stats.get("prop_unmatched_reasons", {}),
        "prop_pin_scope": nonml_stats.get("prop_pin_scope", 0),
        "already_started": 0,
        "outside_start_window": 0,
    }
    stats["matched"] = (stats["matched_moneyline"] + stats["matched_spread"]
                        + stats["matched_total"] + stats["matched_team_total"]
                        + stats["matched_player_prop"])

    now = datetime.now(timezone.utc)
    min_start = now + timedelta(hours=MIN_HOURS_TO_START)
    max_start = now + timedelta(hours=MAX_HOURS_TO_START)

    candidates = []
    for pair in report.pairs:
        nm = pair.market
        pin_start = parse_iso(pair.pin_start)
        if pin_start is None or pin_start <= now:
            stats["already_started"] += 1
            continue

        in_window = min_start <= pin_start <= max_start
        if not in_window:
            stats["outside_start_window"] += 1

        devigged = devig_multiplicative(
            [pair.yes_side_price, pair.opposite_side_price]
        )
        if devigged is None:
            stats["devig_failed"] = stats.get("devig_failed", 0) + 1
            continue
        yes_fair, opp_fair = devigged

        adapter = adapter_for(nm.book)
        candidates.append({
            "market": nm,
            "book": nm.book,
            "market_id": nm.market_id,
            "market_url": adapter.market_url(nm),
            "pin_matchup": pair.pin_matchup,
            "pin_matchup_id": pair.pin_matchup_id,
            "pin_home_name": pair.pin_home_name,
            "pin_away_name": pair.pin_away_name,
            "pin_start_time": pair.pin_start,
            "pin_sport": pair.pin_sport,
            "market_type": pair.market_type,
            "period_label": pair.period_label,
            "line": pair.line,
            "selection": pair.selection,
            "yes_side_label": pair.yes_side_label,
            "opposite_side_label": pair.opposite_side_label,
            "yes_designation": pair.yes_designation,
            "opposite_designation": pair.opposite_designation,
            "yes_side_price": pair.yes_side_price,
            "opposite_side_price": pair.opposite_side_price,
            "yes_fair": yes_fair,
            "opposite_fair": opp_fair,
            "fair_prob": yes_fair,
            "yes_pin_name": pair.yes_side_label,
            "yes_soft_name": nm.yes_sub_title or (nm.team or ""),
            "in_window": in_window,
            "player": nm.player,
            "stat": nm.stat,
            "pin_prop_matchup_id": pair.pin_prop_matchup_id,
        })

    return candidates, stats


def main():
    pin_rows, pin_age = load_latest_snapshot(SNAP_PIN)
    if pin_rows is None:
        print("ERROR: no Pinnacle snapshots found")
        sys.exit(1)

    soft_markets, soft_ages = _load_soft_markets()
    if not soft_markets:
        print("ERROR: no soft-book snapshots found")
        sys.exit(1)

    if pin_age > MAX_SNAPSHOT_AGE_SEC:
        print(f"ERROR: pinnacle snapshot too old ({pin_age:.0f}s)")
        sys.exit(1)
    for book, age in soft_ages.items():
        if age is None:
            continue
        if age > MAX_SNAPSHOT_AGE_SEC:
            print(f"ERROR: {book} snapshot too old ({age:.0f}s)")
            sys.exit(1)

    ages_str = "  ".join(
        f"{book}={age:.0f}s" if age is not None else f"{book}=MISSING"
        for book, age in soft_ages.items()
    )
    print(f"[snapshots] pinnacle age={pin_age:.0f}s  {ages_str}")
    print(f"[snapshots] pin_rows={len(pin_rows)}  soft_normalized={len(soft_markets)}")

    candidates, stats = find_matches(pin_rows, soft_markets)
    print(f"[match] {stats}")

    if not candidates:
        print("\nNo matched markets. Nothing to evaluate.")
        sys.exit(0)

    results, near_misses = [], []
    for c in candidates:
        nm = c["market"]
        adapter = adapter_for(nm.book)
        try:
            yes_ladder, no_ladder = adapter.fetch_both_ladders(nm.market_id)
        except Exception as e:
            print(f"  ! orderbook failed {nm.book}:{nm.market_id}: {e}")
            continue

        sides = [("yes", yes_ladder, c["yes_fair"])]
        if getattr(adapter, "SUPPORTS_NO_SIDE", False) and no_ladder:
            sides.append(("no", no_ladder, c["opposite_fair"]))

        fee_fn = adapter.taker_fee_per_share

        for side, ladder, fair in sides:
            if not ladder:
                continue
            shares, stake, exp_profit, levels = walk_ladder(ladder, fair, fee_fn)
            best_ask, best_qty = ladder[0]
            side_ctx = {**c, "side": side, "fair_prob": fair}
            if shares == 0:
                gross = 1 - best_ask
                ev = fair * gross - (1 - fair) * best_ask - fee_fn(best_ask, fair)
                near_misses.append((ev, side_ctx, best_ask, best_qty))
                continue
            ev_pct = exp_profit / stake * 100 if stake > 0 else 0
            # Props carry a higher min-edge gate (Pinnacle prop max-stake is ~$250
            # vs ~$7.5k+ for team markets — noisier quotes need a bigger cushion).
            if c["market_type"] == "player_prop" and ev_pct < PROP_MIN_EDGE_PCT:
                continue
            results.append({
                **side_ctx, "shares": shares, "stake": stake,
                "expected_profit": exp_profit, "ev_pct": ev_pct,
                "levels": levels, "ladder_top": ladder[:10],
            })

    print(f"[eval] +EV markets: {len(results)}  near-misses: {len(near_misses)}")

    if not results:
        print("\nNo +EV opportunities found at current prices.")
        if near_misses:
            near_misses.sort(reverse=True, key=lambda x: x[0])
            ev, c, ask, qty = near_misses[0]
            nm = c["market"]
            adapter = adapter_for(nm.book)
            side_label = (c.get("side") or "yes").upper()
            print(f"Best near-miss:")
            print(f"  [{nm.book}] {nm.market_id}  ({nm.title})")
            print(f"  Pinnacle: {c['pin_matchup']}  [{c['market_type']} {c['period_label']}]  "
                  f"{c['yes_side_price']:+d}/{c['opposite_side_price']:+d}")
            print(f"  side={side_label} on '{c['selection']}'  fair_prob={c['fair_prob']:.4f}")
            print(f"  best {nm.book} {side_label} ask: {fmt_book_price(ask)}  qty={qty:,}  ev_per_share={ev:+.4f}")
            print(f"  breakeven fair prob at this price: "
                  f"{breakeven_fair(ask, adapter.taker_fee_per_share):.4f}")
        sys.exit(0)

    results.sort(key=lambda r: (r["expected_profit"], r["ev_pct"]), reverse=True)
    best = results[0]
    nm = best["market"]
    adapter = adapter_for(nm.book)

    print()
    print("=" * 72)
    print(f"BEST +EV BET   [{nm.book}]")
    print("=" * 72)
    print(f"Sport / matchup:   {best.get('pin_sport')}  -  {best['pin_matchup']}")
    print(f"Market:            {best['market_type']} {best['period_label']}"
          + (f"  line={best['line']:g}" if best.get('line') is not None else ""))
    print(f"{nm.book} title:     {nm.title}")
    print(f"{nm.book} market_id: {nm.market_id}")
    print(f"Event id:          {nm.event_id}")
    print(f"Game start (UTC):  {nm.start_time}   (Pinnacle: {best.get('pin_start_time')})")
    print()
    vig_pct = (1/american_to_decimal(best['yes_side_price'])
               + 1/american_to_decimal(best['opposite_side_price']) - 1) * 100
    print(f"Pinnacle 2-way:      {best['yes_side_label']} {best['yes_side_price']:+d}  "
          f"|  {best['opposite_side_label']} {best['opposite_side_price']:+d}")
    print(f"Devigged fair prob:  yes {best['yes_fair']:.4f}  |  opp {best['opposite_fair']:.4f}  "
          f"(multiplicative devig, vig={vig_pct:.2f}%)")
    print()
    best_side = (best.get("side") or "yes").upper()
    print(f"Bet:  BUY {best_side} on '{best['selection']}'   ({nm.book} side: '{best['yes_soft_name']}')")
    print(f"Fair probability of this side: {best['fair_prob']:.4f}")
    print(f"URL:  {best['market_url']}")
    print()
    print(f"{best_side}-ask ladder walked (fee model: {nm.book}):")
    print(f"  {'ask':>18}  {'qty':>10}  {'ev/share':>10}  {'ev%':>8}  {'breakeven fair':>14}")
    for price, qty, ev in best["levels"]:
        be = breakeven_fair(price, adapter.taker_fee_per_share)
        print(f"  {fmt_book_price(price):>18}  {qty:>10,}  {ev:>+10.4f}  "
              f"{ev/price*100:>+7.2f}%  {be:>14.4f}")
    print()
    print(f"Totals")
    print(f"  Max shares at +EV:             {best['shares']:,}")
    print(f"  Total stake:                   ${best['stake']:,.2f}")
    print(f"  Expected profit (after fee):   ${best['expected_profit']:,.2f}")
    print(f"  Overall EV on stake:           {best['ev_pct']:+.2f}%")
    last_price = best["levels"][-1][0]
    print(f"  Worst price consumed:          {fmt_book_price(last_price)}  "
          f"(breakeven fair={breakeven_fair(last_price, adapter.taker_fee_per_share):.4f})")
    print()
    print(f"Runner-ups (top 5 by expected profit):")
    for r in results[1:6]:
        tag = f"[{r['book'][:2]}/{r['market_type'][:2]}"
        tag += "" if r['period_label'] == "FULL" else f"/{r['period_label']}"
        tag += "]"
        side_label = (r.get("side") or "yes").upper()
        print(f"  ${r['expected_profit']:>8,.2f} EV  "
              f"{r['ev_pct']:>+5.2f}%  {r['shares']:>8,} sh  "
              f"{r['market_id']}  {tag} {side_label} {r['selection']}")


if __name__ == "__main__":
    main()
