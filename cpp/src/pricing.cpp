// Pricing core implementation. See pricing.hpp for the "use when" notes.
//
// Parity rules (keep when editing): sum() -> compensated_sum, round() ->
// round_half_even, ** -> std::pow (including square roots), same left-to-right
// operation order as the Python originals, and no FMA contraction (set in
// CMakeLists.txt).
#include "pmev/pricing.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace pmev {

// ---- Numeric helpers -------------------------------------------------------

double compensated_sum(const std::vector<double>& xs) {
    double result = 0.0;
    double c = 0.0;
    for (double x : xs) {
        double t = result + x;
        if (std::fabs(result) >= std::fabs(x)) {
            c += (result - t) + x;
        } else {
            c += (x - t) + result;
        }
        result = t;
    }
    if (c != 0.0 && std::isfinite(c)) {
        result += c;
    }
    return result;
}

double round_half_even(double x) { return std::nearbyint(x); }

namespace {

int64_t to_int64(double x) {
    if (!std::isfinite(x) || std::fabs(x) >= 9.2e18) {
        throw std::domain_error("cannot convert non-finite or huge value to integer");
    }
    return static_cast<int64_t>(x);
}

}  // namespace

// ---- Odds ------------------------------------------------------------------

std::optional<double> american_to_decimal(double a) {
    if (a == 0) return std::nullopt;
    if (0 < a && a < 100) return std::nullopt;
    if (a >= 100) return 1.0 + a / 100.0;
    return 1.0 + 100.0 / std::fabs(a);
}

int64_t price_to_american(double price) {
    if (price <= 0 || price >= 1) return 0;
    if (price <= 0.5) return to_int64(round_half_even(100 * (1 - price) / price));
    return to_int64(round_half_even(-100 * price / (1 - price)));
}

std::optional<int64_t> synthesize_combined_american(const std::vector<double>& american) {
    double combined_prob = 0.0;
    for (double a : american) {
        auto d = american_to_decimal(a);
        if (!d || *d <= 0) return std::nullopt;
        combined_prob += 1.0 / *d;
    }
    if (combined_prob <= 0 || combined_prob >= 1) return std::nullopt;
    double combined_decimal = 1.0 / combined_prob;
    if (combined_decimal >= 2.0) {
        return to_int64(round_half_even((combined_decimal - 1.0) * 100.0));
    }
    return to_int64(round_half_even(-100.0 / (combined_decimal - 1.0)));
}

// ---- Margin removal --------------------------------------------------------

namespace {

std::optional<std::vector<double>> raw_probs(const std::vector<double>& american) {
    std::vector<double> probs;
    probs.reserve(american.size());
    for (double p : american) {
        auto d = american_to_decimal(p);
        if (!d || *d <= 0) return std::nullopt;
        probs.push_back(1.0 / *d);
    }
    return probs;
}

std::vector<double> normalize(const std::vector<double>& xs, double total) {
    std::vector<double> out;
    out.reserve(xs.size());
    for (double x : xs) out.push_back(x / total);
    return out;
}

double sum_pow(const std::vector<double>& probs, double k) {
    std::vector<double> terms;
    terms.reserve(probs.size());
    for (double p : probs) terms.push_back(std::pow(p, k));
    return compensated_sum(terms);
}

// Python's `x ** 0.5` calls libm pow(), which can differ from a correctly rounded
// sqrt by one ulp. A literal 0.5 lets the optimizer rewrite pow(x, 0.5) as fsqrt,
// so the exponent is read through a volatile to keep the real pow() call.
volatile double g_shin_exponent = 0.5;

std::vector<double> shin_fairs(const std::vector<double>& probs, double booksum, double z) {
    double denom = 2.0 * (1.0 - z);
    std::vector<double> out;
    out.reserve(probs.size());
    for (double pi : probs) {
        double root = std::pow(z * z + 4.0 * (1.0 - z) * pi * pi / booksum, g_shin_exponent);
        out.push_back((root - z) / denom);
    }
    return out;
}

}  // namespace

