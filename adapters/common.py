"""
Shared soft-book adapter primitives.

`NormalizedMarket` is the book-agnostic row the matcher and EV pipeline
consume. Each adapter is responsible for turning its raw snapshot rows
into NormalizedMarket instances; the matcher only sees this shape.

Also hosts team-name fuzzy utilities used across adapters and the
matcher.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import re


@dataclass
class NormalizedMarket:
    book: str                         # "kalshi" | "polymarket"
    market_id: str                    # Kalshi ticker | Polymarket slug
    event_id: str                     # shared key within a single game across sibling markets
    title: str
    yes_sub_title: Optional[str]
    market_type: str                  # "moneyline" | "spread" | "total" | "team_total" | "player_prop"
    period_label: str                 # "FULL" | "1H" | "2H"
    line: Optional[float]             # None for moneyline
    team: Optional[str]               # YES-side team for spread/team_total; team abbrev for player_prop; None otherwise
    side: Optional[str]               # "over" | "under" for team_total / player_prop; None otherwise
    start_time: str                   # ISO-8601
    raw: dict                         # pass-through for diagnostics
    player: Optional[str] = None      # player_prop: full name as printed by the book
    stat: Optional[str] = None        # player_prop: canonical stat key (see CANONICAL_STAT)


# Canonical stat keys for player props. Both the Kalshi series suffix and the
# Pinnacle `stat_units` / `stat` field map into this vocabulary. Combo stats
# are recorded even though Pinnacle usually doesn't publish a direct counterpart
# — the matcher emits `combo_stat_no_pinnacle` for them rather than silently
# dropping.
CANONICAL_STAT = {
    # --- NBA ---
    # Kalshi suffixes
    "PTS": "points",
    "REB": "rebounds",
    "AST": "assists",
    "BLK": "blocks",
    "STL": "steals",
    "3PT": "threes_made",
    "PRA": "pts_reb_ast",
    "PR":  "pts_reb",
    "RA":  "reb_ast",
    "PA":  "pts_ast",
    # Pinnacle units / stat strings
    "Points": "points",
    "Rebounds": "rebounds",
    "Assists": "assists",
    "Blocks": "blocks",
    "Steals": "steals",
    "ThreePointersMade": "threes_made",
    "Three Pointers Made": "threes_made",
    # --- NHL ---
    # Kalshi suffixes
    "GOALS": "nhl_goals",
    "SOG":   "shots_on_goal",
    # Pinnacle units / stat strings
    "Goals": "nhl_goals",
    "ShotsOnGoal": "shots_on_goal",
    "Shots On Goal": "shots_on_goal",
    # NHL uses "PTS" (= goals + assists) on Kalshi; Pinnacle units = "Points"
    # but in the NHL league context. The hockey context is disambiguated by
    # the parent game, so this collides intentionally with NBA "Points":
    # matcher scopes the stat lookup to a single Pinnacle matchupId.
}

COMBO_STATS = {"pts_reb_ast", "pts_reb", "reb_ast", "pts_ast"}


def canonical_stat(raw):
    """Map a Kalshi suffix or Pinnacle units string to a canonical stat key.

    Returns None if unknown. Case-sensitive for Pinnacle CamelCase; upper()
    fallback catches Kalshi suffixes written in any case.
    """
    if not raw:
        return None
    if raw in CANONICAL_STAT:
        return CANONICAL_STAT[raw]
    u = raw.upper()
    return CANONICAL_STAT.get(u)


# Kalshi event tickers embed a 3-letter team abbreviation pair, e.g.
# "26APR22ORLDET" -> away=ORL, home=DET. We need these to resolve a Kalshi
# prop to its Pinnacle parent game, because the prop ticker's per-player
# segment uses the same 3-letter code and Pinnacle only exposes full team
# names. Nested per-league because several abbreviations overlap (BOS, CHI,
# DAL, DET, MIN, PHI, TOR, UTA all appear in both NBA and NHL).
TEAM_ABBREV_BY_LEAGUE = {
    "NBA": {
        "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
        "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
        "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
        "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
        "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
        "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
        "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
        "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
        "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
        "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
    },
    "NHL": {
        "ANA": "Anaheim Ducks", "BOS": "Boston Bruins", "BUF": "Buffalo Sabres",
        "CGY": "Calgary Flames", "CAR": "Carolina Hurricanes", "CHI": "Chicago Blackhawks",
        "COL": "Colorado Avalanche", "CBJ": "Columbus Blue Jackets", "DAL": "Dallas Stars",
        "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers", "FLA": "Florida Panthers",
        "LAK": "Los Angeles Kings", "MIN": "Minnesota Wild", "MTL": "Montreal Canadiens",
        "NSH": "Nashville Predators", "NJD": "New Jersey Devils", "NYI": "New York Islanders",
        "NYR": "New York Rangers", "OTT": "Ottawa Senators", "PHI": "Philadelphia Flyers",
        "PIT": "Pittsburgh Penguins", "SJS": "San Jose Sharks", "SEA": "Seattle Kraken",
        "STL": "St. Louis Blues", "TBL": "Tampa Bay Lightning", "TOR": "Toronto Maple Leafs",
        "UTA": "Utah Mammoth", "VAN": "Vancouver Canucks", "VGK": "Vegas Golden Knights",
        "WSH": "Washington Capitals", "WPG": "Winnipeg Jets",
    },
}


def resolve_team_abbrev(abbrev, league):
    """3-letter code -> Pinnacle team name. Caller must supply league context
    ("NBA" | "NHL") because abbreviations collide across sports."""
    if not abbrev or not league:
        return None
    return TEAM_ABBREV_BY_LEAGUE.get(league, {}).get(abbrev)


# Kalshi series-ticker prefix -> league. Matches the per-game-prop allowlist
# in kalshi_poller.PER_GAME_PROP_SERIES.
SERIES_TICKER_LEAGUE_PREFIXES = (
    ("KXNBA", "NBA"),
    ("KXNHL", "NHL"),
    ("KXNFL", "NFL"),
    ("KXMLB", "MLB"),
)


def series_ticker_league(series_ticker):
    """League inferred from a Kalshi series ticker prefix. Returns None if
    unknown (caller should skip — matcher scope is NBA + NHL for now)."""
    if not series_ticker:
        return None
    for prefix, league in SERIES_TICKER_LEAGUE_PREFIXES:
        if series_ticker.startswith(prefix):
            return league
    return None


# Strip common player-name suffixes when building a fuzzy key. Kalshi
# renders "Jr." / "III" inline (`Wendell Carter Jr.`) while Pinnacle
# sometimes omits them (`Wendell Carter`). Stripping both sides stabilizes
# the fuzzy_match comparison.
_PLAYER_SUFFIX_RE = re.compile(
    r"\s+(?:jr\.?|sr\.?|ii|iii|iv)$", re.I,
)


def player_key(name):
    """Normalized lowercase player name used as a fuzzy-match input."""
    if not name:
        return ""
    s = name.strip()
    # strip trailing suffixes like "Jr." / "III"
    while True:
        stripped = _PLAYER_SUFFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    return s.lower()


def tokenize(name):
    s = (name or "").lower()
    s = re.sub(r"'s\b", "", s)
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


def fuzzy_match(short_name, long_name):
    """True if every token of short_name is a prefix of some unused token in long_name."""
    s = tokenize(short_name)
    l = tokenize(long_name)
    if not s or not l:
        return False
    used = set()
    for st in s:
        hit = False
        for i, lt in enumerate(l):
            if i in used:
                continue
            if lt.startswith(st):
                used.add(i)
                hit = True
                break
        if not hit:
            return False
    return True
