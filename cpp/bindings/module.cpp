// pybind11 bindings for the pricing core: top-level module `pmev_pricing`.
// Open when: adding or changing a function exposed to Python. The Python
// wrappers in pmev/ keep validation, None handling, and config reads.
#include <pybind11/functional.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "pmev/pricing.hpp"

namespace py = pybind11;
using namespace pmev;

namespace {

using PyLadder = std::vector<std::pair<double, int64_t>>;
using PyLevels = std::vector<std::tuple<double, int64_t, double>>;

std::vector<LadderLevel> to_ladder(const PyLadder& in) {
    std::vector<LadderLevel> out;
    out.reserve(in.size());
    for (const auto& [price, qty] : in) out.push_back({price, qty});
    return out;
}

PyLevels to_levels(const std::vector<WalkLevel>& in) {
    PyLevels out;
    out.reserve(in.size());
    for (const auto& l : in) out.emplace_back(l.price, l.qty, l.ev);
    return out;
}

using PyWalk = std::tuple<int64_t, double, double, PyLevels>;

PyWalk to_walk(const WalkResult& w) { return {w.shares, w.stake, w.ev, to_levels(w.levels)}; }

// (verdict, shares, stake, exp_profit, ev_pct, avg_fill, levels)
using PyEval = std::tuple<Verdict, int64_t, double, double, double, double, PyLevels>;

PyEval to_eval(const EvalResult& e) {
    return {e.verdict, e.shares, e.stake, e.exp_profit, e.ev_pct, e.avg_fill,
            to_levels(e.levels)};
}

GateInputs make_gate(double floor, std::optional<double> rate, double margin,
                     double ceiling, double log_frac) {
    return GateInputs{floor, rate, margin, ceiling, log_frac};
}

}  // namespace

PYBIND11_MODULE(pmev_pricing, m) {
    m.doc() = "C++17 pricing core for the EV engine (odds, devig, fees, ladder, gate, Kelly).";

    py::enum_<Verdict>(m, "Verdict")
        .value("NoLevels", Verdict::NoLevels)
        .value("BelowFloor", Verdict::BelowFloor)
        .value("OverCeiling", Verdict::OverCeiling)
        .value("AcceptNearCeiling", Verdict::AcceptNearCeiling)
        .value("Accept", Verdict::Accept);

    m.def("compensated_sum", &compensated_sum);
    m.def("round_half_even", &round_half_even);
    m.def("american_to_decimal", &american_to_decimal);
    m.def("price_to_american", &price_to_american);
    m.def("synthesize_combined_american", &synthesize_combined_american);
    m.def("implied_probs", &implied_probs);
    m.def("devig_multiplicative", &devig_multiplicative);
    m.def("devig_power", &devig_power, py::arg("american"), py::arg("tol"), py::arg("iters"));
    m.def("devig_shin", &devig_shin, py::arg("american"), py::arg("tol"), py::arg("iters"));
    m.def("haircut_fair", &haircut_fair);
    m.def("quadratic_fee", &quadratic_fee);
    m.def("min_edge_floor", &min_edge_floor, py::arg("floor"), py::arg("rate"),
          py::arg("avg_fill"), py::arg("margin"));

    // Native quadratic-fee path (rate is a float) and callback path (fee_fn is a callable).
    m.def("walk_ladder", [](const PyLadder& l, double fair, double rate) {
        return to_walk(walk_ladder(to_ladder(l), fair, rate));
    });
    m.def("walk_ladder_cb", [](const PyLadder& l, double fair, const FeeFn& fee) {
        return to_walk(walk_ladder(to_ladder(l), fair, fee));
    });
    m.def("breakeven_fair", [](double price, double rate) { return breakeven_fair(price, rate); });
    m.def("breakeven_fair_cb",
          [](double price, const FeeFn& fee) { return breakeven_fair(price, fee); });

    m.def("evaluate_core",
          [](const PyLadder& l, double fair, double rate, double floor,
             std::optional<double> book_rate, double margin, double ceiling, double log_frac) {
              return to_eval(evaluate_core(to_ladder(l), fair, rate,
                                           make_gate(floor, book_rate, margin, ceiling, log_frac)));
          },
          py::arg("ladder"), py::arg("fair"), py::arg("fee_rate"), py::arg("floor"),
          py::arg("book_fee_rate"), py::arg("margin"), py::arg("ceiling"),
          py::arg("ceiling_log_frac"));
    m.def("evaluate_core_cb",
          [](const PyLadder& l, double fair, const FeeFn& fee, double floor,
             std::optional<double> book_rate, double margin, double ceiling, double log_frac) {
              return to_eval(evaluate_core(to_ladder(l), fair, fee,
                                           make_gate(floor, book_rate, margin, ceiling, log_frac)));
          },
          py::arg("ladder"), py::arg("fair"), py::arg("fee_fn"), py::arg("floor"),
          py::arg("book_fee_rate"), py::arg("margin"), py::arg("ceiling"),
          py::arg("ceiling_log_frac"));

    // Returns (f_full, target_shares) or None.
    m.def("kelly_size",
          [](double fair, double vwap, double fee_on_win, double fee_on_stake, double bankroll,
             double kelly_fraction, std::optional<double> max_stake, int64_t pos_shares)
              -> std::optional<std::pair<double, int64_t>> {
              auto r = kelly_size(fair, vwap, fee_on_win, fee_on_stake, bankroll, kelly_fraction,
                                  max_stake, pos_shares);
              if (!r) return std::nullopt;
              return std::make_pair(r->f_full, r->target_shares);
          });
}
