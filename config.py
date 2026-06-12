"""
Central configuration for the EV scanner.

Single source of truth for all tunable values: market enablement, edge thresholds,
bankroll/risk limits, timing constants, feature flags, and file paths.

Secrets and API keys remain in their respective adapters.
SHARP_SOCCER_LEAGUES (and its SOCCER_WHITELIST_DISABLE env read) stays in
adapters/common.py to avoid inverting the import dependency.
"""
import os
from typing import Optional

from adapters.common import LEAGUE_TO_PIN_SPORT

# ---------------------------------------------------------------------------
# 1. Market enablement  (moved verbatim from market_config.py)
# ---------------------------------------------------------------------------

LEAGUE_OVERRIDES: dict[tuple[str, str, str], bool] = {
    # none yet — add per-league carve-outs here
}

SPORT_TOGGLES: dict[tuple[str, str, str], bool] = {

    # ------------------------------------------------------------------ kalshi
    # Basketball — moneyline killed: aggregate -$517 / 61 bets (~16% CLV retention)
    ("kalshi", "Basketball",              "moneyline"):   False,
    ("kalshi", "Basketball",              "spread"):      True,
    ("kalshi", "Basketball",              "total"):       True,
    ("kalshi", "Basketball",              "team_total"):  True,
    ("kalshi", "Basketball",              "player_prop"): True,
    # Hockey
    ("kalshi", "Hockey",                  "moneyline"):   True,
    ("kalshi", "Hockey",                  "spread"):      True,
    ("kalshi", "Hockey",                  "total"):       True,
    ("kalshi", "Hockey",                  "team_total"):  True,
    ("kalshi", "Hockey",                  "player_prop"): True,
    # Baseball
    ("kalshi", "Baseball",                "moneyline"):   True,
    ("kalshi", "Baseball",                "spread"):      True,
    ("kalshi", "Baseball",                "total"):       True,
    ("kalshi", "Baseball",                "team_total"):  True,
    ("kalshi", "Baseball",                "player_prop"): True,
    # Football
    ("kalshi", "Football",                "moneyline"):   True,
    ("kalshi", "Football",                "spread"):      True,
    ("kalshi", "Football",                "total"):       True,
    ("kalshi", "Football",                "team_total"):  True,
    ("kalshi", "Football",                "player_prop"): True,
    # Soccer — moneyline killed: EV-negative in aggregate; whitelist still enforced
    ("kalshi", "Soccer",                  "moneyline"):   False,
    ("kalshi", "Soccer",                  "spread"):      True,
    ("kalshi", "Soccer",                  "total"):       True,
    ("kalshi", "Soccer",                  "team_total"):  True,
    ("kalshi", "Soccer",                  "player_prop"): True,
    # Golf
    ("kalshi", "Golf",                    "moneyline"):   True,
    ("kalshi", "Golf",                    "spread"):      True,
    ("kalshi", "Golf",                    "total"):       True,
    ("kalshi", "Golf",                    "team_total"):  True,
    ("kalshi", "Golf",                    "player_prop"): True,
    # Australian Rules Football
    ("kalshi", "Australian Rules Football", "moneyline"):   True,
    ("kalshi", "Australian Rules Football", "spread"):      True,
    ("kalshi", "Australian Rules Football", "total"):       True,
    ("kalshi", "Australian Rules Football", "team_total"):  True,
    ("kalshi", "Australian Rules Football", "player_prop"): True,
    # Cricket
    ("kalshi", "Cricket",                 "moneyline"):   True,
    ("kalshi", "Cricket",                 "spread"):      True,
    ("kalshi", "Cricket",                 "total"):       True,
    ("kalshi", "Cricket",                 "team_total"):  True,
    ("kalshi", "Cricket",                 "player_prop"): True,

    # --------------------------------------------------------------- polymarket
    # Basketball
    ("polymarket", "Basketball",              "moneyline"):   True,
    ("polymarket", "Basketball",              "spread"):      True,
    ("polymarket", "Basketball",              "total"):       True,
    ("polymarket", "Basketball",              "team_total"):  True,
    ("polymarket", "Basketball",              "player_prop"): True,
    # Hockey
    ("polymarket", "Hockey",                  "moneyline"):   True,
    ("polymarket", "Hockey",                  "spread"):      True,
    ("polymarket", "Hockey",                  "total"):       True,
    ("polymarket", "Hockey",                  "team_total"):  True,
    ("polymarket", "Hockey",                  "player_prop"): True,
    # Baseball
    ("polymarket", "Baseball",                "moneyline"):   True,
    ("polymarket", "Baseball",                "spread"):      True,
    ("polymarket", "Baseball",                "total"):       True,
    ("polymarket", "Baseball",                "team_total"):  True,
    ("polymarket", "Baseball",                "player_prop"): True,
    # Football
    ("polymarket", "Football",                "moneyline"):   True,
    ("polymarket", "Football",                "spread"):      True,
    ("polymarket", "Football",                "total"):       True,
    ("polymarket", "Football",                "team_total"):  True,
    ("polymarket", "Football",                "player_prop"): True,
    # Soccer — moneyline killed: rare on Polymarket but explicit for consistency
    ("polymarket", "Soccer",                  "moneyline"):   False,
    ("polymarket", "Soccer",                  "spread"):      True,
    ("polymarket", "Soccer",                  "total"):       True,
    ("polymarket", "Soccer",                  "team_total"):  True,
    ("polymarket", "Soccer",                  "player_prop"): True,
    # Golf
    ("polymarket", "Golf",                    "moneyline"):   True,
    ("polymarket", "Golf",                    "spread"):      True,
    ("polymarket", "Golf",                    "total"):       True,
    ("polymarket", "Golf",                    "team_total"):  True,
    ("polymarket", "Golf",                    "player_prop"): True,
    # Australian Rules Football
    ("polymarket", "Australian Rules Football", "moneyline"):   True,
    ("polymarket", "Australian Rules Football", "spread"):      True,
    ("polymarket", "Australian Rules Football", "total"):       True,
    ("polymarket", "Australian Rules Football", "team_total"):  True,
    ("polymarket", "Australian Rules Football", "player_prop"): True,
    # Cricket
    ("polymarket", "Cricket",                 "moneyline"):   True,
    ("polymarket", "Cricket",                 "spread"):      True,
    ("polymarket", "Cricket",                 "total"):       True,
    ("polymarket", "Cricket",                 "team_total"):  True,
    ("polymarket", "Cricket",                 "player_prop"): True,
}

