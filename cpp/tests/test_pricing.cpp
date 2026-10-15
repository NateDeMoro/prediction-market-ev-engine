// GoogleTest suite for the pricing core. Open when: changing cpp/src/pricing.cpp.
#include <gtest/gtest.h>

#include <cmath>
#include <numeric>

#include "pmev/pricing.hpp"

using namespace pmev;

namespace {
double sum(const std::vector<double>& v) { return std::accumulate(v.begin(), v.end(), 0.0); }
const double kZeroFee = 0.0;
}  // namespace

TEST(AmericanToDecimal, Values) {
    EXPECT_DOUBLE_EQ(*american_to_decimal(100), 2.0);
    EXPECT_DOUBLE_EQ(*american_to_decimal(150), 2.5);
    EXPECT_DOUBLE_EQ(*american_to_decimal(-110), 1.0 + 100.0 / 110.0);
    EXPECT_DOUBLE_EQ(*american_to_decimal(-100), 2.0);
    EXPECT_FALSE(american_to_decimal(0));
    EXPECT_FALSE(american_to_decimal(50));
    EXPECT_FALSE(american_to_decimal(99.99));
}

TEST(PriceToAmerican, Values) {
    EXPECT_EQ(price_to_american(0.5), 100);
    EXPECT_EQ(price_to_american(0.4), 150);
    EXPECT_EQ(price_to_american(0.6), -150);
    EXPECT_EQ(price_to_american(0), 0);
    EXPECT_EQ(price_to_american(1), 0);
    EXPECT_EQ(price_to_american(-0.2), 0);
    // 0.32 -> 212.5 exactly under double arithmetic: half rounds to even (212).
    EXPECT_EQ(price_to_american(0.32), 212);
}

TEST(CompensatedSum, TenTenths) {
    EXPECT_EQ(compensated_sum(std::vector<double>(10, 0.1)), 1.0);
    EXPECT_NE(sum(std::vector<double>(10, 0.1)), 1.0);  // naive loop is off by an ulp
}

TEST(RoundHalfEven, Halves) {
    EXPECT_EQ(round_half_even(0.5), 0.0);
    EXPECT_EQ(round_half_even(1.5), 2.0);
    EXPECT_EQ(round_half_even(2.5), 2.0);
    EXPECT_EQ(round_half_even(-2.5), -2.0);
}

TEST(ImpliedProbs, Overround) {
    auto p = implied_probs({-110, -110});
    ASSERT_TRUE(p);
    EXPECT_GT(sum(*p), 1.0);
    EXPECT_FALSE(implied_probs({-110, 0}));
    EXPECT_FALSE(implied_probs({}));
}

TEST(DevigMultiplicative, SumsToOne) {
    auto f = devig_multiplicative({-110, -110});
    ASSERT_TRUE(f);
    EXPECT_NEAR(sum(*f), 1.0, 1e-12);
    EXPECT_NEAR((*f)[0], 0.5, 1e-12);
    auto g = devig_multiplicative({-200, 170});
    double a = 1 / *american_to_decimal(-200), b = 1 / *american_to_decimal(170);
    EXPECT_DOUBLE_EQ((*g)[0], a / (a + b));
    EXPECT_FALSE(devig_multiplicative({-110, 50}));
    EXPECT_FALSE(devig_multiplicative({}));
}

TEST(DevigPower, RemovesMoreFromLongshot) {
    auto m = devig_multiplicative({-300, 250});
    auto p = devig_power({-300, 250}, 1e-12, 200);
    ASSERT_TRUE(p);
    EXPECT_NEAR(sum(*p), 1.0, 1e-12);
    EXPECT_LT((*p)[1], (*m)[1]);
    EXPECT_FALSE(devig_power({0, 100}, 1e-12, 200));
}

TEST(DevigShin, ShrinksLongshotMoreThanMultiplicative) {
    // On a 2-way book Shin sits between multiplicative and power (it approximates additive).
    auto m = devig_multiplicative({-300, 250});
    auto p = devig_power({-300, 250}, 1e-12, 200);
    auto s = devig_shin({-300, 250}, 1e-12, 200);
    ASSERT_TRUE(s);
    EXPECT_NEAR(sum(*s), 1.0, 1e-12);
    EXPECT_LT((*s)[1], (*m)[1]);
    EXPECT_GT((*s)[1], (*p)[1]);
    // No-vig book: equals multiplicative.
    auto nv = devig_shin({100, 100}, 1e-12, 200);
    auto mu = devig_multiplicative({100, 100});
    ASSERT_TRUE(nv);
    EXPECT_EQ(*nv, *mu);
}

