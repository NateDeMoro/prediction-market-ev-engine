"""Exact-parity tests: the C++ pricing core (pmev_pricing) vs frozen copies of
the pre-port Python implementations.

Open when: changing cpp/src/pricing.cpp or a pmev wrapper around it and you need
to prove no number moved. References below are verbatim copies of the Python that
was replaced; do not "improve" them. Parity holds on Python >= 3.12, where the
built-in sum() uses compensated summation.
"""
import math
import sys

import pytest

from pmev import config
from pmev.adapters import kalshi, polymarket
from pmev.core import devig
from pmev.execution import paper
from pmev.matching import ev

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="parity is defined against compensated sum() (3.12+)")


# ---------------------------------------------------------------------------
# Frozen pre-port references
# ---------------------------------------------------------------------------

def ref_american_to_decimal(a):
    if a is None:
        return None
    try:
        a = float(a)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    if 0 < a < 100:
        return None
    if a >= 100:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / abs(a)


def ref_devig_multiplicative(american_prices):
    probs = []
    for p in american_prices:
        d = ref_american_to_decimal(p)
        if d is None or d <= 0:
            return None
        probs.append(1.0 / d)
    total = sum(probs)
    if total <= 0:
        return None
    return [p / total for p in probs]


def ref_implied_probs(american_prices):
    probs = []
    for p in american_prices:
        d = ref_american_to_decimal(p)
        if d is None or d <= 0:
            return None
        probs.append(1.0 / d)
    if not probs or sum(probs) <= 0:
        return None
    return probs


def ref_devig_power(american_prices, _tol=1e-12, _iters=200):
    probs = ref_implied_probs(american_prices)
    if probs is None:
        return None
    lo, hi = 0.0, 1.0
    while sum(p ** hi for p in probs) > 1.0:
        hi *= 2.0
        if hi > 1e6:
            return None
    for _ in range(_iters):
        mid = 0.5 * (lo + hi)
        s = sum(p ** mid for p in probs)
        if abs(s - 1.0) < _tol:
            break
        if s > 1.0:
            lo = mid
        else:
            hi = mid
    k = 0.5 * (lo + hi)
    fair = [p ** k for p in probs]
    total = sum(fair)
    if total <= 0:
        return None
    return [f / total for f in fair]


def ref_devig_shin(american_prices, _tol=1e-12, _iters=200):
    probs = ref_implied_probs(american_prices)
    if probs is None:
        return None
    booksum = sum(probs)

    def fairs(z):
        denom = 2.0 * (1.0 - z)
        out = []
        for pi in probs:
            root = (z * z + 4.0 * (1.0 - z) * pi * pi / booksum) ** 0.5
            out.append((root - z) / denom)
        return out

    lo, hi = 0.0, 1.0 - 1e-9
    if sum(fairs(lo)) <= 1.0:
        return ref_devig_multiplicative(american_prices)
    for _ in range(_iters):
        mid = 0.5 * (lo + hi)
        s = sum(fairs(mid))
        if abs(s - 1.0) < _tol:
            break
        if s > 1.0:
            lo = mid
        else:
            hi = mid
    fair = fairs(0.5 * (lo + hi))
    total = sum(fair)
    if total <= 0:
        return None
    return [f / total for f in fair]


def ref_synthesize_combined_american(american_prices):
    combined_prob = 0.0
    for a in american_prices:
        d = ref_american_to_decimal(a)
        if d is None or d <= 0:
            return None
        combined_prob += 1.0 / d
    if combined_prob <= 0 or combined_prob >= 1:
        return None
    combined_decimal = 1.0 / combined_prob
    if combined_decimal >= 2.0:
        return int(round((combined_decimal - 1.0) * 100.0))
    return int(round(-100.0 / (combined_decimal - 1.0)))


def ref_price_to_american(price):
    if price <= 0 or price >= 1:
        return 0
    if price <= 0.5:
        return int(round(100 * (1 - price) / price))
    return int(round(-100 * price / (1 - price)))