_KNOWN_BOOKS = ("kalshi", "polymarket")
_KNOWN_MARKET_TYPES = ("moneyline", "spread", "total", "team_total", "player_prop")
_KNOWN_SPORTS = set(LEAGUE_TO_PIN_SPORT.values())


def _assert_coverage():
    missing = []
    for book in _KNOWN_BOOKS:
        for sport in _KNOWN_SPORTS:
            for mtype in _KNOWN_MARKET_TYPES:
                if (book, sport, mtype) not in SPORT_TOGGLES:
                    missing.append((book, sport, mtype))
    if missing:
        raise RuntimeError(
            "[config] SPORT_TOGGLES is missing entries — add them before deploying:\n"
            + "\n".join(f"  {b!r}, {s!r}, {m!r}" for b, s, m in sorted(missing))
        )

    unknown_leagues = [
        league for (_, league, _) in LEAGUE_OVERRIDES
        if league not in LEAGUE_TO_PIN_SPORT
    ]
    if unknown_leagues:
        raise RuntimeError(
            "[config] LEAGUE_OVERRIDES contains league codes not in LEAGUE_TO_PIN_SPORT: "
            + ", ".join(sorted(set(unknown_leagues)))
        )


_assert_coverage()

_unconfigured_seen: set[tuple[str, str, str, str]] = set()


def market_enabled(book: str, sport: str, league: Optional[str], market_type: str) -> bool:
    """Return True if this (book, sport, market_type) combo is enabled.
    Use when: deciding whether to include a matched pair in the output list
    (non-ML markets: drop in place; ML markets: suppress at output, not match time,
    so event siblings still resolve).

    Resolution order:
      1. MARKET_TOGGLES_DISABLE=1 → always True (backtest bypass).
      2. LEAGUE_OVERRIDES[(book, league.upper(), market_type)] if set.
      3. SPORT_TOGGLES[(book, sport, market_type)] if set.
      4. Fail-closed: log once and return False.
    """
    if os.getenv("MARKET_TOGGLES_DISABLE") == "1":
        return True

    if league:
        override = LEAGUE_OVERRIDES.get((book, league.upper(), market_type))
        if override is not None:
            return override

    toggle = SPORT_TOGGLES.get((book, sport, market_type))
    if toggle is not None:
        return toggle

    key = (book, sport, league or "", market_type)
    if key not in _unconfigured_seen:
        _unconfigured_seen.add(key)
        print(
            f"[config] blocking unconfigured market "
            f"book={book!r} sport={sport!r} league={league!r} type={market_type!r} "
            f"— add to SPORT_TOGGLES or LEAGUE_OVERRIDES in config.py"
        )
    return False


