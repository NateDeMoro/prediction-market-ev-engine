# 1000BetsTracked

Local snapshot of the **paper-trade** tracked-bet history, pulled from the VPS
(`arb@100.94.115.11:~/Arbitrage_Betting/data/`) so the history can be referenced
without SSH. Paper (simulated) bets, not real-money fills.

- **Snapshot pulled:** 2026-06-12
- **Placement span:** 2026-04-25 → 2026-06-13
- **Sports:** Baseball 577, Soccer 307, Basketball 137, Hockey 94, Cricket 1

To refresh: re-`scp` the three files from the VPS path above. This is a static
snapshot — it does not update as new bets settle.

## Files (full bet lifecycle, JSONL — one record per line)

| File | Records | One record = | Key fields |
|------|---------|--------------|------------|
| `paper_trades.jsonl` | 1122 | a placement | `placed_at`, `book`, `market_id`, `side`, `selection`, `fair_prob`, `avg_fill_price`, `shares`, `stake`, `edge_pct`, `kelly_fraction_applied`, `bankroll_at_placement`, `levels` |
| `paper_closes.jsonl` | 1113 | a closing-line capture | `captured_at`, `market_id`, `side`, `fair_prob_close`, `yes_side_price_close`, `minutes_before_start` |
| `paper_settlements.jsonl` | 1116 | a settled bet (outcome) | `settled_at`, `market_id`, `side`, `result`, `net_pnl`, `gross_return`, `fair_prob`, `fair_prob_close`, `clv`, `stake`, `pin_sport`, `market_type` |

`paper_settlements.jsonl` is the richest single view: it already joins placement
`fair_prob`, closing `fair_prob_close`, `clv`, and outcome (`result`, `net_pnl`),
so most analysis (P&L, CLV, drift) reads it alone.

## Join keys

Records link on `(market_id, side, selection)`. Soccer league is **not** stored as
a field — derive it from the Kalshi `market_id` series prefix via
`adapters.common.series_ticker_league(market_id.split("-")[0])`.

## Derived metrics

- **Payoff:** win → `shares - stake`; loss → `-stake` (exact for Kalshi & Polymarket).
- **CLV** = `fair_prob_close - avg_fill_price` (positive = beat the closing price).
- **Fair-line drift** = `fair_prob_close - fair_prob` (negative = phantom edge that
  reverted by close). Identity: `CLV = edge + drift`, `edge = fair_prob - avg_fill_price`.
