# Kalshi player-prop probe — findings

Probed 2026-04-22. Source: `/series?category=Sports` + `/markets?series_ticker=...` (no auth).

## Catalog shape

`/series?category=Sports` returns ~250 series. Of those, ~210 do NOT match the existing `CORE_SUFFIX = (GAME|SPREAD|TOTAL|ML|H2H)$`. That is far too broad to treat as "props" — most are season-long markets (awards, draft picks, division winners, exact-wins-by-team) that have no per-game Pinnacle counterpart.

The plan's intuition that there's a single `PROP_SUFFIX` regex is wrong. Series fall into three buckets:

| Bucket | Examples | Has Pinnacle counterpart? |
| --- | --- | --- |
| Per-game player props | `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBABLK`, `KXNBASTL`, `KXNBA3PT`, `KXNHLPTS`, `KXNHLGOALS`, `KXNHLSOG`, `KXNFLPASSYDS`, `KXNFLRSHYDS`, `KXNFLRECYDS`, `KXNFLREC`, `KXNFLPASSTDS`, `KXNFLANYTD`, `KXNFLNEXTTD` | Yes — these are the target |
| Season-long awards/standings | `KXNBAMVP`, `KXNBADPOY`, `KXNBAATLANTIC`, `KXNFLWINS-DAL`, `KXNFLEXACTWINSKC`, `KXNBAFINMVP` | No — exclude |
| Event/structural | `KXNBADRAFT*`, `KXNBALOTTERYODDS`, `KXNFLCOMBINE40`, `KXCOACHOUTNBA` | No — exclude |

**Recommendation for Phase 2**: don't generalize a `PROP_SUFFIX` regex. Maintain an explicit allowlist of per-game prop series tickers in `kalshi_poller.py`. Allowlists are cheaper to audit (~20 entries) than a regex that needs to deny ~190 false positives. Seed list (NBA + NHL active right now; NFL/MLB to validate when in season):

```python
PER_GAME_PROP_SERIES = {
    # NBA
    "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBABLK", "KXNBASTL", "KXNBA3PT",
    "KXNBAPRA", "KXNBAPR", "KXNBARA", "KXNBAPA",
    # NHL
    "KXNHLPTS", "KXNHLGOALS", "KXNHLSOG",
    # NFL (verify in-season)
    "KXNFLPASSYDS", "KXNFLPASSTDS", "KXNFLRSHYDS", "KXNFLRECYDS",
    "KXNFLREC", "KXNFLANYTD", "KXNFLNEXTTD",
    # MLB — needs follow-up probe in season; KXMLB* per-game stat series not visible today
}
```

NFL series exist in the catalog but are 0-open right now (offseason). MLB per-stat series weren't found at all — the MLB prop catalog may be exposed under different tickers or may not be live yet on Kalshi. Re-probe when season starts.

## Per-game prop ticker format

Highly structured. Pattern observed across NBA, NHL, NFL series:

```
KX<SPORT><STAT>-<YYMMDD><AWAY><HOME>-<TEAM><PLAYER_LAST><JERSEY>-<LINE>
                ^^^^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^   ^^^^^
                event_ticker          per-player segment           threshold
```

Examples:
- `KXNBAPTS-26APR24LALHOU-HOURSHEPPARD15-20` → Reed Sheppard (Houston, jersey 15), 20+ points, 4/24/26 LAL@HOU
- `KXNHLPTS-26APR22PITPHI-PITSCROSBY87-1` → Sidney Crosby (Pittsburgh, jersey 87), 1+ points, 4/22/26 PIT@PHI
- `KXNBASTL-26APR24LALHOU-LALMSMART36-3` → Marcus Smart (Lakers, jersey 36), 3+ steals

Implications for Phase 3:
- `event_ticker` (`KX<SPORT><STAT>-<DATE><AWAY><HOME>`) is the natural `event_group_key` for binding to Pinnacle's parent team matchup. Date + team-abbrev pair is unambiguous.
- `title` field is the cleanest source for `(player, stat, line)` parsing: `"Sidney Crosby: 3+ points"` → `^(?P<player>[^:]+):\s*(?P<line>\d+(?:\.\d+)?)\+\s*(?P<stat>\w+)$`. Use this over ticker-substring parsing — player names aren't easily recoverable from `PITSCROSBY87`.

## Threshold semantics — important

Kalshi prop YES = "player gets **≥ N** [stat]" (e.g., "20+ points" = 20 or more). To match Pinnacle's over/under at half-points, **subtract 0.5**:

| Kalshi market | Equivalent Pinnacle Over |
| --- | --- |
| `Crosby: 1+ points` | Over 0.5 points |
| `Sheppard: 20+ points` | Over 19.5 points |
| `Eason: 10+ rebounds` | Over 9.5 rebounds |

Verified against Pinnacle: Crosby Points prop on matchupId 1628955747 has `points: 0.5` in the price row, matching `KXNHLPTS-26APR22PITPHI-PITSCROSBY87-1`. Hand-match successful (verification criterion #3 from plan).

This `−0.5` translation lives in `adapters/kalshi.py` once Phase 3 starts; do not push it into `market_matcher.py` because it's Kalshi-specific.

## Stat taxonomy gaps

Kalshi stat suffixes seen: `PTS`, `REB`, `AST`, `BLK`, `STL`, `3PT`, `PRA`, `PR`, `RA`, `PA` (NBA combo lines), `PASSYDS`, `PASSTDS`, `RSHYDS`, `RECYDS`, `REC`, `ANYTD`, `NEXTTD` (NFL), `PTS`, `GOALS`, `SOG` (NHL).

Pinnacle units field uses different vocabulary (see pinnacle_probe NOTES). Need a `KALSHI_STAT_TO_CANONICAL` map in `adapters/common.py` and a parallel map in `pinnacle_poller.py` parsing path so both sides land on the same `stat_type` constants.

## Volume estimate

Per-game prop counts (from sampled markets):
- NBA: ~6 stat series × ~10 players × ~5 thresholds = ~300 markets per game (but realistically only top ~10 players quoted at multiple thresholds, so ~150)
- NHL: ~3 series × similar = ~100 per game
- NFL: ~7 series × ~150 markets per game (largest catalog)

At full season, this is 5-10× the current core-market row count from Kalshi. Will need to monitor `kalshi_poller.py` request volume — current `MAX_WORKERS=3` may need to bump if rolling out NBA + NFL simultaneously.