# ---------------------------------------------------------------------------
# 2. Edge thresholds
# ---------------------------------------------------------------------------

# Single source for taker fee rates (used by both the edge formula and the
# read adapters). Update here when a book changes its fee schedule.
PER_BOOK_FEE_RATE: dict[str, float] = {
    "kalshi":     0.07,   # Kalshi fee schedule effective 2026-02-05
    "polymarket": 0.03,   # Polymarket sports rate
}

# Non-prop team-market floor (not env-overridable).
MIN_EDGE_PCT = 2.0

# Player-prop floor. Higher than team markets because Pinnacle's prop max-stake
# is ~$250 vs $7.5k+ for team totals/MLs; quotes are noisier. Env-overridable
# so PROP_MIN_EDGE=6 moves both the placement gate and dashboard display floor.
PROP_MIN_EDGE_PCT = float(os.getenv("PROP_MIN_EDGE", "4.0"))

# Per-(book, market_type) floor table. Floor rows: player_prop uses the
# env-backed PROP_MIN_EDGE_PCT; every other type uses 2.0.
# Scalars MIN_EDGE_PCT / PROP_MIN_EDGE_PCT act as fallbacks for unknown keys.
EDGE_FLOOR_PCT: dict[tuple[str, str], float] = {
    ("kalshi",     "moneyline"):   MIN_EDGE_PCT,
    ("kalshi",     "spread"):      MIN_EDGE_PCT,
    ("kalshi",     "total"):       MIN_EDGE_PCT,
    ("kalshi",     "team_total"):  MIN_EDGE_PCT,
    ("kalshi",     "player_prop"): PROP_MIN_EDGE_PCT,
    ("polymarket", "moneyline"):   MIN_EDGE_PCT,
    ("polymarket", "spread"):      MIN_EDGE_PCT,
    ("polymarket", "total"):       MIN_EDGE_PCT,
    ("polymarket", "team_total"):  MIN_EDGE_PCT,
    ("polymarket", "player_prop"): PROP_MIN_EDGE_PCT,
}

# Per-(book, market_type) margin table (percentage points added on top of the
# break-even fee term). Today every row is 1.0 pp (validated by
# analysis/threshold_sweep_kalshi.py); table structure exposes the per-type
# knob without behavior change.
EDGE_MARGIN_PP: dict[tuple[str, str], float] = {
    ("kalshi",     "moneyline"):   1.0,
    ("kalshi",     "spread"):      1.0,
    ("kalshi",     "total"):       1.0,
    ("kalshi",     "team_total"):  1.0,
    ("kalshi",     "player_prop"): 1.0,
    ("polymarket", "moneyline"):   1.0,
    ("polymarket", "spread"):      1.0,
    ("polymarket", "total"):       1.0,
    ("polymarket", "team_total"):  1.0,
    ("polymarket", "player_prop"): 1.0,
}

# Sanity ceilings on edge (env-overridable for backtesting).
SANITY_MAX_EDGE_PCT      = float(os.getenv("SANITY_MAX_EDGE",      "15.0"))
SANITY_MAX_EDGE_PCT_PROP = float(os.getenv("SANITY_MAX_EDGE_PROP", "25.0"))


