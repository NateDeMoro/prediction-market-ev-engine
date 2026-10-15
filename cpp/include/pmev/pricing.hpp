// Pricing core for the prediction-market EV engine.
//
// Open when: changing odds conversion, devigging, fee models, the ladder walk,
// the edge gate, or Kelly sizing. Every function mirrors the Python it replaced
// operation for operation (compensated sum, half-to-even rounding, pow for all
// powers, no FMA contraction) so results are bit-identical on Python >= 3.12.
#pragma once

#include <cstdint>
#include <functional>
#include <optional>
#include <tuple>
#include <utility>
#include <vector>

namespace pmev {

// ---- Numeric helpers -------------------------------------------------------

// Use when: summing doubles anywhere Python called sum(). Matches CPython's
// compensated (Neumaier) float sum, so ten 0.1s give exactly 1.0.
double compensated_sum(const std::vector<double>& xs);

// Use when: Python called round(x). Rounds halves to even (nearbyint under the
// default rounding mode), unlike std::round.
double round_half_even(double x);

// ---- Odds ------------------------------------------------------------------

// Use when: converting American odds to decimal. Nothing for a == 0 or
// 0 < a < 100.
std::optional<double> american_to_decimal(double a);

// Use when: showing a YES-share price in dollars as American odds. Returns 0
// when the price is outside (0, 1).
int64_t price_to_american(double price);

// Use when: collapsing several American legs into one with implied prob equal
// to the sum of the legs. Nothing on a bad leg or combined prob >= 1.
std::optional<int64_t> synthesize_combined_american(const std::vector<double>& american);

// ---- Margin removal --------------------------------------------------------

// Use when: turning American prices into raw implied probs (1/decimal).
// Nothing on a bad leg, an empty list, or a non-positive sum.
std::optional<std::vector<double>> implied_probs(const std::vector<double>& american);

// Use when: removing vig proportionally. Nothing on degenerate input.
std::optional<std::vector<double>> devig_multiplicative(const std::vector<double>& american);

// Use when: removing vig with the power method (more vig off longshots).
std::optional<std::vector<double>> devig_power(const std::vector<double>& american,
                                               double tol, int iters);

// Use when: removing vig with Shin's insider-fraction model. Falls back to
// multiplicative on a no-vig book.
std::optional<std::vector<double>> devig_shin(const std::vector<double>& american,
                                              double tol, int iters);

// ---- Calibration, fees, thresholds -----------------------------------------

// Use when: shrinking a devigged fair toward less confidence: p - k*p*(1-p).
double haircut_fair(double p, double k);

// Use when: computing a quadratic taker fee per share: rate * p * (1 - p).
double quadratic_fee(double rate, double price);

// Use when: computing the minimum edge pct. Larger of the floor and
// 100*rate*(1-fill) + margin; the floor alone when rate or fill is missing.
double min_edge_floor(double floor, std::optional<double> rate,
                      std::optional<double> avg_fill, double margin);

// ---- Ladder ----------------------------------------------------------------

struct LadderLevel {
    double price;
    int64_t qty;
};

struct WalkLevel {
    double price;
    int64_t qty;
    double ev;
};

struct WalkResult {
    int64_t shares = 0;
    double stake = 0.0;
    double ev = 0.0;
    std::vector<WalkLevel> levels;
};

using FeeFn = std::function<double(double price, double fair)>;

// Use when: consuming ladder levels while EV/share > 0 under a quadratic fee
// with a known rate (no per-level callback).
WalkResult walk_ladder(const std::vector<LadderLevel>& ladder, double fair, double fee_rate);

// Use when: the fee model is an arbitrary callable (tests, fake adapters).
WalkResult walk_ladder(const std::vector<LadderLevel>& ladder, double fair, const FeeFn& fee_fn);

// Use when: solving the fair prob where EV == 0 at a price (40 bisection steps),
// native quadratic-fee path.
double breakeven_fair(double price, double fee_rate);

// Use when: same as above for an arbitrary fee callable.
double breakeven_fair(double price, const FeeFn& fee_fn);

// ---- Gate ------------------------------------------------------------------

enum class Verdict { NoLevels, BelowFloor, OverCeiling, AcceptNearCeiling, Accept };

struct GateInputs {
    double floor;                   // edge floor pct for (book, market type)
    std::optional<double> book_fee_rate;  // nullopt for an unknown book
    double margin;                  // margin pp added to the break-even fee term
    double ceiling;                 // sanity ceiling pct
    double ceiling_log_frac;        // fraction of ceiling that triggers a near-ceiling flag
};

struct EvalResult {
    Verdict verdict = Verdict::NoLevels;
    int64_t shares = 0;
    double stake = 0.0;
    double exp_profit = 0.0;
    double ev_pct = 0.0;
    double avg_fill = 0.0;
    std::vector<WalkLevel> levels;
};

// Use when: walking a ladder and applying the floor and ceiling gate in one
// call. The floor is computed from avg_fill, not the best ask.
EvalResult evaluate_core(const std::vector<LadderLevel>& ladder, double fair,
                         const FeeFn& fee_fn, const GateInputs& gate);

// Use when: same as above with a quadratic fee of known rate.
EvalResult evaluate_core(const std::vector<LadderLevel>& ladder, double fair,
                         double fee_rate, const GateInputs& gate);

// ---- Kelly -----------------------------------------------------------------

struct KellyResult {
    double f_full;
    int64_t target_shares;
};

// Use when: sizing a bet. Nothing on any of size_bet's early-return conditions
// from the vwap range check onward. Target shares are capped at pos_shares.
std::optional<KellyResult> kelly_size(double fair, double vwap, double fee_on_win,
                                      double fee_on_stake, double bankroll,
                                      double kelly_fraction,
                                      std::optional<double> max_stake,
                                      int64_t pos_shares);

}  // namespace pmev
