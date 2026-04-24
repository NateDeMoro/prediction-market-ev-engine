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
import unicodedata


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
    league: Optional[str] = None      # "NBA" | "NHL" | "MLB" | "NFL" | "WNBA" | "NCAAF" | "NCAAB" | None
                                      # Used by matcher to constrain candidate Pinnacle rows to the
                                      # correct sport — prevents Kalshi MLB tickers fuzzy-matching
                                      # AHL hockey games that share city names.


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
    "Threes Made": "threes_made",
    "Pts & Rebs & Asts": "pts_reb_ast",
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
        # Kalshi uses 2-letter codes for a handful of NHL clubs; the third char
        # of the ticker chunk is actually the player's first-initial. The prop
        # adapter falls back to a 2-char lookup when the 3-char lookup misses.
        "TB": "Tampa Bay Lightning",
        "NJ": "New Jersey Devils",
        "SJ": "San Jose Sharks",
        "LA": "Los Angeles Kings",
    },
}


def resolve_team_abbrev(abbrev, league):
    """3-letter code -> Pinnacle team name. Caller must supply league context
    ("NBA" | "NHL") because abbreviations collide across sports."""
    if not abbrev or not league:
        return None
    return TEAM_ABBREV_BY_LEAGUE.get(league, {}).get(abbrev)


# Kalshi series-ticker prefix -> league. Every prefix a soft-book poller
# publishes must be registered here: the matcher now fails closed on
# unknown leagues (see market_matcher._unknown_league_skip). Order matters
# only for collisions, which none of the current prefixes share (KXMLS is
# distinct from KXMLB under startswith).
SERIES_TICKER_LEAGUE_PREFIXES = (
    # US majors
    ("KXNBA", "NBA"),
    ("KXNHL", "NHL"),
    ("KXNFL", "NFL"),
    ("KXMLB", "MLB"),
    # Soccer — Pinnacle groups all of these under sport="Soccer".
    ("KXMLS", "MLS"),
    ("KXLIGAMX", "LIGAMX"),
    ("KXLALIGA", "LALIGA"),
    ("KXALLSVENSKAN", "ALLSVENSKAN"),
    ("KXCOPADOBRASIL", "COPADOBRASIL"),
    ("KXUAEPL", "UAEPL"),
    ("KXDIMAYOR", "DIMAYOR"),
    ("KXISL", "ISL"),
    ("KXEREDIVISIE", "EREDIVISIE"),
    ("KXSAUDIPL", "SAUDIPL"),
    ("KXDFBPOKAL", "DFBPOKAL"),
    ("KXECULP", "ECULP"),
    ("KXBELGIANPL", "BELGIANPL"),
    ("KXLIGAPORTUGAL", "LIGAPORTUGAL"),
    ("KXUSL", "USL"),
    ("KXARGPREMDIV", "ARGPREMDIV"),
    ("KXBOLPDIV", "BOLPDIV"),
    ("KXTACAPORT", "TACAPORT"),
    ("KXPERLIGA1", "PERLIGA1"),
    ("KXEGYPL", "EGYPL"),
    ("KXDENSUPERLIGA", "DENSUPERLIGA"),
    ("KXEPL", "EPL"),
    ("KXBALLERLEAGUE", "BALLERLEAGUE"),
    # European top flights (added 2026-04-24). Registered without team
    # allowlists — falls back to sport-only gating, consistent with the rest
    # of the soccer block. `KXBUNDESLIGA` startswith also covers
    # `KXBUNDESLIGA2` (2. Bundesliga); `KXSERIEA` and `KXSERIEB` are kept
    # distinct (Serie A is Italian top flight; Serie B is Italian 2nd tier).
    ("KXBUNDESLIGA", "BUNDESLIGA"),
    ("KXLIGUE1", "LIGUE1"),
    ("KXSERIEA", "SERIEA"),
    ("KXSERIEB", "SERIEB"),
    ("KXSCOTTISHPREM", "SCOTTISHPREM"),
    ("KXSWISSLEAGUE", "SWISSLEAGUE"),
    # Non-soccer internationals. Each has a distinct Pinnacle sport bucket
    # with no same-sport collisions against a registered league's allowlist
    # (MLB's allowlist excludes NPB/KBO clubs, so falling through to
    # no-allowlist Baseball matching on NPB/KBO tickers is safe). `KXPGA`
    # covers both `KXPGAH2H` and any future PGA suffix; `KXIPL` covers
    # `KXIPLGAME` + `KXIPLTEAMTOTAL`.
    ("KXNPB", "NPB"),
    ("KXKBO", "KBO"),
    ("KXPGA", "PGA"),
    ("KXAFL", "AFL"),
    ("KXIPL", "IPL"),
)


