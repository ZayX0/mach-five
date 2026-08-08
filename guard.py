"""Quote gate + toxic-flow guard — WHEN the quoting loop may rest orders.

Pure decision logic, no I/O: mach_five.run() feeds it fv polls, lineup
confirmations, tape prints, book mids, and feed health; it answers
"quote or pull, and why". Everything is per-game except feed staleness.

The empirical basis (replay.py, recordings from 2026-08-02): fills in the
hour before lineup completion ran ~-1.6c/share markout (picked off by flow
that beat Pinnacle's reprice); fills after both lineups were confirmed AND
fv settled ran +0.2..+0.8c/share at both queue bounds. Hence the gate:

  QUOTE only when  both lineups confirmed
               AND fv settled since (LINEUP_SETTLE_POLLS calm polls)
               AND not within STOP_BEFORE_PITCH_MIN of scheduled start
               AND no toxic condition is active (below)

Toxic conditions (each pulls quotes and starts TOXIC_COOLDOWN_SEC):
  - tape burst: one-sided signed notional over TAPE_WINDOW_SEC beyond
    TAPE_ONESIDED_MAX, or a single print beyond PRINT_MAX. Units are real
    DOLLARS (calibrated 2026-08-02: size = contracts, price*size = $).
    SIDE-AWARE (2026-08-07, fix 3): a burst is directional — selling A
    endangers only OUR resting A bid (the falling side); the B bid drifts
    safer and keeps its queue spot. TapeStats names the threatened side,
    mark_toxic cools only it, and decision() stays green while the other
    side quotes (step() zeroes the blocked side via `blocked`). Session
    2026-08-06T1955: one $8.2k print pulled BOTH sides and burned 46min
    of queue age on each.
    VALUES re-based by sweep.py 2026-08-04 (41 games): print size was
    barely predictive inside gated windows (>=1c-move rate ~4% at any
    size vs ~2% base) and the old 60s x $3000 flow rule caught 0 of 13
    real repricings while firing ~2x/game — the gate itself removes the
    toxicity these rules hunt. Raised to tail-insurance levels; re-sweep
    as tape accumulates
  - divergence: |Pinnacle fv - venue mid| > DIVERGENCE_MAX (someone knows
    something our anchor doesn't)
  - dead man: the trade WS hasn't been heard from in FEED_STALE_SEC —
    "no data" and "news we can't see" are indistinguishable, so pull.
  - stale anchor: no Pinnacle poll for FV_STALE_SEC — Pinnacle DELISTS a
    game's pre-game moneyline when something is wrong with it (seen live
    2026-08-03, Dodgers-Cubs pulled ~70min before pitch), and quoting
    against a frozen fv is quoting blind. Not a cooldown: the hold lasts
    exactly as long as the anchor is missing.

Offline check: python3 tests/check_guard.py
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

LINEUP_SETTLE_POLLS = 3    # consecutive calm fv polls after lineups post...
LINEUP_SETTLE_EPS = 0.002  # ...each moving less than this = digested
STOP_BEFORE_PITCH_MIN = 2.0   # pull ahead of the venue's start auto-cancel
DIVERGENCE_MAX = 0.02      # |fv - mid| circuit breaker, price units
TAPE_WINDOW_SEC = 120.0    # rolling window for one-sided flow
TAPE_ONESIDED_MAX = 6000.0  # |signed notional| in window that means informed
PRINT_MAX = 5000.0         # single-print notional that means informed
TOXIC_COOLDOWN_SEC = 180.0  # stay pulled this long after any toxic trigger
FEED_STALE_SEC = 30.0      # trade-WS silence that blinds the guard
FV_STALE_SEC = 120.0       # 2 missed 60s odds polls = anchor is gone


class TapeStats:
    """Rolling one-sided-flow detector for one market's A-frame tape."""

    def __init__(self, token_a: str):
        self.token_a = token_a
        self.window: deque[tuple[float, float]] = deque()  # (ts, signed $)

    def on_trade(self, line: dict) -> tuple[str, str] | None:
        """Feed one recorder-schema trade line; (reason, threatened side)
        when the tape turned toxic, else None. Sign: selling A = negative
        pressure on A, selling B (== buying A) = positive. trade_line
        normalizes every print to a SELL of `token`, so the sold token IS
        the side whose bid the flow endangers — the complementary side's
        bid only drifts further behind and is safe to leave resting."""
        ts, notional = line["ts"], line["price"] * line["size"]
        sold_a = line["token"] == self.token_a
        signed = -notional if sold_a else notional
        self.window.append((ts, signed))
        while self.window and self.window[0][0] < ts - TAPE_WINDOW_SEC:
            self.window.popleft()
        if notional > PRINT_MAX:
            return f"print {notional:.0f}", ("A" if sold_a else "B")
        flow = sum(s for _, s in self.window)
        if abs(flow) > TAPE_ONESIDED_MAX:
            return (f"one-sided flow {flow:+.0f}/{TAPE_WINDOW_SEC:.0f}s",
                    "A" if flow < 0 else "B")
        return None