def ref_walk_ladder(ladder, fair_prob, fee_fn):
    levels = []
    total_shares, total_stake, total_ev = 0, 0.0, 0.0
    for price, qty in ladder:
        gross = 1.0 - price
        ev_ = fair_prob * gross - (1 - fair_prob) * price - fee_fn(price, fair_prob)
        if ev_ <= 0:
            break
        levels.append((price, qty, ev_))
        total_shares += qty
        total_stake += qty * price
        total_ev += qty * ev_
    return total_shares, total_stake, total_ev, levels


def ref_breakeven_fair(price, fee_fn):
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        ev_ = mid * (1 - price) - (1 - mid) * price - fee_fn(price, mid)
        if ev_ > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def ref_min_edge_pct(book, market_type, avg_fill_price):
    floor = config.EDGE_FLOOR_PCT.get(
        (book, market_type),
        config.PROP_MIN_EDGE_PCT if market_type == "player_prop" else config.MIN_EDGE_PCT,
    )
    rate = config.PER_BOOK_FEE_RATE.get(book)
    if rate is None or avg_fill_price is None:
        return floor
    margin = config.EDGE_MARGIN_PP.get((book, market_type), 1.0)
    be_plus_margin = 100.0 * rate * (1.0 - avg_fill_price) + margin
    return max(floor, be_plus_margin)


def ref_evaluate(ladder, fair, fee_fn, book, market_type):
    """Returns (result_or_None, printed_lines)."""
    lines = []
    if not ladder:
        return None, lines
    shares, stake, exp_profit, levels = ref_walk_ladder(ladder, fair, fee_fn)
    if shares == 0:
        return None, lines
    ev_pct = exp_profit / stake * 100 if stake > 0 else 0.0
    avg_fill = stake / shares
    if ev_pct < ref_min_edge_pct(book, market_type, avg_fill):
        return None, lines
    is_prop = market_type == "player_prop"
    max_edge = config.SANITY_MAX_EDGE_PCT_PROP if is_prop else config.SANITY_MAX_EDGE_PCT
    if ev_pct > max_edge:
        lines.append(f"[evaluate] over-ceiling reject book={book} type={market_type} "
                     f"ev={ev_pct:.2f}% ceiling={max_edge:.1f}% avg_fill={avg_fill:.3f}")
        return None, lines
    if ev_pct >= config.CEILING_LOG_FRAC * max_edge:
        lines.append(f"[evaluate] near-ceiling book={book} type={market_type} "
                     f"ev={ev_pct:.2f}% ceiling={max_edge:.1f}% avg_fill={avg_fill:.3f}")
    return {"shares": shares, "stake": stake, "exp_profit": exp_profit,
            "ev_pct": ev_pct, "avg_fill": avg_fill, "levels": levels}, lines


def ref_size_bet(ladder, fair_prob, bankroll, adapter, max_stake=None):
    """Pre-port size_bet; shares the (unchanged) _walk_positive_ev / _fill_shares helpers."""
    pos_levels, pos_shares, pos_stake = paper._walk_positive_ev(ladder, fair_prob, adapter)
    if pos_shares <= 0 or pos_stake <= 0:
        return None
    vwap = pos_stake / pos_shares
    if vwap <= 0 or vwap >= 1:
        return None
    net_win = (1 - vwap) - adapter.fee_on_win_per_share(vwap)
    eff_stake = vwap + adapter.fee_on_stake_per_share(vwap)
    if net_win <= 0 or eff_stake <= 0:
        return None
    b = net_win / eff_stake
    f_full = fair_prob - (1 - fair_prob) / b
    if f_full <= 0:
        return None
    kelly_stake_budget = bankroll * f_full * config.KELLY_FRACTION
    if max_stake is not None:
        kelly_stake_budget = min(kelly_stake_budget, max_stake)
    if kelly_stake_budget <= 0:
        return None
    target_shares = int(math.floor(kelly_stake_budget / eff_stake))
    shares = min(target_shares, pos_shares)
    if shares <= 0:
        return None
    filled, price_stake, detail = paper._fill_shares(pos_levels, shares)
    if filled <= 0:
        return None
    avg_fill = price_stake / filled
    fee_upfront = filled * adapter.fee_on_stake_per_share(avg_fill)
    total_stake = price_stake + fee_upfront
    expected_fee = adapter.taker_fee_per_share(avg_fill, fair_prob)
    ev_per_share = (fair_prob * (1 - avg_fill) - (1 - fair_prob) * avg_fill - expected_fee)
    expected_profit = ev_per_share * filled
    return {
        "shares": filled,
        "stake": round(total_stake, 4),
        "price_stake": round(price_stake, 4),
        "fee_upfront": round(fee_upfront, 4),
        "avg_fill_price": round(avg_fill, 6),
        "kelly_fraction_full": round(f_full, 6),
        "kelly_fraction_applied": config.KELLY_FRACTION,
        "expected_profit": round(expected_profit, 4),
        "levels": detail,
    }


