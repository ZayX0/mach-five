"""Fair value from a sharp two-way price (Pinnacle moneyline).

Adapted from mlb-prop-finder's market/odds.py + market/devig.py. A binary
Polymarket game market is the same shape as a moneyline: two complementary
outcomes. Pipeline is:

    American odds --american_to_prob--> implied prob (with vig)
                  --devig-------------> no-vig prob that side A wins

Multiplicative is the default; power method shades more onto the favourite
for markets with a strong fav/longshot split.
"""
from __future__ import annotations


# --- American-odds conversion (from market/odds.py) -----------------------
def american_to_prob(price: int) -> float:
    """Implied probability from American odds. Includes the vig."""
    if price == 0:
        raise ValueError("American odds cannot be 0")
    if price > 0:
        return 100.0 / (price + 100.0)
    return -price / (-price + 100.0)


# --- devig (from market/devig.py) -----------------------------------------
def overround(p_a: float, p_b: float) -> float:
    """Total implied probability minus 1 — the book's hold."""
    return p_a + p_b - 1.0


def devig_multiplicative(p_a: float, p_b: float) -> tuple[float, float]:
    """Scale both sides down proportionally so they sum to 1."""
    _validate(p_a, p_b)
    total = p_a + p_b
    return p_a / total, p_b / total


def devig_power(p_a: float, p_b: float, *, tol: float = 1e-10) -> tuple[float, float]:
    """Power method: find k so that p_a**k + p_b**k == 1.

    k > 1 when margin is present (both shrink); shades more probability onto
    the favourite than the multiplicative method.
    """
    _validate(p_a, p_b)
    if abs(p_a + p_b - 1.0) <= tol:
        return p_a, p_b
    lo, hi = 1e-6, 100.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if p_a**mid + p_b**mid > 1.0:
            lo = mid
        else:
            hi = mid
        if hi - lo <= tol:
            break
    k = (lo + hi) / 2.0
    return p_a**k, p_b**k


def _validate(p_a: float, p_b: float) -> None:
    if not (0.0 < p_a < 1.0 and 0.0 < p_b < 1.0):
        raise ValueError(f"probabilities must be in (0, 1), got {p_a}, {p_b}")


# --- the thing the bot calls ----------------------------------------------
def fair_prob(price_a: int, price_b: int, *, power: bool = False) -> float:
    """No-vig probability that side A wins, from the two American prices.

    price_a / price_b are the sharp book's moneyline for the two sides of a
    market. Returns fair value in (0, 1) for side A.
    """
    devig = devig_power if power else devig_multiplicative
    fair_a, _ = devig(american_to_prob(price_a), american_to_prob(price_b))
    return fair_a
