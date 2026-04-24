"""Devigging helpers shared between find_ev_bet.py (live EV scan),
paper_tracker.py (CLV capture), and any future analytics.

Open when: changing how American odds are converted or how vig is removed
(e.g. switching from multiplicative to power / Shin / logit devig).
"""


def american_to_decimal(a):
    """Standard American-odds to decimal. None in, None out. Uses |a| in the
    denominator so both +N and -N are handled symmetrically. Assumes |a|>=100
    (the Pinnacle convention); returns None on a=0 or non-numeric input so
    callers handle degenerate quotes (0 = unquoted) without crashing."""
    if a is None:
        return None
    try:
        a = float(a)
    except (TypeError, ValueError):
        return None
    if a == 0:
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
    """Collapse N American prices into one American price whose implied
    probability equals the sum of inputs' implied probs.

    Use when: flattening a 3-way market's "other two" legs into a synthetic
    NO price so the existing 2-way devig yields the correct fair prob for
    the YES leg (multiplicative devig on [yes, synthesized] equals full
    3-way devig on [home, away, draw] for the YES outcome).

    Returns None if any input is degenerate or the combined implied prob
    is >= 1 (no finite American representation for a guaranteed outcome).
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