@dataclass
class GameGuard:
    """Per-game quote/pull decision state."""
    commence_ts: float
    lineups_done: bool = False
    calm: int = 0
    prev_fv: float | None = None
    last_fv_ts: float = 0.0  # epoch of the last Pinnacle poll (0 = never)
    gate_open: bool = False
    toxic_until: dict = field(default_factory=lambda: {"A": 0.0, "B": 0.0})
    toxic_reason: dict = field(default_factory=lambda: {"A": "", "B": ""})
    delayed: bool = False  # MLB detailedState Delayed/Postponed/Suspended

    def on_lineups_confirmed(self) -> None:
        """Both sides' lineups observed. Settle counting starts fresh —
        only post-lineup fv polls prove Pinnacle digested the news."""
        if not self.lineups_done:
            self.lineups_done = True
            self.calm, self.prev_fv = 0, None

    def on_fv(self, fv: float, now: float) -> None:
        """Feed every Pinnacle poll. Every poll refreshes the anchor's
        freshness; settle counting only runs post-lineup, pre-gate."""
        self.last_fv_ts = now
        if not self.lineups_done or self.gate_open:
            return
        if self.prev_fv is not None and abs(fv - self.prev_fv) < LINEUP_SETTLE_EPS:
            self.calm += 1
            if self.calm >= LINEUP_SETTLE_POLLS:
                self.gate_open = True
        else:
            self.calm = 0
        self.prev_fv = fv

    def mark_toxic(self, now: float, reason: str,
                   side: str | None = None) -> None:
        """Start a cooldown: side None = both (direction-less triggers,
        e.g. divergence), 'A'/'B' = a tape burst threatening only that
        side's bid — the other side keeps quoting and its queue spot."""
        for s in ("A", "B") if side is None else (side,):
            self.toxic_until[s] = now + TOXIC_COOLDOWN_SEC
            self.toxic_reason[s] = reason

    def side_blocked(self, side: str, now: float) -> bool:
        """True while `side` sits inside a toxic cooldown. decision() only
        goes red when BOTH sides are; a half-blocked game keeps quoting
        and the caller zeroes the blocked side (mach_five.step `blocked`)."""
        return now < self.toxic_until[side]

    def decision(self, now: float, fv: float, mid: float | None,
                 feed_age: float) -> tuple[bool, str]:
        """(quote?, reason-if-not). Order matters: hard stops first."""
        if now >= self.commence_ts - STOP_BEFORE_PITCH_MIN * 60.0:
            return False, "first pitch"
        if self.delayed:
            return False, "game delayed/postponed"
        if not self.gate_open:
            return False, ("awaiting lineups" if not self.lineups_done
                           else "awaiting fv settle")
        if self.side_blocked("A", now) and self.side_blocked("B", now):
            reasons = " + ".join(sorted(set(self.toxic_reason.values())))
            return False, f"cooldown ({reasons})"
        if now - self.last_fv_ts > FV_STALE_SEC:
            return False, f"fv anchor stale {now - self.last_fv_ts:.0f}s"
        if feed_age > FEED_STALE_SEC:
            return False, f"tape feed silent {feed_age:.0f}s"
        if mid is not None and abs(fv - mid) > DIVERGENCE_MAX:
            self.mark_toxic(now, f"fv-mid divergence {abs(fv - mid):.3f}")
            return False, f"cooldown ({self.toxic_reason['A']})"
        return True, ""