std::optional<std::vector<double>> implied_probs(const std::vector<double>& american) {
    auto probs = raw_probs(american);
    if (!probs) return std::nullopt;
    if (probs->empty() || compensated_sum(*probs) <= 0) return std::nullopt;
    return probs;
}

std::optional<std::vector<double>> devig_multiplicative(const std::vector<double>& american) {
    auto probs = raw_probs(american);
    if (!probs) return std::nullopt;
    double total = compensated_sum(*probs);
    if (total <= 0) return std::nullopt;
    return normalize(*probs, total);
}

std::optional<std::vector<double>> devig_power(const std::vector<double>& american,
                                               double tol, int iters) {
    auto probs = implied_probs(american);
    if (!probs) return std::nullopt;
    double lo = 0.0, hi = 1.0;
    while (sum_pow(*probs, hi) > 1.0) {
        hi *= 2.0;
        if (hi > 1e6) return std::nullopt;
    }
    for (int i = 0; i < iters; ++i) {
        double mid = 0.5 * (lo + hi);
        double s = sum_pow(*probs, mid);
        if (std::fabs(s - 1.0) < tol) break;
        if (s > 1.0) {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    double k = 0.5 * (lo + hi);
    std::vector<double> fair;
    fair.reserve(probs->size());
    for (double p : *probs) fair.push_back(std::pow(p, k));
    double total = compensated_sum(fair);
    if (total <= 0) return std::nullopt;
    return normalize(fair, total);
}

std::optional<std::vector<double>> devig_shin(const std::vector<double>& american,
                                              double tol, int iters) {
    auto probs = implied_probs(american);
    if (!probs) return std::nullopt;
    double booksum = compensated_sum(*probs);
    double lo = 0.0, hi = 1.0 - 1e-9;
    if (compensated_sum(shin_fairs(*probs, booksum, lo)) <= 1.0) {
        return devig_multiplicative(american);
    }
    for (int i = 0; i < iters; ++i) {
        double mid = 0.5 * (lo + hi);
        double s = compensated_sum(shin_fairs(*probs, booksum, mid));
        if (std::fabs(s - 1.0) < tol) break;
        if (s > 1.0) {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    auto fair = shin_fairs(*probs, booksum, 0.5 * (lo + hi));
    double total = compensated_sum(fair);
    if (total <= 0) return std::nullopt;
    return normalize(fair, total);
}

// ---- Calibration, fees, thresholds -----------------------------------------

double haircut_fair(double p, double k) { return p - k * p * (1.0 - p); }

double quadratic_fee(double rate, double price) { return rate * price * (1.0 - price); }

double min_edge_floor(double floor, std::optional<double> rate,
                      std::optional<double> avg_fill, double margin) {
    if (!rate || !avg_fill) return floor;
    double be_plus_margin = 100.0 * *rate * (1.0 - *avg_fill) + margin;
    return be_plus_margin > floor ? be_plus_margin : floor;
}

// ---- Ladder ----------------------------------------------------------------

namespace {

template <class Fee>
WalkResult walk_impl(const std::vector<LadderLevel>& ladder, double fair, Fee&& fee) {
    WalkResult r;
    for (const auto& lv : ladder) {
        double gross = 1.0 - lv.price;
        double ev = fair * gross - (1 - fair) * lv.price - fee(lv.price, fair);
        if (ev <= 0) break;
        r.levels.push_back({lv.price, lv.qty, ev});
        r.shares += lv.qty;
        r.stake += static_cast<double>(lv.qty) * lv.price;
        r.ev += static_cast<double>(lv.qty) * ev;
    }
    return r;
}

template <class Fee>
double breakeven_impl(double price, Fee&& fee) {
    double lo = 0.0, hi = 1.0;
    for (int i = 0; i < 40; ++i) {
        double mid = (lo + hi) / 2;
        double ev = mid * (1 - price) - (1 - mid) * price - fee(price, mid);
        if (ev > 0) {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    return (lo + hi) / 2;
}

template <class Fee>
EvalResult evaluate_impl(const std::vector<LadderLevel>& ladder, double fair, Fee&& fee,
                         const GateInputs& g) {
    EvalResult out;
    if (ladder.empty()) return out;
    WalkResult w = walk_impl(ladder, fair, fee);
    if (w.shares == 0) return out;

    out.shares = w.shares;
    out.stake = w.stake;
    out.exp_profit = w.ev;
    out.levels = std::move(w.levels);
    out.ev_pct = out.stake > 0 ? out.exp_profit / out.stake * 100 : 0.0;
    out.avg_fill = out.stake / static_cast<double>(out.shares);

    double floor = min_edge_floor(g.floor, g.book_fee_rate, out.avg_fill, g.margin);
    if (out.ev_pct < floor) {
        out.verdict = Verdict::BelowFloor;
    } else if (out.ev_pct > g.ceiling) {
        out.verdict = Verdict::OverCeiling;
    } else if (out.ev_pct >= g.ceiling_log_frac * g.ceiling) {
        out.verdict = Verdict::AcceptNearCeiling;
    } else {
        out.verdict = Verdict::Accept;
    }
    return out;
}

}  // namespace

WalkResult walk_ladder(const std::vector<LadderLevel>& ladder, double fair, double fee_rate) {
    return walk_impl(ladder, fair,
                     [fee_rate](double p, double) { return quadratic_fee(fee_rate, p); });
}

WalkResult walk_ladder(const std::vector<LadderLevel>& ladder, double fair, const FeeFn& fee_fn) {
    return walk_impl(ladder, fair, fee_fn);
}

double breakeven_fair(double price, double fee_rate) {
    return breakeven_impl(price,
                          [fee_rate](double p, double) { return quadratic_fee(fee_rate, p); });
}

double breakeven_fair(double price, const FeeFn& fee_fn) { return breakeven_impl(price, fee_fn); }

// ---- Gate ------------------------------------------------------------------

EvalResult evaluate_core(const std::vector<LadderLevel>& ladder, double fair,
                         const FeeFn& fee_fn, const GateInputs& gate) {
    return evaluate_impl(ladder, fair, fee_fn, gate);
}

EvalResult evaluate_core(const std::vector<LadderLevel>& ladder, double fair,
                         double fee_rate, const GateInputs& gate) {
    return evaluate_impl(
        ladder, fair, [fee_rate](double p, double) { return quadratic_fee(fee_rate, p); }, gate);
}

// ---- Kelly -----------------------------------------------------------------

std::optional<KellyResult> kelly_size(double fair, double vwap, double fee_on_win,
                                      double fee_on_stake, double bankroll,
                                      double kelly_fraction,
                                      std::optional<double> max_stake,
                                      int64_t pos_shares) {
    if (vwap <= 0 || vwap >= 1) return std::nullopt;
    double net_win = (1 - vwap) - fee_on_win;
    double eff_stake = vwap + fee_on_stake;
    if (net_win <= 0 || eff_stake <= 0) return std::nullopt;

    double b = net_win / eff_stake;
    double f_full = fair - (1 - fair) / b;
    if (f_full <= 0) return std::nullopt;

    double budget = bankroll * f_full * kelly_fraction;
    if (max_stake && *max_stake < budget) budget = *max_stake;
    if (budget <= 0) return std::nullopt;

    double target = std::floor(budget / eff_stake);
    int64_t shares;
    if (target >= static_cast<double>(pos_shares)) {
        shares = pos_shares;
    } else {
        shares = to_int64(target);
    }
    if (shares <= 0) return std::nullopt;
    return KellyResult{f_full, shares};
}

}  // namespace pmev
