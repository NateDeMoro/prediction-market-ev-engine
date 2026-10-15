"""Devigging helpers shared between find_ev_bet.py (live EV scan),
paper_tracker.py (CLV capture), and any future analytics.

Open when: changing how American odds are converted or how vig is removed
(e.g. switching from multiplicative to power / Shin / logit devig).

The arithmetic lives in C++ (cpp/src/pricing.cpp, module pmev_pricing); these
wrappers keep None / str / float coercion and the public signatures.
"""
import pmev_pricing as _pp


def _coerce(a):
    """float(a), or None for None / non-numeric input."""
    if a is None:
        return None
    try:
        return float(a)
    except (TypeError, ValueError):
        return None


def _coerce_legs(american_prices):
    """Coerce every leg to float; None if any leg is None or non-numeric."""
    legs = []
    for p in american_prices:
        v = _coerce(p)
        if v is None:
            return None
        legs.append(v)
    return legs


def american_to_decimal(a):
    """Standard American-odds to decimal. None in, None out. Uses |a| in the
    denominator so both +N and -N are handled symmetrically. Returns None on
    a=0, non-numeric input, or 0 < a < 100 (non-standard sub-100 odds that
    would silently invert the implied probability if allowed through)."""
    a = _coerce(a)
    if a is None:
        return None
    return _pp.american_to_decimal(a)


def devig_multiplicative(american_prices):
    """Multiplicative (a.k.a. proportional) devig of a 2-way (or n-way) book.

    Returns a list of fair probabilities summing to 1, or None if the input
    is degenerate (all zeros, malformed). Callers must handle None — a return
    of None means no reliable fair prob could be derived from this book.
    """
    legs = _coerce_legs(american_prices)
    if legs is None:
        return None
    return _pp.devig_multiplicative(legs)


def _implied_probs(american_prices):
    """American prices -> list of raw implied probs (1/decimal), or None if any
    leg is degenerate. Sum is the overround (>1 for a normal vigged book)."""
    legs = _coerce_legs(american_prices)
    if legs is None:
        return None
    return _pp.implied_probs(legs)


def devig_power(american_prices, _tol=1e-12, _iters=200):
    """Power devig: find exponent k with Σ pᵢᵏ = 1, then fairᵢ = pᵢᵏ.

    Unlike multiplicative (which splits vig proportionally and so depends only on
    the price *ratio*), power removes proportionally more vig from longshots,
    partially correcting favorite-longshot bias. Always feasible (stays in
    [0,1]); sits between multiplicative and Shin in aggressiveness.

    Returns a list of fair probabilities summing to 1, or None on degenerate
    input. Callers must handle None.
    """
    legs = _coerce_legs(american_prices)
    if legs is None:
        return None
    return _pp.devig_power(legs, _tol, _iters)


def devig_shin(american_prices, _tol=1e-12, _iters=200):
    """Shin (1993) devig: models the book's margin as a defense against insider
    money, removing more vig from longshots than power does. Solve for the
    insider fraction z ∈ [0,1) such that Σ fairᵢ(z) = 1, where

        fairᵢ(z) = ( sqrt(z² + 4(1-z)·πᵢ²/Σπ) − z ) / (2(1-z))

    with πᵢ the raw implied probs and Σπ the overround. Strongest of the three
    favorite-longshot corrections; for a 2-way market it approximates additive.

    Returns a list of fair probabilities summing to 1, or None on degenerate
    input. Callers must handle None.
    """
    legs = _coerce_legs(american_prices)
    if legs is None:
        return None
    return _pp.devig_shin(legs, _tol, _iters)


def synthesize_combined_american(american_prices):
    """Collapse N American prices into one with implied prob = Σ inputs' probs.

    Use when: flattening a 3-way market's other legs into a synthetic NO so
    2-way devig on [yes, synthesized] equals full 3-way devig on the YES leg.

    Returns None on degenerate input or combined implied prob >= 1.
    """
    legs = _coerce_legs(american_prices)
    if legs is None:
        return None
    return _pp.synthesize_combined_american(legs)