# ---------------------------------------------------------------------------
# Grids
# ---------------------------------------------------------------------------

CENT_PRICES = [c / 100 for c in range(1, 100)]

_AMERICAN = [-1500, -800, -400, -250, -200, -165, -150, -130, -120, -115, -110, -105, -102, -100,
             100, 102, 105, 110, 115, 120, 130, 150, 165, 200, 250, 300, 400, 600, 900, 1500]
TWO_WAY = [[a, b] for a in _AMERICAN for b in _AMERICAN]
THREE_WAY = [[a, b, c] for a in (-300, -150, 110, 200, 400)
             for b in (-200, 120, 250, 330) for c in (-120, 180, 300, 700)]
BAD_BOOKS = [[], [0, -110], [50, -110], [None, -110], ["abc", -110], [-110], [99.9, 100],
             ["-110", "+100"], [-110.5, 105.5]]
ALL_BOOKS = TWO_WAY + THREE_WAY + BAD_BOOKS

LADDERS = [
    [],
    [(0.40, 100)],
    [(0.30, 100), (0.35, 80), (0.41, 40), (0.50, 10)],
    [(0.01, 5), (0.02, 1000), (0.45, 7), (0.55, 300)],
    [(0.62, 20), (0.64, 50), (0.70, 900), (0.91, 3)],
    [(0.12, 1), (0.15, 2), (0.19, 4), (0.24, 8), (0.30, 16), (0.37, 32), (0.45, 64)],
    [(0.97, 400), (0.98, 400)],
]
FAIRS = [0.02, 0.1, 0.25, 0.33, 0.4, 0.4999, 0.5, 0.55, 0.62, 0.7, 0.83, 0.95, 0.99]


def zero_fee(price, fair):
    return 0.0


FEES = {"zero": zero_fee, "kalshi": kalshi.taker_fee_per_share,
        "polymarket": polymarket.taker_fee_per_share}


# ---------------------------------------------------------------------------
# Odds + devig
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a", [None, 0, 50, 99.99, 100, 101, -100, -101, -50, "150", "x", 2500, -0.5])
def test_american_to_decimal(a):
    assert devig.american_to_decimal(a) == ref_american_to_decimal(a)


def test_price_to_american_every_cent_and_edges():
    for p in CENT_PRICES + [0, 1, -0.1, 1.5, 0.005, 0.995, 0.3333, 0.6667]:
        assert ev.price_to_american(p) == ref_price_to_american(p), p


@pytest.mark.parametrize("fn,ref", [
    (devig.devig_multiplicative, ref_devig_multiplicative),
    (devig.devig_power, ref_devig_power),
    (devig.devig_shin, ref_devig_shin),
    (devig._implied_probs, ref_implied_probs),
    (devig.synthesize_combined_american, ref_synthesize_combined_american),
])
def test_devig_family_exact(fn, ref):
    for book in ALL_BOOKS:
        assert fn(book) == ref(book), (fn.__name__, book)


def test_power_and_shin_custom_tolerance():
    for book in TWO_WAY[::17]:
        assert devig.devig_power(book, _tol=1e-6, _iters=20) == ref_devig_power(book, 1e-6, 20)
        assert devig.devig_shin(book, _tol=1e-6, _iters=20) == ref_devig_shin(book, 1e-6, 20)


