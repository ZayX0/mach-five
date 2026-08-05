"""Offline self-check for guard.py — pure decision logic, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guard import (DIVERGENCE_MAX, FEED_STALE_SEC, FV_STALE_SEC,
                   LINEUP_SETTLE_POLLS, PRINT_MAX, STOP_BEFORE_PITCH_MIN,
                   TAPE_ONESIDED_MAX, TAPE_WINDOW_SEC, TOXIC_COOLDOWN_SEC,
                   GameGuard, TapeStats)


def check() -> None:
    PITCH = 100_000.0

    # --- lineup gate ---------------------------------------------------
    g = GameGuard(commence_ts=PITCH)
    ok, why = g.decision(PITCH - 7200, 0.5, 0.5, feed_age=1.0)
    assert not ok and why == "awaiting lineups"
    g.on_fv(0.5, PITCH - 7500)         # pre-lineup polls must not count
    g.on_fv(0.5, PITCH - 7440)
    g.on_fv(0.5, PITCH - 7380)
    assert not g.gate_open
    g.on_lineups_confirmed()
    ok, why = g.decision(PITCH - 7200, 0.5, 0.5, 1.0)
    assert not ok and why == "awaiting fv settle"
    g.on_fv(0.52, PITCH - 7200)        # first post-lineup poll: baseline
    g.on_fv(0.53, PITCH - 7140)        # 0.01 move: reset
    for i in range(LINEUP_SETTLE_POLLS):
        g.on_fv(0.531, PITCH - 7080 + 60 * i)   # calm
    assert g.gate_open
    g.on_fv(0.531, PITCH - 3600)       # keep the anchor fresh
    ok, why = g.decision(PITCH - 3600, 0.531, 0.531, 1.0)
    assert ok and why == ""

    # --- hard stops ------------------------------------------------------
    ok, why = g.decision(PITCH - STOP_BEFORE_PITCH_MIN * 60, 0.5, 0.5, 1.0)
    assert not ok and why == "first pitch"
    g.delayed = True
    ok, why = g.decision(PITCH - 3600, 0.5, 0.5, 1.0)
    assert not ok and "delayed" in why
    g.delayed = False

    # --- dead man --------------------------------------------------------
    ok, why = g.decision(PITCH - 3600, 0.5, 0.5, FEED_STALE_SEC + 5)
    assert not ok and "silent" in why

    # --- divergence trips AND starts a cooldown --------------------------
    now = PITCH - 3600
    ok, why = g.decision(now, 0.50, 0.50 + DIVERGENCE_MAX + 0.005, 1.0)
    assert not ok and "divergence" in why
    ok, why = g.decision(now + 10, 0.5, 0.5, 1.0)   # mid healthy again...
    assert not ok and "cooldown" in why             # ...but still cooling
    g.on_fv(0.5, now + TOXIC_COOLDOWN_SEC + 1)      # fresh anchor
    ok, why = g.decision(now + TOXIC_COOLDOWN_SEC + 1, 0.5, 0.5, 1.0)
    assert ok
    ok, _ = g.decision(now + TOXIC_COOLDOWN_SEC + 2, 0.5, None, 1.0)
    assert ok                          # one-sided book (mid None): no signal

    # --- stale anchor: Pinnacle delisted the game -> hold, no cooldown ----
    t0 = now + TOXIC_COOLDOWN_SEC + 2
    ok, why = g.decision(t0 + FV_STALE_SEC + 1, 0.5, 0.5, 1.0)
    assert not ok and "fv anchor stale" in why, why
    g.on_fv(0.5, t0 + FV_STALE_SEC + 9)   # anchor returns...
    ok, _ = g.decision(t0 + FV_STALE_SEC + 9, 0.5, 0.5, 1.0)
    assert ok                             # ...quoting resumes immediately

    # --- explicit toxic mark (the tape path) ------------------------------
    g.mark_toxic(now, "print 9999")
    ok, why = g.decision(now + 1, 0.5, 0.5, 1.0)
    assert not ok and "print 9999" in why

    # --- tape stats --------------------------------------------------------
    t = TapeStats("tokA")
    line = lambda ts, tok, px, sz: {"ts": ts, "token": tok,
                                    "price": px, "size": sz}
    # balanced two-way flow: quiet
    assert t.on_trade(line(0.0, "tokA", 0.5, 1000.0)) is None
    assert t.on_trade(line(1.0, "tokB", 0.5, 1000.0)) is None
    # single huge print
    big = t.on_trade(line(2.0, "tokA", 0.5, (PRINT_MAX / 0.5) + 10))
    assert big and big.startswith("print"), big
    # one-sided grind past the window threshold
    t2 = TapeStats("tokA")
    n, each = 8, (TAPE_ONESIDED_MAX / 8) + 1
    got = None
    for i in range(n):
        got = t2.on_trade(line(float(i), "tokA", 1.0, each))
    assert got and "one-sided" in got, got
    # three prints that would trip the window sum together (3x2500 > 6000),
    # but the first expires out of the window: stays quiet
    each3 = (TAPE_ONESIDED_MAX / 3) + 500     # < PRINT_MAX, 3 in window trip
    assert each3 < PRINT_MAX and 2 * each3 < TAPE_ONESIDED_MAX
    t3 = TapeStats("tokA")
    assert t3.on_trade(line(0.0, "tokA", 1.0, each3)) is None
    assert t3.on_trade(line(TAPE_WINDOW_SEC + 1, "tokA", 1.0, each3)) is None
    assert t3.on_trade(line(TAPE_WINDOW_SEC + 2, "tokA", 1.0, each3)) is None
    print("ok")


if __name__ == "__main__":
    check()
