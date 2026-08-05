"""Offline self-check for fair_value.py — pure math, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fair_value import american_to_prob, fair_prob, overround


def check() -> None:
    # a book that posts a fair coin with vig: -110 / -110 -> ~0.5238 each
    p = american_to_prob(-110)
    assert abs(p - 0.5238) < 1e-3, p
    assert overround(p, p) > 0.0  # vig present

    fv = fair_prob(-110, -110)
    assert abs(fv - 0.5) < 1e-9, fv  # symmetric -> exactly 50%

    # favourite -136 / dog +124: fair value sits below the raw vig'd implied
    # prob (devig strips the hold off the favourite too)
    raw_fav = american_to_prob(-136)
    fv_fav = fair_prob(-136, 124)
    assert 0.5 < fv_fav < raw_fav, (fv_fav, raw_fav)

    # power method still returns a valid prob and agrees closely near 50/50
    assert abs(fair_prob(-110, -110, power=True) - 0.5) < 1e-6
    print("ok")


if __name__ == "__main__":
    check()
