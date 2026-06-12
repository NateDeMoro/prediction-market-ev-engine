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