def test_synthesize_exact_half_rounds_to_even():
    # Prob 0.4 -> decimal 2.5 -> (2.5-1)*100 = 150; construct an exact .5 via the grid instead.
    for book in TWO_WAY + THREE_WAY:
        assert devig.synthesize_combined_american(book) == ref_synthesize_combined_american(book)
    assert ev.price_to_american(0.32) == 212 == ref_price_to_american(0.32)


# ---------------------------------------------------------------------------
# Ladder, breakeven, evaluate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fee_name", list(FEES))
def test_walk_ladder_exact(fee_name):
    fee = FEES[fee_name]
    for ladder in LADDERS:
        for fair in FAIRS:
            assert ev.walk_ladder(ladder, fair, fee) == ref_walk_ladder(ladder, fair, fee)


@pytest.mark.parametrize("fee_name", list(FEES))
def test_breakeven_fair_exact(fee_name):
    fee = FEES[fee_name]
    for p in CENT_PRICES:
        assert ev.breakeven_fair(p, fee) == ref_breakeven_fair(p, fee), p


@pytest.mark.parametrize("book,mod", [("kalshi", kalshi), ("polymarket", polymarket)])
@pytest.mark.parametrize("market_type", ["moneyline", "total", "player_prop"])
def test_evaluate_exact(book, mod, market_type, capsys):
    for ladder in LADDERS:
        for fair in FAIRS:
            for fee in (zero_fee, mod.taker_fee_per_share):
                capsys.readouterr()
                got = ev.evaluate(ladder, fair, fee, book, market_type)
                got_out = capsys.readouterr().out
                want, lines = ref_evaluate(ladder, fair, fee, book, market_type)
                assert got == want
                assert got_out == "".join(line + "\n" for line in lines)


def test_evaluate_unknown_book_uses_floor_only(capsys):
    ladder = LADDERS[2]
    for fair in FAIRS:
        got = ev.evaluate(ladder, fair, zero_fee, "test_book", "moneyline")
        want, _ = ref_evaluate(ladder, fair, zero_fee, "test_book", "moneyline")
        assert got == want


def test_min_edge_and_haircut_exact():
    for book in ("kalshi", "polymarket", "other"):
        for mt in ("moneyline", "player_prop", "weird"):
            for fill in (None, 0.05, 0.3, 0.5, 0.9, 0.99):
                assert config.min_edge_pct(book, mt, fill) == ref_min_edge_pct(book, mt, fill)
    assert config.haircut_fair(None) is None
    for p in [0.0, 0.01, 0.37, 0.5, 0.99, 1.0]:
        assert config.haircut_fair(p) == p - config.HAIRCUT_K * p * (1.0 - p)


# ---------------------------------------------------------------------------
# Adapter fee_rate guard, native vs callback
# ---------------------------------------------------------------------------

def test_adapter_fee_rate_matches_config():
    assert kalshi.taker_fee_per_share.fee_rate == config.PER_BOOK_FEE_RATE["kalshi"]
    assert polymarket.taker_fee_per_share.fee_rate == config.PER_BOOK_FEE_RATE["polymarket"]
    for p in CENT_PRICES:
        assert kalshi.fee_on_stake_per_share(p) == config.PER_BOOK_FEE_RATE["kalshi"] * p * (1.0 - p)
        assert polymarket.fee_on_stake_per_share(p) == config.PER_BOOK_FEE_RATE["polymarket"] * p * (1.0 - p)


@pytest.mark.parametrize("mod", [kalshi, polymarket])
def test_native_and_callback_paths_agree(mod):
    def equiv(price, fair):  # no fee_rate attribute
        return mod.FEE_RATE * price * (1.0 - price)
    assert not hasattr(equiv, "fee_rate")
    for ladder in LADDERS:
        for fair in FAIRS:
            assert ev.walk_ladder(ladder, fair, mod.taker_fee_per_share) == \
                ev.walk_ladder(ladder, fair, equiv)
    for p in CENT_PRICES:
        assert ev.breakeven_fair(p, mod.taker_fee_per_share) == ev.breakeven_fair(p, equiv)


@pytest.mark.parametrize("mod", [kalshi, polymarket])
def test_adapter_fee_rate_reproduces_callable(mod):
    fn = mod.taker_fee_per_share
    for p in CENT_PRICES + [0.0, 0.001, 0.4999, 0.999, 1.0]:
        for fair in (0.0, p, 1.0):
            assert fn(p, fair) == fn.fee_rate * p * (1.0 - p)