TEST(SynthesizeCombined, Cases) {
    // Draw + away of a 3-way market collapses to a single price.
    auto r = synthesize_combined_american({250, 300});
    ASSERT_TRUE(r);
    double prob = 1 / *american_to_decimal(250) + 1 / *american_to_decimal(300);
    double dec = 1 / prob;
    EXPECT_EQ(*r, static_cast<int64_t>(std::nearbyint(dec >= 2.0 ? (dec - 1) * 100 : -100 / (dec - 1))));
    EXPECT_FALSE(synthesize_combined_american({-200, -200}));  // prob >= 1
    EXPECT_FALSE(synthesize_combined_american({250, 50}));
    // Both sides of decimal 2.0.
    EXPECT_GT(*synthesize_combined_american({300, 300}), 0);   // prob 0.5 -> +100
    EXPECT_LT(*synthesize_combined_american({150, 200}), 0);
    // Exact half: prob 0.5 + ... -> even rounding. 1/dec = 0.4 + 0.1 gives +100.
    EXPECT_EQ(*synthesize_combined_american({150, 900}), 100);
}

TEST(Haircut, Shape) {
    EXPECT_EQ(haircut_fair(0.37, 0.0), 0.37);
    EXPECT_DOUBLE_EQ(haircut_fair(0.5, 0.064), 0.5 - 0.064 / 4);
    EXPECT_EQ(haircut_fair(0.0, 0.064), 0.0);
    EXPECT_EQ(haircut_fair(1.0, 0.064), 1.0);
}

TEST(QuadraticFee, Formula) {
    EXPECT_EQ(quadratic_fee(0.07, 0.4), 0.07 * 0.4 * (1.0 - 0.4));
    EXPECT_EQ(quadratic_fee(0.03, 0.5), 0.03 * 0.5 * 0.5);
}

TEST(FeeFloor, Selection) {
    // price 0.5, rate 0.07 -> 100*0.07*0.5 + 1 = 4.5 beats floor 2.
    EXPECT_DOUBLE_EQ(min_edge_floor(2.0, 0.07, 0.5, 1.0), 4.5);
    // price 0.99 -> 0.07 + 1 = 1.07, floor 2 wins.
    EXPECT_DOUBLE_EQ(min_edge_floor(2.0, 0.07, 0.99, 1.0), 2.0);
    EXPECT_EQ(min_edge_floor(2.0, std::nullopt, 0.5, 1.0), 2.0);
    EXPECT_EQ(min_edge_floor(2.0, 0.07, std::nullopt, 1.0), 2.0);
}

TEST(WalkLadder, StopsAtFirstNonPositive) {
    std::vector<LadderLevel> ladder{{0.40, 100}, {0.45, 50}, {0.60, 500}, {0.41, 10}};
    auto w = walk_ladder(ladder, 0.5, kZeroFee);
    EXPECT_EQ(w.shares, 150);  // 0.60 level is negative; the later 0.41 level is not reached
    EXPECT_DOUBLE_EQ(w.stake, 100 * 0.40 + 50 * 0.45);
    ASSERT_EQ(w.levels.size(), 2u);
    EXPECT_DOUBLE_EQ(w.levels[0].ev, 0.5 * 0.6 - 0.5 * 0.4);
    EXPECT_DOUBLE_EQ(w.ev, 100 * w.levels[0].ev + 50 * w.levels[1].ev);
}

TEST(WalkLadder, EmptyGivesZeros) {
    auto w = walk_ladder({}, 0.5, 0.07);
    EXPECT_EQ(w.shares, 0);
    EXPECT_EQ(w.stake, 0.0);
    EXPECT_EQ(w.ev, 0.0);
    EXPECT_TRUE(w.levels.empty());
}

TEST(WalkLadder, NativeAndCallbackAgree) {
    std::vector<LadderLevel> ladder{{0.30, 100}, {0.35, 80}, {0.41, 40}, {0.50, 10}};
    auto a = walk_ladder(ladder, 0.45, 0.07);
    FeeFn fn = [](double p, double) { return 0.07 * p * (1.0 - p); };
    auto b = walk_ladder(ladder, 0.45, fn);
    EXPECT_EQ(a.shares, b.shares);
    EXPECT_EQ(a.stake, b.stake);
    EXPECT_EQ(a.ev, b.ev);
    EXPECT_EQ(a.levels.size(), b.levels.size());
}

TEST(BreakevenFair, Behaviour) {
    EXPECT_NEAR(breakeven_fair(0.4, 0.0), 0.4, 1e-9);
    EXPECT_GT(breakeven_fair(0.4, 0.07), 0.4);
    FeeFn fn = [](double p, double) { return 0.07 * p * (1.0 - p); };
    EXPECT_EQ(breakeven_fair(0.4, 0.07), breakeven_fair(0.4, fn));
}

namespace {
GateInputs gate(double floor = 2.0, double ceiling = 15.0) {
    return GateInputs{floor, std::nullopt, 1.0, ceiling, 0.8};
}
}  // namespace

