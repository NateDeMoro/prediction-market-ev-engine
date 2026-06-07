#!/usr/bin/env python3
"""
Book-agnostic soft-market <-> Pinnacle matcher.

Consumes `list[NormalizedMarket]` from one or more soft books (Kalshi,
Polymarket US, ...) and pairs each market with its counterpart Pinnacle
2-way line. The moneyline stage fuzzy-matches team names and
disambiguates by startTime proximity; the non-mainline stage pairs
spread/total/team_total markets to the Pinnacle alternate line at the
matching (period, points, designation).

Book-specific parsing (title regexes, ticker suffix classifiers, YES-side
resolution) lives in `adapters/<book>.py`. This module only sees the
normalized row shape declared in `adapters/common.py`.
"""
from dataclasses import dataclass, field
from datetime import datetime

from adapters import adapter_for
from devig_utils import synthesize_combined_american
from adapters.common import (
    COMBO_STATS,
    NormalizedMarket,
    canonical_stat,
    fuzzy_match,
    league_team_allowlist,
    pin_sport_for_league,
    player_key,
    soccer_league_whitelisted,
    team_in_league,
)
from config import market_enabled

PIN_PERIOD_LABEL = {0: "FULL", 1: "1H", 2: "2H"}
PERIOD_LABEL_TO_INT = {v: k for k, v in PIN_PERIOD_LABEL.items()}


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Pinnacle selectors (unchanged from prior contract)
# ---------------------------------------------------------------------------

def load_pinnacle_moneylines(rows):
    return [
        r for r in rows
        if r.get("type") == "moneyline"
        and r.get("period") == 0
        and len(r.get("prices") or []) in (2, 3)
    ]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Match:
    """Moneyline pairing between one soft-book NormalizedMarket and a Pinnacle
    matchup. `pin_home_name` / `pin_away_name` are the raw Pinnacle matchup
    tokens in matchup-string order (not necessarily home/away — Pinnacle's
    `designation` field on each price row is authoritative for that)."""
    soft: NormalizedMarket
    pinnacle: dict
    pin_home_name: str
    pin_away_name: str
    delta_seconds: float


@dataclass
class UnmatchedSoft:
    soft: NormalizedMarket
    reason: str  # 'title_unparseable' | 'no_candidate' | 'time_out_of_tolerance'
    candidates: list = field(default_factory=list)


@dataclass
class MatchReport:
    matched: list
    unmatched_soft: list
    unmatched_pinnacle: list
    stats: dict

    def to_json(self):
        return {
            "stats": self.stats,
            "matched": [
                {
                    "book": m.soft.book,
                    "soft_market_id": m.soft.market_id,
                    "soft_event_id": m.soft.event_id,
                    "soft_title": m.soft.title,
                    "soft_yes_sub": m.soft.yes_sub_title,
                    "soft_start": m.soft.start_time,
                    "pin_sport": m.pinnacle.get("sport"),
                    "pin_matchup": m.pinnacle.get("matchup"),
                    "pin_start": m.pinnacle.get("startTime"),
                    "pin_is_live": m.pinnacle.get("isLive"),
                    "delta_seconds": m.delta_seconds,
                }
                for m in self.matched
            ],
            "unmatched_soft": [
                {
                    "book": u.soft.book,
                    "soft_market_id": u.soft.market_id,
                    "soft_title": u.soft.title,
                    "soft_start": u.soft.start_time,
                    "reason": u.reason,
                    "candidates": u.candidates,
                }
                for u in self.unmatched_soft
            ],
        }


@dataclass
class MatchedPair:
    """Unified view of a soft-book YES market paired with a Pinnacle 2-way book.

    For moneyline markets, `line is None`. For spreads/totals/team-totals,
    `line` carries the half-point handicap / over-under number. Side prices
    are already extracted so downstream devig sees a flat 2-way book.
    """
    market: NormalizedMarket       # soft-book normalized row
    pin_matchup_id: int
    pin_matchup: str
    pin_home_name: str
    pin_away_name: str
    pin_start: str
    pin_sport: str
    pin_is_live: bool
    market_type: str
    period_label: str
    line: float
    yes_side_label: str
    opposite_side_label: str
    yes_side_price: int
    opposite_side_price: int
    delta_seconds: float
    selection: str
    yes_designation: str
    opposite_designation: str
    pin_prop_matchup_id: int = None  # player_prop only; None for team markets