@pytest.mark.parametrize("mod", [kalshi, polymarket])
def test_patched_fee_rate_does_not_diverge(mod, monkeypatch):
    monkeypatch.setattr(mod, "FEE_RATE", mod.FEE_RATE * 2)
    def equiv(price, fair):
        return mod.FEE_RATE * price * (1.0 - price)
    assert ev._native_fee_rate(mod.taker_fee_per_share) is None
    for ladder in LADDERS:
        for fair in FAIRS:
            assert ev.walk_ladder(ladder, fair, mod.taker_fee_per_share) == \
                ev.walk_ladder(ladder, fair, equiv)
    for p in CENT_PRICES:
        assert ev.breakeven_fair(p, mod.taker_fee_per_share) == ev.breakeven_fair(p, equiv)


def test_mismatched_fee_rate_tag_falls_back_to_callback():
    def rounded(price, fair):
        return math.ceil(0.07 * price * (1.0 - price) * 100) / 100
    rounded.fee_rate = 0.07
    assert ev._native_fee_rate(rounded) is None
    for ladder in LADDERS:
        for fair in FAIRS:
            assert ev.walk_ladder(ladder, fair, rounded) == ref_walk_ladder(ladder, fair, rounded)


def test_non_numeric_fee_rate_attribute_falls_back_to_callback():
    class Weird:
        fee_rate = "n/a"
        def __call__(self, price, fair):
            return 0.0
    assert ev.walk_ladder(LADDERS[2], 0.5, Weird()) == ref_walk_ladder(LADDERS[2], 0.5, zero_fee)


# ---------------------------------------------------------------------------
# size_bet (Kelly)
# ---------------------------------------------------------------------------

class FakeAdapter:
    def __init__(self, mod):
        self.mod = mod
    def taker_fee_per_share(self, price, fair):
        return self.mod.taker_fee_per_share(price, fair)
    def fee_on_stake_per_share(self, price):
        return self.mod.fee_on_stake_per_share(price)
    def fee_on_win_per_share(self, price):
        return 0.0


class WinFeeAdapter(FakeAdapter):
    """Exercises a non-zero fee on win and the net_win <= 0 early return."""
    def __init__(self, win_fee):
        super().__init__(kalshi)
        self.win_fee = win_fee
    def fee_on_win_per_share(self, price):
        return self.win_fee


@pytest.mark.parametrize("mod", [kalshi, polymarket])
def test_size_bet_exact(mod):
    adapter = FakeAdapter(mod)
    for ladder in LADDERS:
        for fair in FAIRS:
            for bankroll in (10.0, 250.0, 5000.0, 123456.78):
                for cap in (None, 3.0, 40.0, 1e9):
                    assert paper.size_bet(ladder, fair, bankroll, adapter, max_stake=cap) == \
                        ref_size_bet(ladder, fair, bankroll, adapter, max_stake=cap)


@pytest.mark.parametrize("win_fee", [0.0, 0.01, 0.2, 0.7, 1.5])
def test_size_bet_with_win_fee(win_fee):
    adapter = WinFeeAdapter(win_fee)
    for ladder in LADDERS[1:4]:
        for fair in FAIRS:
            assert paper.size_bet(ladder, fair, 5000.0, adapter) == \
                ref_size_bet(ladder, fair, 5000.0, adapter)


def test_size_bet_pinned_result():
    ladder = [(0.40, 200), (0.44, 300), (0.80, 100)]
    got = paper.size_bet(ladder, 0.52, 5000.0, FakeAdapter(kalshi))
    assert got == ref_size_bet(ladder, 0.52, 5000.0, FakeAdapter(kalshi))
    # Pinned numbers, computed from the pre-port implementation.
    assert got["shares"] == 431
    assert got["stake"] == 188.9963
    assert got["kelly_fraction_full"] == 0.15242
    assert got["expected_profit"] == 35.1237
    assert got["levels"] == [[0.4, 200], [0.44, 231]]