def series_ticker_league(series_ticker):
    """League inferred from a Kalshi series ticker prefix. Returns None if
    unknown. The matcher treats None as fail-closed (no match attempted)."""
    if not series_ticker:
        return None
    for prefix, league in SERIES_TICKER_LEAGUE_PREFIXES:
        if series_ticker.startswith(prefix):
            return league
    return None


# League hint -> Pinnacle `sport` field. Pinnacle exposes only the broad sport
# name (Title Case English), not a league. Filtering candidate Pinnacle rows by
# this map prevents the Apr-2026 Eagles incident: Kalshi MLB ticker for
# "San Diego at Colorado" fuzzy-matched AHL "Colorado Eagles vs San Diego Gulls"
# because (a) fuzzy_match accepts city-only prefixes and (b) the matcher
# previously iterated all Pinnacle moneylines regardless of sport.
LEAGUE_TO_PIN_SPORT = {
    "NBA":   "Basketball",
    "WNBA":  "Basketball",
    "NCAAB": "Basketball",
    "NHL":   "Hockey",
    "MLB":   "Baseball",
    "NFL":   "Football",
    "NCAAF": "Football",
    # Soccer leagues — Pinnacle exposes all as sport="Soccer". Within-sport
    # cross-league collisions (e.g. Liga MX "Dallas" ~= MLS "FC Dallas") are
    # addressed by the per-league team allowlist below.
    "MLS":            "Soccer",
    "LIGAMX":         "Soccer",
    "LALIGA":         "Soccer",
    "ALLSVENSKAN":    "Soccer",
    "COPADOBRASIL":   "Soccer",
    "UAEPL":          "Soccer",
    "DIMAYOR":        "Soccer",
    "ISL":            "Soccer",
    "EREDIVISIE":     "Soccer",
    "SAUDIPL":        "Soccer",
    "DFBPOKAL":       "Soccer",
    "ECULP":          "Soccer",
    "BELGIANPL":      "Soccer",
    "LIGAPORTUGAL":   "Soccer",
    "USL":            "Soccer",
    "ARGPREMDIV":     "Soccer",
    "BOLPDIV":        "Soccer",
    "TACAPORT":       "Soccer",
    "PERLIGA1":       "Soccer",
    "EGYPL":          "Soccer",
    "DENSUPERLIGA":   "Soccer",
    "EPL":            "Soccer",
    "BALLERLEAGUE":   "Soccer",
    "BUNDESLIGA":     "Soccer",
    "LIGUE1":         "Soccer",
    "SERIEA":         "Soccer",
    "SERIEB":         "Soccer",
    "SCOTTISHPREM":   "Soccer",
    "SWISSLEAGUE":    "Soccer",
    # Non-soccer internationals. No within-sport collisions against a
    # registered allowlisted league: MLB's allowlist excludes Japanese/Korean
    # clubs, and Golf / Australian Rules Football / Cricket are each their
    # own Pinnacle sport bucket.
    "NPB": "Baseball",
    "KBO": "Baseball",
    "PGA": "Golf",
    "AFL": "Australian Rules Football",
    "IPL": "Cricket",
}


def pin_sport_for_league(league):
    """Pinnacle `sport` value expected for a league hint, or None if unknown.
    None disables sport filtering for that row (preserves prior behavior)."""
    if not league:
        return None
    return LEAGUE_TO_PIN_SPORT.get(league.upper())