@dataclass
class AllMatchReport:
    pairs: list
    moneyline_report: MatchReport
    stats: dict

    def pairs_by_type(self):
        out = {}
        for p in self.pairs:
            out.setdefault(p.market_type, []).append(p)
        return out


# ---------------------------------------------------------------------------
# Moneyline matching
# ---------------------------------------------------------------------------

def _anchor_for(soft):
    """(datetime, tolerance_sec) for a NormalizedMarket; book-agnostic with an
    adapter-provided fallback when `start_time` is missing."""
    dt = parse_iso(soft.start_time)
    if dt is not None:
        return dt, 6 * 3600
    adapter = adapter_for(soft.book)
    fb = getattr(adapter, "fallback_anchor", None)
    if fb is None:
        return None, None
    iso_str, tol = fb(soft)
    dt = parse_iso(iso_str) if iso_str else None
    if dt is None:
        return None, None
    return dt, tol


_unknown_leagues_seen = set()
_not_whitelisted_leagues_seen = set()


def match_markets(pin_rows, soft_moneylines):
    """Pair soft-book moneyline NormalizedMarkets to Pinnacle moneylines."""
    pin_mls = load_pinnacle_moneylines(pin_rows)

    pin_indexed = []
    for pin in pin_mls:
        matchup = pin.get("matchup") or ""
        if " vs " not in matchup:
            continue
        p_a, p_b = [s.strip() for s in matchup.split(" vs ", 1)]
        pin_indexed.append((pin, p_a, p_b))

    matched = []
    unmatched_soft = []
    matched_pin_ids = set()
    stats = {
        "soft_moneyline_scope": len(soft_moneylines),
        "pin_moneyline_scope": len(pin_mls),
        "title_parse_fail": 0,
        "unknown_league": 0,
        "league_not_whitelisted": 0,
        "no_candidate": 0,
        "time_out_of_tolerance": 0,
        "name_postcheck_failed": 0,
        "matched": 0,
        "ambiguous_resolved": 0,
        "by_book": {},
    }

    def bump_book(book, bucket):
        d = stats["by_book"].setdefault(
            book,
            {"total": 0, "matched": 0, "title_parse_fail": 0,
             "unknown_league": 0, "league_not_whitelisted": 0,
             "no_candidate": 0, "time_out_of_tolerance": 0, "name_postcheck_failed": 0},
        )
        d[bucket] = d.get(bucket, 0) + 1

    for nm in soft_moneylines:
        bump_book(nm.book, "total")

        adapter = adapter_for(nm.book)
        parsed = adapter.parse_moneyline_teams(nm)
        if not parsed:
            stats["title_parse_fail"] += 1
            bump_book(nm.book, "title_parse_fail")
            unmatched_soft.append(UnmatchedSoft(soft=nm, reason="title_unparseable"))
            continue
        k_t1, k_t2 = parsed

        ks_anchor, tolerance_sec = _anchor_for(nm)
        # Sport gate from the soft-book's league hint. None means the league
        # is unregistered in adapters/common.SERIES_TICKER_LEAGUE_PREFIXES or
        # has no Pinnacle sport mapping. Fail closed: without a sport gate,
        # fuzzy_match cheerfully pairs "Minnesota" (Kalshi MLS) with
        # "Minnesota Wild" (Pinnacle NHL), which happened on 2026-04-22.
        expected_sport = pin_sport_for_league(nm.league)
        if expected_sport is None:
            league_key = (nm.book, nm.league)
            if league_key not in _unknown_leagues_seen:
                _unknown_leagues_seen.add(league_key)
                print(f"[matcher] dropping unknown league {nm.league!r} on {nm.book} "
                      f"— add to LEAGUE_TO_PIN_SPORT in adapters/common.py")
            stats["unknown_league"] += 1
            bump_book(nm.book, "unknown_league")
            unmatched_soft.append(UnmatchedSoft(soft=nm, reason="unknown_league"))
            continue
        # Soccer league whitelist: only allow leagues where Pinnacle lines are
        # genuinely sharp. Minor/low-liquidity leagues produce phantom edge
        # (Pinnacle's devigged "fair" price mean-reverts before kickoff).
        # Non-soccer sports are unaffected. Bypass with SOCCER_WHITELIST_DISABLE=1.
        if expected_sport == "Soccer" and not soccer_league_whitelisted(nm.league):
            league_key = (nm.book, nm.league)
            if league_key not in _not_whitelisted_leagues_seen:
                _not_whitelisted_leagues_seen.add(league_key)
                print(f"[matcher] dropping non-whitelisted soccer league {nm.league!r} on {nm.book} "
                      f"— add to SHARP_SOCCER_LEAGUES in adapters/common.py to enable")
            stats["league_not_whitelisted"] += 1
            bump_book(nm.book, "league_not_whitelisted")
            unmatched_soft.append(UnmatchedSoft(soft=nm, reason="league_not_whitelisted"))
            continue
        # Same-sport collision gate: require both Pinnacle participants to be
        # in the league's team allowlist (closes the NHL-vs-AHL and
        # MLS-vs-LigaMX holes that the sport filter alone misses). None = no
        # allowlist registered for this league -> skip this gate (looser but
        # sport-safe).
        allowlist = league_team_allowlist(nm.league)

        candidates = []
        for pin, p_a, p_b in pin_indexed:
            if pin.get("sport") != expected_sport:
                continue
            if allowlist is not None and not (
                team_in_league(p_a, allowlist) and team_in_league(p_b, allowlist)
            ):
                continue
            orient_1 = fuzzy_match(k_t1, p_a) and fuzzy_match(k_t2, p_b)
            orient_2 = fuzzy_match(k_t1, p_b) and fuzzy_match(k_t2, p_a)
            # Symmetric ambiguity (both orientations fit) means the soft-book
            # tokens are too generic to pick a side. Skip rather than guess.
            if orient_1 and orient_2:
                continue
            if orient_1 or orient_2:
                pin_start = parse_iso(pin.get("startTime"))
                if ks_anchor and pin_start:
                    delta = abs((ks_anchor - pin_start).total_seconds())
                else:
                    delta = 0.0
                candidates.append((delta, pin, p_a, p_b))

        if not candidates:
            stats["no_candidate"] += 1
            bump_book(nm.book, "no_candidate")
            unmatched_soft.append(UnmatchedSoft(soft=nm, reason="no_candidate"))
            continue

        candidates.sort(key=lambda x: x[0])
        best_delta, best_pin, p_a, p_b = candidates[0]

        if tolerance_sec is not None and best_delta > tolerance_sec:
            stats["time_out_of_tolerance"] += 1
            bump_book(nm.book, "time_out_of_tolerance")
            debug_cands = [
                (d, (p.get("matchup"), p.get("startTime"))) for d, p, _, _ in candidates[:3]
            ]
            unmatched_soft.append(UnmatchedSoft(
                soft=nm, reason="time_out_of_tolerance", candidates=debug_cands
            ))
            continue

        if len(candidates) > 1 and tolerance_sec is not None and candidates[1][0] <= tolerance_sec:
            stats["ambiguous_resolved"] += 1

        matched.append(Match(
            soft=nm, pinnacle=best_pin,
            pin_home_name=p_a, pin_away_name=p_b,
            delta_seconds=best_delta,
        ))
        stats["matched"] += 1
        bump_book(nm.book, "matched")
        if id(best_pin) not in matched_pin_ids:
            matched_pin_ids.add(id(best_pin))

    unmatched_pinnacle = [p for p, _, _ in pin_indexed if id(p) not in matched_pin_ids]

    return MatchReport(
        matched=matched,
        unmatched_soft=unmatched_soft,
        unmatched_pinnacle=unmatched_pinnacle,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# Non-mainline matching (spreads, totals, team totals)
# ---------------------------------------------------------------------------

def _fmt_line(line):
    if line is None:
        return ""
    return f"{line:g}"


def _resolve_team_side(ks_team, home_name, away_name):
    if fuzzy_match(ks_team, home_name):
        return "home"
    if fuzzy_match(ks_team, away_name):
        return "away"
    return None


def _find_spread_prices(pin_markets, yes_designation, line):
    opp = "away" if yes_designation == "home" else "home"
    target_yes_points = -line
    for r in pin_markets:
        prices = r.get("prices") or []
        yes_price = None
        opp_price = None
        for p in prices:
            d = p.get("designation")
            pts = p.get("points")
            if d == yes_designation and pts is not None and abs(pts - target_yes_points) < 1e-9:
                yes_price = p.get("price")
            elif d == opp:
                opp_price = p.get("price")
        if yes_price is not None and opp_price is not None:
            return r, yes_price, opp_price
    return None


def _find_total_prices(pin_markets, line):
    for r in pin_markets:
        prices = r.get("prices") or []
        over_price = None
        under_price = None
        for p in prices:
            pts = p.get("points")
            if pts is None or abs(pts - line) >= 1e-9:
                continue
            d = p.get("designation")
            if d == "over":
                over_price = p.get("price")
            elif d == "under":
                under_price = p.get("price")
        if over_price is not None and under_price is not None:
            return r, over_price, under_price
    return None


def load_pinnacle_props(rows):
    """Subset of a Pinnacle snapshot that are player-prop rows.

    Each row has type=='player_prop' with player, stat / stat_units, line,
    and a 2-price list (Over / Under keyed by participantId). Emitted by
    `pinnacle_poller.collect_prop_rows` when PINNACLE_INCLUDE_PROPS=1.
    """
    out = []
    for r in rows:
        if r.get("type") != "player_prop":
            continue
        if len(r.get("prices") or []) != 2:
            continue
        out.append(r)
    return out


def _pinnacle_prop_index(pin_prop_rows):
    """(matchup_id, canonical_stat, player_key) -> list of Pinnacle prop rows.

    Index can have multiple rows per key when Pinnacle publishes alt lines
    for the same player+stat (uncommon on props, but handled).
    """
    idx = {}
    for r in pin_prop_rows:
        stat = canonical_stat(r.get("stat_units")) or canonical_stat(r.get("stat"))
        if stat is None:
            continue
        pkey = player_key(r.get("player"))
        if not pkey:
            continue
        idx.setdefault((r.get("matchupId"), stat, pkey), []).append(r)
    return idx


def _pinnacle_prop_fuzzy_lookup(idx, matchup_id, stat, pkey):
    """Exact lookup first; fall back to fuzzy_match across same-(matchup, stat)
    entries when a literal key miss is likely a name-suffix mismatch (rare
    but Kalshi/Pinnacle disagree on suffixes for a handful of players)."""
    exact = idx.get((matchup_id, stat, pkey))
    if exact:
        return exact
    candidates = []
    for (mid, s, pk), rows in idx.items():
        if mid != matchup_id or s != stat:
            continue
        if fuzzy_match(pkey, pk) or fuzzy_match(pk, pkey):
            candidates.extend(rows)
    return candidates


def _find_prop_prices(pin_prop_rows, line):
    """Find the Pinnacle prop row whose line matches `line`. Return
    (over_price, under_price) or None. Pinnacle prop prices have
    participant order [Over, Under] per participants[0/1] in the related
    matchup — captured by `pinnacle_poller.collect_prop_rows` which preserves
    that order in the prices list.
    """
    for r in pin_prop_rows:
        if abs((r.get("line") or 0) - line) >= 1e-9:
            continue
        prices = r.get("prices") or []
        if len(prices) != 2:
            continue
        # prices[0] = Over (participants order 0); prices[1] = Under.
        over_price = prices[0].get("price")
        under_price = prices[1].get("price")
        if over_price is None or under_price is None:
            continue
        return r, over_price, under_price
    return None


def _find_team_total_prices(pin_markets, target_side, line):
    for r in pin_markets:
        if r.get("side") != target_side:
            continue
        prices = r.get("prices") or []
        over_price = None
        under_price = None
        for p in prices:
            pts = p.get("points")
            if pts is None or abs(pts - line) >= 1e-9:
                continue
            d = p.get("designation")
            if d == "over":
                over_price = p.get("price")
            elif d == "under":
                under_price = p.get("price")
        if over_price is not None and under_price is not None:
            return r, over_price, under_price
    return None


_DRAW_LABELS = {"tie", "tied", "draw", "drawn"}


def _ml_match_to_pair(m):
    """Convert a moneyline Match to a MatchedPair. Resolves YES -> home/away/draw.

    For 3-way (soccer), the opposite is synthesized from the other two
    Pinnacle legs' combined implied prob so MatchedPair stays flat and
    downstream 2-way devig gives the correct YES-leg fair prob.

    Returns None when YES cannot be resolved (`yes_side_unknown`).
    """
    pin = m.pinnacle
    prices = pin.get("prices") or []
    n_prices = len(prices)
    if n_prices not in (2, 3):
        return None

    by_des = {p.get("designation"): p.get("price") for p in prices
              if p.get("designation") in ("home", "away", "draw")}
    expected_des = {"home", "away", "draw"} if n_prices == 3 else {"home", "away"}
    if set(by_des.keys()) != expected_des:
        return None

    nm = m.soft
    yes_sub = (nm.yes_sub_title or nm.team or "").strip()
    p_home, p_away = m.pin_home_name, m.pin_away_name
    yes_designation = None
    yes_label = None
    if yes_sub:
        if n_prices == 3 and yes_sub.lower() in _DRAW_LABELS:
            yes_designation, yes_label = "draw", "Draw"
        else:
            home_match = (fuzzy_match(yes_sub, p_home)
                          or yes_sub.lower() == p_home.lower())
            away_match = (fuzzy_match(yes_sub, p_away)
                          or yes_sub.lower() == p_away.lower())
            if home_match and away_match:
                # Ambiguous: yes_sub matches both participants (e.g., the sub
                # was just "Dallas" on a hypothetical "Dallas X vs Dallas Y").
                # Drop rather than silently pick home.
                return None
            if home_match:
                yes_designation, yes_label = "home", p_home
            elif away_match:
                yes_designation, yes_label = "away", p_away
            if yes_designation is None:
                adapter = adapter_for(nm.book)
                parsed = adapter.parse_moneyline_teams(nm)
                if parsed:
                    k_t1, k_t2 = parsed
                    yes_title_team = None
                    if fuzzy_match(yes_sub, k_t1) or yes_sub.lower() == k_t1.lower():
                        yes_title_team = k_t1
                    elif fuzzy_match(yes_sub, k_t2) or yes_sub.lower() == k_t2.lower():
                        yes_title_team = k_t2
                    if yes_title_team:
                        for name, des in ((p_home, "home"), (p_away, "away")):
                            if fuzzy_match(yes_title_team, name):
                                yes_designation = des
                                yes_label = name
                                break
    if yes_designation is None:
        return None

    yes_price = by_des[yes_designation]
    if n_prices == 2:
        opposite_designation = "away" if yes_designation == "home" else "home"
        opposite_label = p_away if yes_designation == "home" else p_home
        opposite_price = by_des[opposite_designation]
    else:
        opposite_designation = f"not_{yes_designation}"
        opposite_label = f"Not {yes_label}"
        other_prices = [v for k, v in by_des.items() if k != yes_designation]
        opposite_price = synthesize_combined_american(other_prices)
        if opposite_price is None:
            return None

    return MatchedPair(
        market=nm,
        pin_matchup_id=pin.get("matchupId"),
        pin_matchup=pin.get("matchup") or f"{p_home} vs {p_away}",
        pin_home_name=p_home,
        pin_away_name=p_away,
        pin_start=pin.get("startTime"),
        pin_sport=pin.get("sport"),
        pin_is_live=bool(pin.get("isLive")),
        market_type="moneyline",
        period_label="FULL",
        line=None,
        yes_side_label=yes_label,
        opposite_side_label=opposite_label,
        yes_side_price=yes_price,
        opposite_side_price=opposite_price,
        delta_seconds=m.delta_seconds,
        selection=yes_label,
        yes_designation=yes_designation,
        opposite_designation=opposite_designation,
    )


def match_all_markets(pin_rows, soft_markets):
    """Run moneyline matcher, then resolve spread/total/team_total via each
    event's ML sibling. Returns AllMatchReport with a unified `pairs` list.
    `soft_markets` may contain rows from multiple books (Kalshi, Polymarket).
    """
    soft_mls = [nm for nm in soft_markets if nm.market_type == "moneyline"]
    soft_nonml = [nm for nm in soft_markets if nm.market_type != "moneyline"]

    ml_report = match_markets(pin_rows, soft_mls)

    # (book, event_group_key) -> Pinnacle matchup info, via the ML sibling.
    event_to_pin = {}
    for m in ml_report.matched:
        adapter = adapter_for(m.soft.book)
        sfx = adapter.event_group_key(m.soft)
        if not sfx:
            continue
        pin = m.pinnacle
        event_to_pin[(m.soft.book, sfx)] = {
            "matchup_id": pin.get("matchupId"),
            "matchup": pin.get("matchup"),
            "home": m.pin_home_name,
            "away": m.pin_away_name,
            "start": pin.get("startTime"),
            "sport": pin.get("sport"),
            "is_live": bool(pin.get("isLive")),
            "delta_seconds": m.delta_seconds,
        }

    # matchup_id -> period -> type -> [pin rows]
    pin_index = {}
    for r in pin_rows:
        mid = r.get("matchupId")
        period = r.get("period")
        t = r.get("type")
        pin_index.setdefault(mid, {}).setdefault(period, {}).setdefault(t, []).append(r)

    # Pinnacle player-prop index, built once.
    pin_prop_rows = load_pinnacle_props(pin_rows)
    prop_index = _pinnacle_prop_index(pin_prop_rows)

    stats = {
        "nonml_total": len(soft_nonml),
        "by_type": {"spread": 0, "total": 0, "team_total": 0, "player_prop": 0},
        "matched_by_type": {"spread": 0, "total": 0, "team_total": 0, "player_prop": 0},
        "no_event_match": 0,
        "unsupported_period": 0,
        "no_pin_market": 0,
        "no_line_match": 0,
        "side_unresolved": 0,
        "team_total_side_missing": 0,
        "market_disabled": 0,
        "moneyline_disabled": 0,
        "prop_pin_scope": len(pin_prop_rows),
        "prop_unmatched_reasons": {
            "combo_stat_no_pinnacle": 0,
            "no_team_anchor": 0,
            "no_player_match": 0,
            "no_line_match": 0,
            "missing_stat": 0,
        },
        "by_book": {},
    }

    def bump_book(book, bucket, n=1):
        d = stats["by_book"].setdefault(
            book,
            {"nonml_total": 0, "matched": 0,
             "matched_by_type": {"spread": 0, "total": 0, "team_total": 0}},
        )
        if bucket == "matched_by_type":
            return d["matched_by_type"]
        d[bucket] = d.get(bucket, 0) + n

    non_ml_pairs = []
    for nm in soft_nonml:
        stats["by_type"][nm.market_type] = stats["by_type"].get(nm.market_type, 0) + 1
        bump_book(nm.book, "nonml_total")

        adapter = adapter_for(nm.book)
        sfx = adapter.event_group_key(nm)
        pin_info = event_to_pin.get((nm.book, sfx))
        if not pin_info:
            stats["no_event_match"] += 1
            continue

        if not market_enabled(nm.book, pin_info["sport"], nm.league, nm.market_type):
            stats["market_disabled"] += 1
            continue

        # Player props route via a separate Pinnacle index keyed by
        # (matchup_id, canonical_stat, player_key). They do not obey the
        # period / pin_index lookup used by team markets, so handle here
        # before the generic block.
        if nm.market_type == "player_prop":
            if nm.stat in COMBO_STATS:
                stats["prop_unmatched_reasons"]["combo_stat_no_pinnacle"] += 1
                continue
            if nm.stat is None:
                stats["prop_unmatched_reasons"]["missing_stat"] += 1
                continue
            if not nm.team:
                stats["prop_unmatched_reasons"]["no_team_anchor"] += 1
                continue
            pkey = player_key(nm.player)
            if not pkey:
                stats["prop_unmatched_reasons"]["no_player_match"] += 1
                continue
            prop_rows = _pinnacle_prop_fuzzy_lookup(
                prop_index, pin_info["matchup_id"], nm.stat, pkey
            )
            if not prop_rows:
                stats["prop_unmatched_reasons"]["no_player_match"] += 1
                continue
            found = _find_prop_prices(prop_rows, nm.line)
            if not found:
                stats["prop_unmatched_reasons"]["no_line_match"] += 1
                stats["no_line_match"] += 1
                continue
            pin_prop_row, over_price, under_price = found
            pin_prop_matchup_id = pin_prop_row.get("prop_matchupId")
            # Kalshi YES = player reaches N+ = Pinnacle Over N-0.5.
            yes_price, opp_price = over_price, under_price
            yes_designation, opposite_designation = "over", "under"
            line_str = f"{nm.line:g}"
            yes_label = f"{nm.player} Over {line_str} {nm.stat}"
            opposite_label = f"{nm.player} Under {line_str} {nm.stat}"
            selection = yes_label

            non_ml_pairs.append(MatchedPair(
                market=nm,
                pin_matchup_id=pin_info["matchup_id"],
                pin_matchup=pin_info["matchup"] or f"{pin_info['home']} vs {pin_info['away']}",
                pin_home_name=pin_info["home"],
                pin_away_name=pin_info["away"],
                pin_start=pin_info["start"],
                pin_sport=pin_info["sport"],
                pin_is_live=pin_info["is_live"],
                market_type="player_prop",
                period_label="FULL",
                line=nm.line,
                yes_side_label=yes_label,
                opposite_side_label=opposite_label,
                yes_side_price=yes_price,
                opposite_side_price=opp_price,
                delta_seconds=pin_info["delta_seconds"],
                selection=selection,
                yes_designation=yes_designation,
                opposite_designation=opposite_designation,
                pin_prop_matchup_id=pin_prop_matchup_id,
            ))
            stats["matched_by_type"]["player_prop"] = stats["matched_by_type"].get("player_prop", 0) + 1
            per_book = stats["by_book"].setdefault(
                nm.book,
                {"nonml_total": 0, "matched": 0,
                 "matched_by_type": {"spread": 0, "total": 0, "team_total": 0, "player_prop": 0}},
            )
            per_book["matched"] += 1
            per_book["matched_by_type"]["player_prop"] = per_book["matched_by_type"].get("player_prop", 0) + 1
            continue

        pin_period = PERIOD_LABEL_TO_INT.get(nm.period_label)
        if pin_period is None:
            stats["unsupported_period"] += 1
            continue

        by_period = pin_index.get(pin_info["matchup_id"], {})
        pin_markets = by_period.get(pin_period, {}).get(nm.market_type, [])
        if not pin_markets:
            stats["no_pin_market"] += 1
            continue

        mtype = nm.market_type
        if mtype == "spread":
            yes_designation = _resolve_team_side(nm.team or "", pin_info["home"], pin_info["away"])
            if yes_designation is None:
                stats["side_unresolved"] += 1
                continue
            found = _find_spread_prices(pin_markets, yes_designation, nm.line)
            if not found:
                stats["no_line_match"] += 1
                continue
            _, yes_price, opp_price = found
            yes_label = f"{nm.team} -{_fmt_line(nm.line)}"
            opp_side = "away" if yes_designation == "home" else "home"
            opp_team = pin_info[opp_side]
            opposite_label = f"{opp_team} +{_fmt_line(nm.line)}"
            suffix_label = "" if nm.period_label == "FULL" else f" ({nm.period_label})"
            selection = f"{yes_label}{suffix_label}"
            opposite_designation = opp_side

        elif mtype == "total":
            found = _find_total_prices(pin_markets, nm.line)
            if not found:
                stats["no_line_match"] += 1
                continue
            _, over_price, under_price = found
            yes_label = f"Over {_fmt_line(nm.line)}"
            opposite_label = f"Under {_fmt_line(nm.line)}"
            yes_price, opp_price = over_price, under_price
            suffix_label = "" if nm.period_label == "FULL" else f" ({nm.period_label})"
            selection = f"{yes_label}{suffix_label}"
            yes_designation = "over"
            opposite_designation = "under"

        elif mtype == "team_total":
            target_side = _resolve_team_side(nm.team or "", pin_info["home"], pin_info["away"])
            if target_side is None:
                stats["side_unresolved"] += 1
                continue
            if not any(r.get("side") for r in pin_markets):
                stats["team_total_side_missing"] += 1
                continue
            found = _find_team_total_prices(pin_markets, target_side, nm.line)
            if not found:
                stats["no_line_match"] += 1
                continue
            _, over_price, under_price = found
            kalshi_side = nm.side or "over"
            if kalshi_side == "over":
                yes_price, opp_price = over_price, under_price
                yes_designation, opposite_designation = "over", "under"
            else:
                yes_price, opp_price = under_price, over_price
                yes_designation, opposite_designation = "under", "over"
            team_label = nm.team
            yes_label = f"{team_label} {kalshi_side.capitalize()} {_fmt_line(nm.line)}"
            opp_word = "Under" if kalshi_side == "over" else "Over"
            opposite_label = f"{team_label} {opp_word} {_fmt_line(nm.line)}"
            suffix_label = "" if nm.period_label == "FULL" else f" ({nm.period_label})"
            selection = f"{yes_label}{suffix_label}"

        else:
            continue

        non_ml_pairs.append(MatchedPair(
            market=nm,
            pin_matchup_id=pin_info["matchup_id"],
            pin_matchup=pin_info["matchup"] or f"{pin_info['home']} vs {pin_info['away']}",
            pin_home_name=pin_info["home"],
            pin_away_name=pin_info["away"],
            pin_start=pin_info["start"],
            pin_sport=pin_info["sport"],
            pin_is_live=pin_info["is_live"],
            market_type=mtype,
            period_label=nm.period_label,
            line=nm.line,
            yes_side_label=yes_label,
            opposite_side_label=opposite_label,
            yes_side_price=yes_price,
            opposite_side_price=opp_price,
            delta_seconds=pin_info["delta_seconds"],
            selection=selection,
            yes_designation=yes_designation,
            opposite_designation=opposite_designation,
        ))
        stats["matched_by_type"][mtype] = stats["matched_by_type"].get(mtype, 0) + 1
        per_book = stats["by_book"].setdefault(
            nm.book,
            {"nonml_total": 0, "matched": 0,
             "matched_by_type": {"spread": 0, "total": 0, "team_total": 0}},
        )
        per_book["matched"] += 1
        per_book["matched_by_type"][mtype] = per_book["matched_by_type"].get(mtype, 0) + 1

    ml_pairs = []
    ml_unresolved = 0
    ml_three_way = 0
    for m in ml_report.matched:
        if len(m.pinnacle.get("prices") or []) == 3:
            ml_three_way += 1
        pair = _ml_match_to_pair(m)
        if pair is None:
            ml_unresolved += 1
            continue
        if not market_enabled(pair.market.book, pair.pin_sport, pair.market.league, "moneyline"):
            stats["moneyline_disabled"] += 1
            continue
        ml_pairs.append(pair)
    stats["moneyline_three_way_matched"] = ml_three_way
    stats["moneyline_yes_side_unresolved"] = ml_unresolved
    stats["matched_by_type"]["moneyline"] = len(ml_pairs)

    return AllMatchReport(
        pairs=ml_pairs + non_ml_pairs,
        moneyline_report=ml_report,
        stats=stats,
    )
