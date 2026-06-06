"""
Market-level on/off config for the EV scanner.

Two-tier lookup:
  1. LEAGUE_OVERRIDES — keyed (book, league, market_type) → bool.
     An entry here overrides the sport-level default for that specific league.
     Use when: re-enabling a single league from a disabled sport (e.g. re-enable
     NBA moneylines when Basketball is off), or blocking a single league when its
     sport is on.
  2. SPORT_TOGGLES — keyed (book, sport, market_type) → bool.
     The hand-editable defaults. All combos in the cross product are listed so a
     human can scan and flip one line. Unlisted combos fall through to fail-closed.

Resolution order: league override → sport default → fail-closed (False + one-time log).

Backtest bypass: set MARKET_TOGGLES_DISABLE=1 to return True for every combo
(mirrors SOCCER_WHITELIST_DISABLE in adapters/common.py).

WARNING: adding a new sport to LEAGUE_TO_PIN_SPORT in adapters/common.py without
adding matching SPORT_TOGGLES entries will raise RuntimeError at startup (the
startup assertion enforces full coverage). Add the new sport rows to SPORT_TOGGLES
before deploying.
"""
import os
from typing import Optional

from adapters.common import LEAGUE_TO_PIN_SPORT

# ---------------------------------------------------------------------------
# League-level overrides (most specific).
# Key: (book, league_code_uppercase, market_type)
# Example: ("kalshi", "NBA", "moneyline"): True  — re-enable NBA MLs while
#          Basketball sport default is False.
# ---------------------------------------------------------------------------
LEAGUE_OVERRIDES: dict[tuple[str, str, str], bool] = {
    # none yet — add per-league carve-outs here
}

# ---------------------------------------------------------------------------
# Sport-level defaults.
# Key: (book, sport_title_case, market_type)
# Group by book for readability. All True except the three disabled combos.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Startup assertion — runs at import.
# Raises RuntimeError if SPORT_TOGGLES is missing any (book, sport, market_type)
# for a sport known to the league registry. Also validates LEAGUE_OVERRIDES league
# keys against the registry.
# ---------------------------------------------------------------------------
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
            "[market_config] SPORT_TOGGLES is missing entries — add them before deploying:\n"
            + "\n".join(f"  {b!r}, {s!r}, {m!r}" for b, s, m in sorted(missing))
        )

    unknown_leagues = [
        league for (_, league, _) in LEAGUE_OVERRIDES
        if league not in LEAGUE_TO_PIN_SPORT
    ]
    if unknown_leagues:
        raise RuntimeError(
            "[market_config] LEAGUE_OVERRIDES contains league codes not in LEAGUE_TO_PIN_SPORT: "
            + ", ".join(sorted(set(unknown_leagues)))
        )

_assert_coverage()

# ---------------------------------------------------------------------------
# Lookup helper — the only public API callers should use.
# ---------------------------------------------------------------------------
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
            f"[market_config] blocking unconfigured market "
            f"book={book!r} sport={sport!r} league={league!r} type={market_type!r} "
            f"— add to SPORT_TOGGLES or LEAGUE_OVERRIDES in market_config.py"
        )
    return False
