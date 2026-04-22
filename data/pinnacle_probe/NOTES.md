# Pinnacle player-prop probe — findings

Probed 2026-04-22 against `https://guest.api.arcadia.pinnacle.com/0.1` with the existing `X-API-Key` from `pinnacle_poller.py:30`. Targets:
- `1628888090` Philadelphia Flyers vs Pittsburgh Penguins (NHL)
- `1628530428` Boston Red Sox vs New York Yankees (MLB)
- `1628535829` New York Mets vs Minnesota Twins (MLB)

## Endpoint result

The plan's speculative `/matchups/{id}/markets/special` endpoint **does not exist** (HTTP 400, `'special' is not one of ['straight', 'parlay']`). The same is true at the sport level (`/sports/{id}/markets/special`).

The actual prop ingestion path is **two endpoints**:

1. `GET /matchups/{parent_id}/related` — enumerates child matchups including each player prop. Returns list of matchup objects.
2. `GET /matchups/{parent_id}/markets/related/straight` — returns ALL price rows for the parent + every child matchup in one call. Filter by `matchupId == child_id` to pull a specific prop's prices.

This is **better than the plan assumed** — only 2 calls per parent matchup, and (2) is the same endpoint already used for live games at `pinnacle_poller.py:155`. The bulk pregame endpoint `/sports/{sport_id}/markets/straight?primaryOnly=false` returned 0 rows in this probe (parameter behavior may need follow-up), so today the per-matchup path is the only confirmed route.

## Prop child matchup shape

Each prop is a `type: "special"` matchup with `parentId` set to the team matchup. Sample (NHL):

```json
{
  "id": 1628955747,
  "type": "special",
  "parentId": 1628888090,
  "units": "Points",
  "special": {
    "category": "Player Props",
    "description": "Sidney Crosby (Points)"
  },
  "participants": [
    {"name": "Over",  "alignment": "neutral", "order": 0},
    {"name": "Under", "alignment": "neutral", "order": 1}
  ]
}
```

Key fields:
- `units` — Pinnacle's stat label. Observed values: `Points`, `Goals`, `Assists`, `ShotsOnGoal`, `Saves` (NHL); `Runs`, `TotalBases`, `Strikeouts`, `HomeRuns`, `HitsAllowed`, `EarnedRuns`, `PitchingOuts` (MLB).
- `special.category` — `"Player Props"` for player markets, but other categories appear too (`"Next Run"`, `"Highest Scoring Inning"`). **Filter on `category == "Player Props"`** to avoid pulling team/game props that have no Kalshi counterpart.
- `special.description` — `"<Player Name> (<Stat>)"`, sometimes with a suffix annotation like `"Dan Vladar (Saves)(must start)"` for goalies. Parse with `^(?P<player>.+?)\s*\((?P<stat>[^)]+)\)(?:\s*\(.*\))?$` — the second parenthesized group is metadata to ignore.

## Prop price row shape

Inside the bulk `/markets/related/straight` response, each prop has exactly one row keyed by its child matchupId. Sample:

```json
{
  "matchupId": 1628955747,
  "type": "total",
  "key": "s;0;ou",
  "period": 0,
  "prices": [
    {"participantId": 1628955748, "points": 0.5, "price": -259},
    {"participantId": 1628955749, "points": 0.5, "price": 187}
  ],
  "limits": [{"amount": 250, "type": "maxRiskStake"}]
}
```

- `type: "total"` and `key: "s;0;ou"` for every player prop, regardless of stat.
- `points` is the line. **No `designation: "over"/"under"`** field — instead the two prices have `participantId` matching the participants in the related-matchup record (order 0 = Over, order 1 = Under).
- `limits.amount` is the max stake in dollars. Observed: $250 for player props vs $7,500 for team totals vs $10,000 for moneyline. Confirms the "low-limit" claim in `player_probs_plan.md` and validates the higher edge gate (4-5% vs 2%).

## Implied vig

Crosby Points: -259 / +187 → decimal 1.386 / 2.870 → implied probs 0.722 + 0.348 = 1.070 → **~7% vig**. In line with the plan's "6-10%" estimate. 2-way multiplicative devig is appropriate.

## Cost estimate per cycle

Per game: 2 extra HTTP requests (`/related` + `/markets/related/straight`). At current pregame-window scale of ~30-50 NHL+MLB+NBA games, that's 60-100 extra calls per 60s cycle. With the existing `INTER_REQUEST_SLEEP = 0.2` (5 req/sec cap), this adds ~12-20 seconds per cycle. Within budget.

Note that `/markets/related/straight` already runs for live games. For pregame, the current poller uses bulk; switching to per-matchup adds the cost above but unlocks all alt-line and prop coverage. This is a tradeoff Phase 2 needs to make explicit.

## Verification status

Phase 1 verification criteria (from plan):

1. ✅ Kalshi NOTES.md proposes a concrete allowlist (regex-on-suffix turned out to be wrong; allowlist is the right answer).
2. ✅ This file answers: endpoint = `/matchups/{id}/related` + `/matchups/{id}/markets/related/straight`; fields = `units` + `special.description` + price-row `points`; cost = 2 extra calls per game.
3. ✅ Hand-matched: Kalshi `KXNHLPTS-26APR22PITPHI-PITSCROSBY87-1` ("Sidney Crosby: 1+ points") = Pinnacle matchupId 1628955747 with `units="Points"`, `description="Sidney Crosby (Points)"`, line 0.5. Same player, same game, equivalent threshold (Kalshi `N+` = Pinnacle Over `N−0.5`).

Phase 2 can proceed. Recommended adjustments to the design plan:
- Drop `/markets/special` from `pinnacle_poller.py` — endpoint doesn't exist.
- Use 2-call-per-matchup pattern, not the assumed single per-matchup fetch.
- Filter prop children on `special.category == "Player Props"` to exclude game-level specials.
- Player→team resolver from Pinnacle side becomes trivial: `parentId` directly references the team matchup, so no team lookup table needed (the plan's "small player→team lookup" can be skipped for Pinnacle; it may still be needed on the Kalshi side for sanity checks).
