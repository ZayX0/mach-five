"""Offline self-check for sweep.py — synthetic events, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guard import LINEUP_SETTLE_EPS, LINEUP_SETTLE_POLLS
from replay import lineup_gated_start
from sweep import (fv_after, gated_start_param, move_events,
                   signed_flow_firings)


def check() -> None:
    # fv_after: forward-looking lookup (vs replay.fv_at's backward one)
    series = [(100.0, 0.50), (200.0, 0.52), (300.0, 0.55)]
    assert fv_after(series, 150.0) == 0.52     # next poll at-or-after
    assert fv_after(series, 200.0) == 0.52     # exact hit
    assert fv_after(series, 999.0) == 0.55     # past the end: last
    assert fv_after(series, 0.0) == 0.50

    # gated_start_param at the live constants == replay.lineup_gated_start
    ev = [{"type": "lineup", "ts": 10.0, "side": "A"},
          {"type": "lineup", "ts": 20.0, "side": "B"},
          {"type": "pinnacle", "ts": 30.0, "fv": 0.500},
          {"type": "pinnacle", "ts": 40.0, "fv": 0.501},
          {"type": "pinnacle", "ts": 50.0, "fv": 0.5015},
          {"type": "pinnacle", "ts": 60.0, "fv": 0.5012}]
    assert (gated_start_param(ev, LINEUP_SETTLE_POLLS, LINEUP_SETTLE_EPS)
            == lineup_gated_start(ev) == 60.0)
    assert gated_start_param(ev, 2, LINEUP_SETTLE_EPS) == 50.0  # fewer polls
    assert gated_start_param(ev, 5, LINEUP_SETTLE_EPS) is None  # never settles
    assert gated_start_param([], 3, 0.002) is None              # no lineups

    # signed flow firings: crossing counts once per refractory period
    trades = [(0.0, 1000.0), (10.0, 1000.0), (20.0, 1200.0),  # crosses 3000
              (30.0, 1000.0),                                  # refractory
              (400.0, 2000.0), (410.0, 1500.0)]                # fires again
    fires = signed_flow_firings(trades, window=60.0, threshold=3000.0)
    assert fires == [20.0, 410.0], fires
    # balanced flow never fires
    assert signed_flow_firings([(0.0, 2000.0), (1.0, -2000.0),
                                (2.0, 2000.0), (3.0, -2000.0)],
                               60.0, 3000.0) == []
    # old trades expire out of the window
    assert signed_flow_firings([(0.0, 2000.0), (100.0, 2000.0)],
                               60.0, 3000.0) == []

    # move_events: >= MOVE_EPS steps inside the window only
    series = [(10.0, 0.50), (20.0, 0.52), (30.0, 0.521), (40.0, 0.50)]
    assert move_events(series, 15.0, 45.0) == [20.0, 40.0]
    assert move_events(series, 25.0, 45.0) == [40.0]   # 20.0 outside window
    print("ok")


if __name__ == "__main__":
    check()