def min_edge_pct(book: str, market_type: str, avg_fill_price) -> float:
    """Minimum edge_pct required to place a bet on this book at this fill price.
    Use when: gating placement or display in paper_tracker, real_tracker,
    ev_dashboard, or analysis/backtest_haircut.

    Fallback behavior mirrors old paper_tracker._min_edge_pct(book, px, is_prop):
    - Unknown book (rate=None) or None price → return floor only.
    - Unknown (book, market_type) key → floor from scalar defaults, margin 1.0.
    - Known book with any market_type → fee formula applies.
    """
    floor = EDGE_FLOOR_PCT.get(
        (book, market_type),
        PROP_MIN_EDGE_PCT if market_type == "player_prop" else MIN_EDGE_PCT,
    )
    rate = PER_BOOK_FEE_RATE.get(book)
    if rate is None or avg_fill_price is None:
        return floor
    margin = EDGE_MARGIN_PP.get((book, market_type), 1.0)
    be_plus_margin = 100.0 * rate * (1.0 - avg_fill_price) + margin
    return max(floor, be_plus_margin)


# ---------------------------------------------------------------------------
# 3. Bankroll / risk
# ---------------------------------------------------------------------------

PAPER_INITIAL_BANKROLL = 5000.0
KELLY_FRACTION = 0.25
PER_MATCH_STAKE_CAP_PCT = 0.03   # aggregate exposure cap per Pinnacle matchup
PER_MATCH_BET_CAP = 2            # max open bets sharing one Pinnacle matchup

REAL_INITIAL_BANKROLL = 1000.0
INITIAL_KALSHI_BALANCE = 500.0
INITIAL_POLYMARKET_BALANCE = 500.0

# Real-money hard caps (env-overridable for testing without restarting).
PER_BET_HARD_CAP_USD   = float(os.getenv("REAL_PER_BET_HARD_CAP_USD",  "30.0"))
DAILY_LOSS_HALT_USD    = float(os.getenv("REAL_DAILY_LOSS_HALT_USD",   "-100.0"))

# ---------------------------------------------------------------------------
# 4. Timing / pacing
# ---------------------------------------------------------------------------

# Shared poller cadence for the soft books (Kalshi, Polymarket). Pinnacle
# overrides this with its own faster interval (PINNACLE_POLL_INTERVAL_SEC).
POLLER_INTERVAL_SEC      = 60
POLLER_SNAPSHOT_RETENTION = 60   # keep the N most recent snapshots (≈ last hour)
POLLER_REQUEST_TIMEOUT   = 15

# Kalshi poller specifics.
KALSHI_WINDOW_HOURS          = 24
KALSHI_MAX_WORKERS           = 3      # gate bounds the rate; workers only tune I/O overlap
KALSHI_RATE_LIMIT_RETRIES    = 4
KALSHI_RATE_LIMIT_BACKOFF_SEC = 2.0
KALSHI_INTER_REQUEST_SLEEP   = 0.2   # global min-interval (~5 req/s baseline); tune against sidecar rate_limit_429
KALSHI_DEAD_SERIES_SKIP_AFTER  = 3
KALSHI_DEAD_SERIES_RETRY_AFTER = 5

# Pinnacle poller specifics.
PINNACLE_WINDOW_HOURS          = 3.25   # pregame upper bound (now + 3h15m); just above the 3h betting-window max
PINNACLE_LIVE_LOOKBACK_HOURS   = 0.5    # live rows for last 30m; covers the 15-min close-capture trail with margin
PINNACLE_INTER_REQUEST_SLEEP   = 0.2   # global min-interval (~10 req/s baseline); backoff on 429
PINNACLE_MAX_WORKERS           = 4      # gate bounds the rate regardless; this only tunes I/O overlap
PINNACLE_RATE_LIMIT_RETRIES    = 4
PINNACLE_RATE_LIMIT_BACKOFF_SEC = 2.0
# Pinnacle polls faster than the soft books: its snapshot is the only
# price-accuracy risk (soft ladders are pulled live). The interval is the
# freshness floor — keep it <= MAX_PIN_SNAPSHOT_AGE_SEC so a healthy snapshot
# never trips the gate. Watch rate_limit_429 in the sidecars; back off to
# 35-40 if it climbs.
PINNACLE_POLL_INTERVAL_SEC     = 30

# Polymarket poller specifics.
POLYMARKET_MAX_WORKERS         = 4
POLYMARKET_INTER_REQUEST_SLEEP = 0.2   # global min-interval (~5 req/s baseline); tune against sidecar rate_limit_429
POLYMARKET_RATE_LIMIT_RETRIES  = 4
POLYMARKET_RATE_LIMIT_BACKOFF_SEC = 2.0