TEST(EvaluateCore, Verdicts) {
    // Empty ladder and no positive levels.
    EXPECT_EQ(evaluate_core({}, 0.5, 0.0, gate()).verdict, Verdict::NoLevels);
    EXPECT_EQ(evaluate_core({{0.6, 10}}, 0.5, 0.0, gate()).verdict, Verdict::NoLevels);
    // ev_pct = (fair - p)/p*100. p=0.49, fair=0.5 -> ~2.04% (accept), floor 5 -> below floor.
    EXPECT_EQ(evaluate_core({{0.49, 10}}, 0.5, 0.0, gate()).verdict, Verdict::Accept);
    EXPECT_EQ(evaluate_core({{0.49, 10}}, 0.5, 0.0, gate(5.0)).verdict, Verdict::BelowFloor);
    // p=0.40, fair=0.5 -> 25% -> over ceiling 15.
    EXPECT_EQ(evaluate_core({{0.40, 10}}, 0.5, 0.0, gate()).verdict, Verdict::OverCeiling);
    // p=0.44 -> 13.6% -> in [12, 15] near ceiling.
    EXPECT_EQ(evaluate_core({{0.44, 10}}, 0.5, 0.0, gate()).verdict, Verdict::AcceptNearCeiling);
}

TEST(EvaluateCore, ExactlyAtCeilingIsAccepted) {
    auto r = evaluate_core({{0.44, 10}}, 0.5, 0.0, gate());
    GateInputs g = gate(2.0, r.ev_pct);
    EXPECT_EQ(evaluate_core({{0.44, 10}}, 0.5, 0.0, g).verdict, Verdict::AcceptNearCeiling);
}

TEST(EvaluateCore, FloorUsesAvgFillNotBestAsk) {
    // Book rate 0.07, margin 1: floor at fill f is 7*(1-f)+1. Ladder fills 10@0.30 and 10@0.46
    // for avg 0.38 -> floor 5.34. Best ask 0.30 would give 5.9.
    std::vector<LadderLevel> ladder{{0.30, 10}, {0.46, 10}};
    GateInputs g{2.0, 0.07, 1.0, 100.0, 0.8};
    auto r = evaluate_core(ladder, 0.60, 0.0, g);
    EXPECT_DOUBLE_EQ(r.avg_fill, (10 * 0.30 + 10 * 0.46) / 20);
    double floor = min_edge_floor(2.0, 0.07, r.avg_fill, 1.0);
    EXPECT_GT(r.ev_pct, floor);
    EXPECT_EQ(r.verdict, Verdict::Accept);
    // Raise margin so the avg-fill floor sits just above ev_pct: BelowFloor.
    g.margin = r.ev_pct - 7.0 * (1 - r.avg_fill) + 0.01;
    EXPECT_EQ(evaluate_core(ladder, 0.60, 0.0, g).verdict, Verdict::BelowFloor);
}

TEST(Kelly, Formula) {
    auto k = kelly_size(0.55, 0.45, 0.0, 0.0, 1000.0, 0.25, std::nullopt, 1000000);
    ASSERT_TRUE(k);
    double b = (1 - 0.45) / 0.45;
    EXPECT_EQ(k->f_full, 0.55 - (1 - 0.55) / b);
    EXPECT_EQ(k->target_shares, static_cast<int64_t>(std::floor(1000.0 * k->f_full * 0.25 / 0.45)));
}

TEST(Kelly, EarlyReturns) {
    EXPECT_FALSE(kelly_size(0.40, 0.45, 0, 0, 1000, 0.25, std::nullopt, 100));  // no edge
    EXPECT_FALSE(kelly_size(0.55, 0.0, 0, 0, 1000, 0.25, std::nullopt, 100));   // vwap low
    EXPECT_FALSE(kelly_size(0.55, 1.0, 0, 0, 1000, 0.25, std::nullopt, 100));   // vwap high
    EXPECT_FALSE(kelly_size(0.55, 0.45, 0.6, 0, 1000, 0.25, std::nullopt, 100)); // net win <= 0
    EXPECT_FALSE(kelly_size(0.55, 0.45, 0, 0, 1.0, 0.25, std::nullopt, 100));   // zero shares
}

TEST(Kelly, MaxStakeAndShareCap) {
    auto uncapped = kelly_size(0.55, 0.45, 0, 0, 100000, 0.25, std::nullopt, 1000000);
    auto capped = kelly_size(0.55, 0.45, 0, 0, 100000, 0.25, 90.0, 1000000);
    ASSERT_TRUE(capped);
    EXPECT_EQ(capped->target_shares, static_cast<int64_t>(std::floor(90.0 / 0.45)));
    EXPECT_LT(capped->target_shares, uncapped->target_shares);
    auto shares_capped = kelly_size(0.55, 0.45, 0, 0, 100000, 0.25, std::nullopt, 7);
    EXPECT_EQ(shares_capped->target_shares, 7);
}
