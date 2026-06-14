import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from pmev.core.devig import american_to_decimal, devig_multiplicative


# Group 1 — New guard: 0 < a < 100 returns None

def test_sub100_positive_midpoint():
    assert american_to_decimal(50) is None

def test_sub100_positive_lower_boundary():
    assert american_to_decimal(1) is None

def test_sub100_positive_upper_boundary():
    assert american_to_decimal(99) is None


# Group 2 — Valid inputs must not regress

def test_even_money_positive():
    assert american_to_decimal(100) == pytest.approx(2.0)

def test_standard_positive_favourite():
    assert american_to_decimal(110) == pytest.approx(2.1)

def test_standard_negative():
    assert american_to_decimal(-110) == pytest.approx(1.0 + 100.0 / 110)

def test_even_money_negative():
    assert american_to_decimal(-100) == pytest.approx(2.0)

def test_large_positive_underdog():
    assert american_to_decimal(300) == pytest.approx(4.0)

def test_large_negative_favourite():
    assert american_to_decimal(-300) == pytest.approx(1.0 + 100.0 / 300)


# Group 3 — Existing degenerate guards must not regress

def test_zero_returns_none():
    assert american_to_decimal(0) is None

def test_none_returns_none():
    assert american_to_decimal(None) is None

def test_nonnumeric_returns_none():
    assert american_to_decimal("abc") is None

def test_string_numeric_is_accepted():
    assert american_to_decimal("110") == pytest.approx(2.1)


# Group 4 — Propagation through devig_multiplicative

def test_devig_sub100_positive_propagates_none():
    assert devig_multiplicative([50, -110]) is None

def test_devig_valid_two_way_book():
    result = devig_multiplicative([110, -130])
    assert result is not None
    assert len(result) == 2
    assert sum(result) == pytest.approx(1.0)
    assert all(0 < p < 1 for p in result)

def test_devig_zero_price_propagates_none():
    assert devig_multiplicative([0, -110]) is None

def test_devig_empty_list_returns_none():
    assert devig_multiplicative([]) is None