# Adapter timeouts (read adapters are more lenient; trade adapters wait longer
# to distinguish ambiguous vs definitive placement failures).
ADAPTER_READ_TIMEOUT  = 10   # adapters/kalshi.py, adapters/polymarket.py
ADAPTER_TRADE_TIMEOUT = 15   # adapters/kalshi_trade.py, adapters/polymarket_trade.py

# Tracker timing.
SETTLEMENT_POLL_SEC     = 30 * 60   # shared by paper and real trackers
CLOSE_CAPTURE_POLL_SEC  = 30
CLOSE_CAPTURE_LEAD_SEC  = 60        # start capture this far before startTime
CLOSE_CAPTURE_TRAIL_SEC = 15 * 60   # stop capture this far after startTime
ORDER_POLL_SEC          = 5
BALANCE_LOG_POLL_SEC    = 5 * 60

# Dashboard.
DASHBOARD_REFRESH_SEC         = 60
LADDER_FETCH_TIMEOUT_SEC      = float(os.getenv("LADDER_FETCH_TIMEOUT_SEC", "5.0"))

# find_ev_bet / snapshot age. Two-tier by role: Pinnacle fair value is read
# straight from its snapshot (price-accuracy risk) so it is gated tight; soft
# books are re-fetched live at decision time so their snapshot age is only a
# coverage/liveness check and is gated loose. A blanket value would either
# false-reject the soft books (which are spaced 60-78s apart) or fail to
# tighten Pinnacle.
MAX_PIN_SNAPSHOT_AGE_SEC  = 45
MAX_SOFT_SNAPSHOT_AGE_SEC = 120
MIN_HOURS_TO_START   = 0.5
MAX_HOURS_TO_START   = 3.0

# ---------------------------------------------------------------------------
# 5. Feature flags
# ---------------------------------------------------------------------------

REAL_TRADING_ENABLED   = os.getenv("REAL_TRADING_ENABLED")   == "1"
PAPER_INCLUDE_PROPS    = os.getenv("PAPER_INCLUDE_PROPS")     == "1"
REAL_INCLUDE_PROPS     = os.getenv("REAL_INCLUDE_PROPS")      == "1"
KALSHI_INCLUDE_PROPS   = os.getenv("KALSHI_INCLUDE_PROPS")    == "1"
PINNACLE_INCLUDE_PROPS = os.getenv("PINNACLE_INCLUDE_PROPS")  == "1"
EV_DASHBOARD_HOST      = os.environ.get("EV_DASHBOARD_HOST", "127.0.0.1")

# ---------------------------------------------------------------------------
# 6. Paths
# ---------------------------------------------------------------------------

_DIR     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_DIR, "data")

# Pinnacle snapshots (read by find_ev_bet and paper_tracker's close-capture loop).
PIN_SNAPSHOT_DIR = os.path.join(DATA_DIR, "snapshots")

# Paper tracker state files.
PAPER_TRADES_PATH          = os.path.join(DATA_DIR, "paper_trades.jsonl")
PAPER_SETTLEMENTS_PATH     = os.path.join(DATA_DIR, "paper_settlements.jsonl")
PAPER_CLOSES_PATH          = os.path.join(DATA_DIR, "paper_closes.jsonl")
PAPER_SANITY_REJECTED_PATH = os.path.join(DATA_DIR, "sanity_rejected.jsonl")

# Real tracker state files.
REAL_TRADES_PATH           = os.path.join(DATA_DIR, "real_trades.jsonl")
REAL_FILLS_PATH            = os.path.join(DATA_DIR, "real_fills.jsonl")
REAL_SETTLEMENTS_PATH      = os.path.join(DATA_DIR, "real_settlements.jsonl")
REAL_CLOSES_PATH           = os.path.join(DATA_DIR, "real_closes.jsonl")
REAL_HALT_PATH             = os.path.join(DATA_DIR, "real_halt.json")
BALANCE_SNAPSHOT_PATH      = os.path.join(DATA_DIR, "balance_snapshots.jsonl")

# Poller snapshot dirs (log paths remain poller-local; they're not shared).
KALSHI_SNAPSHOT_DIR      = os.path.join(DATA_DIR, "kalshi_snapshots")
POLYMARKET_SNAPSHOT_DIR  = os.path.join(DATA_DIR, "polymarket_snapshots")
