"""Devigging helpers shared between find_ev_bet.py (live EV scan),
paper_tracker.py (CLV capture), and any future analytics.

Open when: changing how American odds are converted or how vig is removed
(e.g. switching from multiplicative to power / Shin / logit devig).
"""


def american_to_decimal(a):
    """Standard American-odds to decimal. None in, None out. Uses |a| in the
    denominator so both +N and -N are handled symmetrically. Returns None on
    a=0, non-numeric input, or 0 < a < 100 (non-standard sub-100 odds that
    would silently invert the implied probability if allowed through)."""
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


def devig_multiplicative(american_prices):
    """Multiplicative (a.k.a. proportional) devig of a 2-way (or n-way) book.

    Returns a list of fair probabilities summing to 1, or None if the input
    is degenerate (all zeros, malformed). Callers must handle None — a return
    of None means no reliable fair prob could be derived from this book.
    """
    probs = []
    for p in american_prices:
        d = american_to_decimal(p)
        if d is None or d <= 0:
            return None
        probs.append(1.0 / d)
    total = sum(probs)
    if total <= 0:
        return None
    return [p / total for p in probs]


def _implied_probs(american_prices):
    """American prices -> list of raw implied probs (1/decimal), or None if any
    leg is degenerate. Sum is the overround (>1 for a normal vigged book)."""
    probs = []
    for p in american_prices:
        d = american_to_decimal(p)
        if d is None or d <= 0:
            return None
        probs.append(1.0 / d)
    if not probs or sum(probs) <= 0:
        return None
    return probs


def devig_power(american_prices, _tol=1e-12, _iters=200):
    """Power devig: find exponent k with Σ pᵢᵏ = 1, then fairᵢ = pᵢᵏ.

    Unlike multiplicative (which splits vig proportionally and so depends only on
    the price *ratio*), power removes proportionally more vig from longshots,
    partially correcting favorite-longshot bias. Always feasible (stays in
    [0,1]); sits between multiplicative and Shin in aggressiveness.

    Returns a list of fair probabilities summing to 1, or None on degenerate
    input. Callers must handle None.
    """
    probs = _implied_probs(american_prices)
    if probs is None:
        return None
    # f(k) = Σ pᵢᵏ - 1 is strictly decreasing in k (every pᵢ < 1): f(0)=n-1>0,
    # f(∞)→-1. Bisect for the unique root. lo=0 covers the no-vig/arb case (k<1).
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
    return [f / total for f in fair]  # renormalize away residual bisection drift


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
    probs = _implied_probs(american_prices)
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

    # g(z) = Σ fairᵢ(z) - 1 is decreasing on [0,1): g(0)=sqrt(booksum)-1>0 for a
    # vigged book, g→<0 as z→1. Bisect for the unique root.
    lo, hi = 0.0, 1.0 - 1e-9
    if sum(fairs(lo)) <= 1.0:
        return devig_multiplicative(american_prices)  # no/negative vig -> no Shin
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
    return [f / total for f in fair]  # renormalize away residual bisection drift


def synthesize_combined_american(american_prices):
    """Collapse N American prices into one with implied prob = Σ inputs' probs.

    Use when: flattening a 3-way market's other legs into a synthetic NO so
    2-way devig on [yes, synthesized] equals full 3-way devig on the YES leg.

    Returns None on degenerate input or combined implied prob >= 1.
    """
    combined_prob = 0.0
    for a in american_prices:
        d = american_to_decimal(a)
        if d is None or d <= 0:
            return None
        combined_prob += 1.0 / d
    if combined_prob <= 0 or combined_prob >= 1:
        return None
    combined_decimal = 1.0 / combined_prob
    if combined_decimal >= 2.0:
        return int(round((combined_decimal - 1.0) * 100.0))
    return int(round(-100.0 / (combined_decimal - 1.0)))