# Per-league full-team-name allowlists. The sport filter alone (NBA -> Basketball)
# does not stop a Kalshi NHL ticker for "Colorado" from matching Pinnacle's AHL
# "Colorado Eagles" -- both are sport=Hockey. Requiring both Pinnacle participants
# to be in the league's allowlist closes that hole. Maintained by hand for the
# four majors + WNBA; NCAA is intentionally absent (~130 FBS / ~350 D1 schools is
# unmaintainable, and Polymarket's event_id binding mostly handles it).
_NBA_TEAMS = frozenset(TEAM_ABBREV_BY_LEAGUE["NBA"].values())
_NHL_TEAMS = frozenset(TEAM_ABBREV_BY_LEAGUE["NHL"].values())
_MLB_TEAMS = frozenset({
    "Arizona Diamondbacks", "Atlanta Braves", "Baltimore Orioles", "Boston Red Sox",
    "Chicago Cubs", "Chicago White Sox", "Cincinnati Reds", "Cleveland Guardians",
    "Colorado Rockies", "Detroit Tigers", "Houston Astros", "Kansas City Royals",
    "Los Angeles Angels", "Los Angeles Dodgers", "Miami Marlins", "Milwaukee Brewers",
    "Minnesota Twins", "New York Mets", "New York Yankees",
    # Athletics relocation in flux (Oakland -> Sacramento -> Las Vegas). Pinnacle
    # may publish either form; keep both.
    "Oakland Athletics", "Athletics",
    "Philadelphia Phillies", "Pittsburgh Pirates", "San Diego Padres",
    "San Francisco Giants", "Seattle Mariners", "St. Louis Cardinals",
    "Tampa Bay Rays", "Texas Rangers", "Toronto Blue Jays", "Washington Nationals",
})
_NFL_TEAMS = frozenset({
    "Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills",
    "Carolina Panthers", "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns",
    "Dallas Cowboys", "Denver Broncos", "Detroit Lions", "Green Bay Packers",
    "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars", "Kansas City Chiefs",
    "Las Vegas Raiders", "Los Angeles Chargers", "Los Angeles Rams", "Miami Dolphins",
    "Minnesota Vikings", "New England Patriots", "New Orleans Saints", "New York Giants",
    "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers",
    "San Francisco 49ers", "Seattle Seahawks", "Tampa Bay Buccaneers",
    "Tennessee Titans", "Washington Commanders",
})
_WNBA_TEAMS = frozenset({
    "Atlanta Dream", "Chicago Sky", "Connecticut Sun", "Dallas Wings",
    "Golden State Valkyries", "Indiana Fever", "Las Vegas Aces",
    "Los Angeles Sparks", "Minnesota Lynx", "New York Liberty",
    "Phoenix Mercury", "Seattle Storm", "Washington Mystics",
})
# Soccer allowlists. Pinnacle's sport gate alone treats MLS/Liga MX/La Liga
# as one bucket (sport="Soccer"), so "Dallas" in a Kalshi MLS ticker can
# still fuzzy-match Liga MX "Dallas"-something. Allowlists per active soccer
# league re-tighten the gate. Maintained by hand; only includes leagues we've
# actually seen produce matches. Unregistered soccer leagues fall back to
# sport-only gating (looser but still cross-sport-safe).
_MLS_TEAMS = frozenset({
    "Atlanta United", "Austin FC", "Charlotte FC", "Chicago Fire",
    "Colorado Rapids", "Columbus Crew", "DC United", "FC Cincinnati",
    "FC Dallas", "Houston Dynamo", "Inter Miami", "LA Galaxy",
    "Los Angeles FC", "Minnesota United", "Montreal Impact", "CF Montreal",
    "Nashville SC", "New England Revolution", "New York City FC",
    "New York Red Bulls", "Orlando City", "Philadelphia Union",
    "Portland Timbers", "Real Salt Lake", "San Jose Earthquakes",
    "San Diego FC", "Seattle Sounders", "Sporting Kansas City",
    "St. Louis City", "Toronto FC", "Vancouver Whitecaps",
})
_LIGAMX_TEAMS = frozenset({
    "America", "Club America", "Atlas", "Atletico San Luis", "Cruz Azul",
    "Guadalajara", "Chivas Guadalajara", "Juarez", "FC Juarez",
    "Leon", "Club Leon", "Mazatlan", "Monterrey", "Necaxa",
    "Pachuca", "Puebla", "Pumas UNAM", "Pumas", "Queretaro",
    "Santos Laguna", "Tigres UANL", "Tigres", "Tijuana", "Club Tijuana",
    "Toluca", "Deportivo Toluca", "UNAM", "UANL",
})

LEAGUE_TEAM_ALLOWLIST = {
    "NBA":    _NBA_TEAMS,
    "WNBA":   _WNBA_TEAMS,
    "NHL":    _NHL_TEAMS,
    "MLB":    _MLB_TEAMS,
    "NFL":    _NFL_TEAMS,
    "MLS":    _MLS_TEAMS,
    "LIGAMX": _LIGAMX_TEAMS,
}


def league_team_allowlist(league):
    """Frozenset of full team names known to belong to this league, or None
    when no allowlist exists (caller skips the team-allowlist filter)."""
    if not league:
        return None
    return LEAGUE_TEAM_ALLOWLIST.get(league.upper())


def team_in_league(team, allowlist):
    """True if `team` matches any allowlist member via fuzzy_match in either
    direction. Bidirectional handles Pinnacle's longer forms ("Oakland Athletics"
    vs allowlist "Athletics") and soft-book shortenings ("Athletics" vs
    allowlist "Oakland Athletics") symmetrically."""
    if not team or not allowlist:
        return False
    for known in allowlist:
        if fuzzy_match(team, known) or fuzzy_match(known, team):
            return True
    return False


# Strip common player-name suffixes when building a fuzzy key. Kalshi
# renders "Jr." / "III" inline (`Wendell Carter Jr.`) while Pinnacle
# sometimes omits them (`Wendell Carter`). Stripping both sides stabilizes
# the fuzzy_match comparison.
_PLAYER_SUFFIX_RE = re.compile(
    r"\s+(?:jr\.?|sr\.?|ii|iii|iv)$", re.I,
)


def player_key(name):
    """Normalized lowercase player name used as a fuzzy-match input.

    NFKD-decomposes and strips combining marks so Kalshi's "Nikola Vučević"
    collapses to the same key as Pinnacle's "Nikola Vucevic".
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name.strip())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
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
