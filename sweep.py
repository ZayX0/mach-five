"""Sweep — offline calibration of guard.py thresholds and quote parameters.

Where replay.py asks WHEN to quote, sweep.py asks WHERE the guard's knobs
should sit. Two techniques over the same recordings:

1. Event studies (no trading simulated): inside each game's GATED window
   (lineup_gated_start -> first pitch minus STOP_BEFORE_PITCH_MIN), replay
   the tape/fv/book series and measure what followed each would-be trigger:
   - print size buckets -> fv move afterward (PRINT_MAX)
   - one-sided rolling flow grid -> precision/recall vs >=1c fv moves
     (TAPE_WINDOW_SEC x TAPE_ONESIDED_MAX)
   - |fv - book mid| distribution + exceedance predictiveness (DIVERGENCE_MAX)
   - time for fv to finish repricing after a trigger (TOXIC_COOLDOWN_SEC)
2. Paper re-runs (replay.simulate): the lu-gated strategy re-scored under
   candidate HALF_SPREADs and LINEUP_SETTLE_POLLS/EPS grids, plus fill
   markout by minutes-to-pitch (STOP_BEFORE_PITCH_MIN).

Caveats baked into reading the output: the 2026-08-01 tape gap (16:34-22:07
UTC) mutes tape-based rows for that slate; outside the recorder's fast odds
window fv is sampled at 600s, so "fv move within 300s" uses the NEXT poll
at-or-after the horizon (fv_after) and is an upper bound on latency there.

Usage:  python3 sweep.py [recordings]      (no args: recordings/)
Offline check: python3 tests/check_sweep.py
"""
from __future__ import annotations

import statistics
import sys
from bisect import bisect_left
from pathlib import Path

import mach_five
from guard import (DIVERGENCE_MAX, LINEUP_SETTLE_EPS, LINEUP_SETTLE_POLLS,
                   PRINT_MAX, STOP_BEFORE_PITCH_MIN, TAPE_ONESIDED_MAX,
                   TAPE_WINDOW_SEC, TOXIC_COOLDOWN_SEC)
from replay import (expand, fv_at, lineup_complete_ts, lineup_gated_start,
                    load, markout, simulate)

PRINT_BUCKETS = (250.0, 500.0, 1000.0, 1500.0, 3000.0)   # bucket upper edges
FLOW_WINDOWS = (30.0, 60.0, 120.0)
FLOW_THRESHOLDS = (1500.0, 3000.0, 6000.0)
DIVERGENCE_CANDIDATES = (0.010, 0.015, 0.020, 0.030)
HALF_SPREADS = (0.005, 0.006, 0.0075, 0.010)             # 1 tick .. 2 ticks
SETTLE_POLLS = (2, 3, 4, 5)
SETTLE_EPS = (0.001, 0.002, 0.003)
MOVE_EPS = 0.010          # "a real fv move" for precision/recall, price units
LOOKAHEAD_SEC = 300.0     # how far after a trigger we look for the move
REFRACTORY_SEC = 300.0    # min gap between counted firings of one rule
PITCH_BUCKET_MIN = 5      # stop-time table granularity (last 30 min)


# --- shared helpers ----------------------------------------------------------
def fv_after(series: list[tuple[float, float]], ts: float) -> float:
    """First fv at-or-after ts (last fv if the series ends first). The
    forward-looking complement of replay.fv_at — at 600s poll cadence the
    move lands on the NEXT poll, which fv_at would miss."""
    i = bisect_left(series, (ts, -1.0))
    return series[min(i, len(series) - 1)][1]


def gated_start_param(events: list[dict], polls: int, eps: float) -> float | None:
    """lineup_gated_start with the settle parameters as arguments."""
    lu = lineup_complete_ts(events)
    if lu is None:
        return None
    calm, prev = 0, None
    for e in events:
        if e["type"] != "pinnacle" or e["ts"] <= lu:
            continue
        if prev is not None and abs(e["fv"] - prev) < eps:
            calm += 1
            if calm >= polls:
                return e["ts"]
        else:
            calm = 0
        prev = e["fv"]
    return None


def gated_window(meta: dict, events: list[dict]) -> tuple[float, float] | None:
    """(start, end) of the window the live loop would quote, or None."""
    st = lineup_gated_start(events)
    if st is None:
        return None
    end = meta["commence_ts"] - STOP_BEFORE_PITCH_MIN * 60.0
    return (st, end) if st < end else None


def signed_flow_firings(trades: list[tuple[float, float]], window: float,
                        threshold: float) -> list[float]:
    """Timestamps where |rolling signed notional over `window`s| first
    crosses `threshold`, with a REFRACTORY_SEC gap between firings.
    `trades` = [(ts, signed notional)] sorted by ts."""
    out: list[float] = []
    buf: list[tuple[float, float]] = []
    last_fire = -1e18
    for ts, signed in trades:
        buf.append((ts, signed))
        while buf and buf[0][0] < ts - window:
            buf.pop(0)
        if (abs(sum(s for _, s in buf)) > threshold
                and ts - last_fire >= REFRACTORY_SEC):
            out.append(ts)
            last_fire = ts
    return out


def move_events(series: list[tuple[float, float]], start: float,
                end: float) -> list[float]:
    """Timestamps of fv polls inside [start, end] that moved >= MOVE_EPS
    from the previous poll — the 'real repricings' a tape rule should catch."""
    out = []
    prev = None
    for ts, fv in series:
        if prev is not None and start <= ts <= end and abs(fv - prev) >= MOVE_EPS:
            out.append(ts)
        prev = fv
    return out


# --- 1. PRINT_MAX ------------------------------------------------------------
def print_size_study(games) -> str:
    agg: dict[float, list[float]] = {e: [] for e in PRINT_BUCKETS + (float("inf"),)}
    for meta, events in games:
        win = gated_window(meta, events)
        if win is None:
            continue
        series = [(e["ts"], e["fv"]) for e in events if e["type"] == "pinnacle"]
        for e in events:
            if e["type"] != "trade" or not win[0] <= e["ts"] <= win[1]:
                continue
            notional = e["price"] * e["size"]
            move = abs(fv_after(series, e["ts"] + LOOKAHEAD_SEC)
                       - fv_at(series, e["ts"]))
            edge = next(b for b in agg if notional <= b)
            agg[edge].append(move)
    rows = [f"  print $ bucket    prints   mean |fv move| next {int(LOOKAHEAD_SEC)}s"
            f"   frac >= {MOVE_EPS * 100:.0f}c",
            "  --------------    ------   ------------------------   -----------"]
    lo = 0.0
    for edge in sorted(agg):
        moves = agg[edge]
        hi = "inf" if edge == float("inf") else f"{edge:.0f}"
        mark = "  <- PRINT_MAX" if lo < PRINT_MAX <= (edge if edge != float("inf") else 1e18) else ""
        if moves:
            frac = sum(1 for m in moves if m >= MOVE_EPS) / len(moves)
            rows.append(f"  {lo:6.0f}-{hi:>6}    {len(moves):6d}   "
                        f"{100 * statistics.mean(moves):22.2f}c   {frac:11.2%}{mark}")
        else:
            rows.append(f"  {lo:6.0f}-{hi:>6}    {0:6d}   {'-':>23}   {'-':>11}{mark}")
        lo = edge
    return "\n".join(rows)


# --- 2. TAPE_WINDOW_SEC x TAPE_ONESIDED_MAX ----------------------------------
def flow_grid_study(games) -> str:
    cells = {(w, t): [0, 0] for w in FLOW_WINDOWS for t in FLOW_THRESHOLDS}
    caught = {(w, t): 0 for w in FLOW_WINDOWS for t in FLOW_THRESHOLDS}
    total_moves = 0
    for meta, events in games:
        win = gated_window(meta, events)
        if win is None:
            continue
        series = [(e["ts"], e["fv"]) for e in events if e["type"] == "pinnacle"]
        token_a = meta["token_a"]
        trades = [(e["ts"], (-1 if e["token"] == token_a else 1)
                   * e["price"] * e["size"])
                  for e in events
                  if e["type"] == "trade" and win[0] <= e["ts"] <= win[1]]
        moves = move_events(series, win[0], win[1])
        total_moves += len(moves)
        for (w, t), cell in cells.items():
            fires = signed_flow_firings(trades, w, t)
            cell[0] += len(fires)
            cell[1] += sum(1 for f in fires
                           if abs(fv_after(series, f + LOOKAHEAD_SEC)
                                  - fv_at(series, f)) >= MOVE_EPS)
            caught[(w, t)] += sum(1 for m in moves
                                  if any(0 <= m - f <= LOOKAHEAD_SEC for f in fires))
    rows = [f"  window x threshold   firings   precision (>= {MOVE_EPS * 100:.0f}c move follows)"
            f"   recall ({total_moves} moves)",
            "  ------------------   -------   -------------------------------   ------"]
    for (w, t), (n, hit) in sorted(cells.items()):
        mark = "  <- current" if (w, t) == (TAPE_WINDOW_SEC, TAPE_ONESIDED_MAX) else ""
        prec = f"{hit / n:.2%}" if n else "-"
        rec = f"{caught[(w, t)] / total_moves:.2%}" if total_moves else "-"
        rows.append(f"  {w:4.0f}s x ${t:6.0f}     {n:7d}   {prec:>31}   {rec:>6}{mark}")
    return "\n".join(rows)


# --- 3. DIVERGENCE_MAX -------------------------------------------------------
def divergence_study(games) -> str:
    divs: list[float] = []
    exceed = {d: [] for d in DIVERGENCE_CANDIDATES}
    for meta, events in games:
        win = gated_window(meta, events)
        if win is None:
            continue
        series = [(e["ts"], e["fv"]) for e in events if e["type"] == "pinnacle"]
        last_fire = {d: -1e18 for d in DIVERGENCE_CANDIDATES}
        for e in events:
            if (e["type"] != "book" or e.get("side") != "A"
                    or not win[0] <= e["ts"] <= win[1]
                    or e.get("best_bid") is None or e.get("best_ask") is None):
                continue
            mid = (e["best_bid"] + e["best_ask"]) / 2.0
            d = abs(fv_at(series, e["ts"]) - mid)
            divs.append(d)
            for cand in DIVERGENCE_CANDIDATES:
                if d > cand and e["ts"] - last_fire[cand] >= REFRACTORY_SEC:
                    last_fire[cand] = e["ts"]
                    exceed[cand].append(
                        abs(fv_after(series, e["ts"] + LOOKAHEAD_SEC)
                            - fv_at(series, e["ts"])))
    if not divs:
        return "  (no gated book snapshots)"
    divs.sort()
    pct = {p: divs[min(len(divs) - 1, int(p / 100 * len(divs)))] for p in (50, 90, 99)}
    rows = [f"  |fv - mid| in gated windows ({len(divs)} snapshots): "
            f"p50 {100 * pct[50]:.2f}c  p90 {100 * pct[90]:.2f}c  "
            f"p99 {100 * pct[99]:.2f}c  max {100 * divs[-1]:.2f}c",
            "",
            f"  threshold   firings   mean |fv move| next {int(LOOKAHEAD_SEC)}s"
            f"   frac >= {MOVE_EPS * 100:.0f}c",
            "  ---------   -------   ------------------------   -----------"]
    for cand in DIVERGENCE_CANDIDATES:
        ms = exceed[cand]
        mark = "  <- DIVERGENCE_MAX" if abs(cand - DIVERGENCE_MAX) < 1e-9 else ""
        if ms:
            frac = sum(1 for m in ms if m >= MOVE_EPS) / len(ms)
            rows.append(f"  {100 * cand:7.1f}c   {len(ms):7d}   "
                        f"{100 * statistics.mean(ms):22.2f}c   {frac:11.2%}{mark}")
        else:
            rows.append(f"  {100 * cand:7.1f}c   {0:7d}   {'-':>23}   {'-':>11}{mark}")
    return "\n".join(rows)


# --- 4. TOXIC_COOLDOWN_SEC ---------------------------------------------------
def reprice_duration_study(games) -> str:
    durations: list[float] = []
    for meta, events in games:
        win = gated_window(meta, events)
        if win is None:
            continue
        series = [(e["ts"], e["fv"]) for e in events if e["type"] == "pinnacle"]
        token_a = meta["token_a"]
        trades = [(e["ts"], (-1 if e["token"] == token_a else 1)
                   * e["price"] * e["size"])
                  for e in events
                  if e["type"] == "trade" and win[0] <= e["ts"] <= win[1]]
        fires = signed_flow_firings(trades, TAPE_WINDOW_SEC, TAPE_ONESIDED_MAX)
        fires += [ts for ts, s in trades if abs(s) > PRINT_MAX]
        for f in sorted(set(fires)):
            moving = [ts for ts, fv in series
                      if f < ts <= f + 600.0
                      and abs(fv - fv_at(series, ts - 1)) >= LINEUP_SETTLE_EPS]
            durations.append((moving[-1] - f) if moving else 0.0)
    if not durations:
        return "  (no gated tape triggers)"
    med = statistics.median(durations)
    mean = statistics.mean(durations)
    frac = sum(1 for d in durations if d <= TOXIC_COOLDOWN_SEC) / len(durations)
    return (f"  {len(durations)} trigger(s): fv finished repricing after "
            f"median {med:.0f}s, mean {mean:.0f}s "
            f"(within current {TOXIC_COOLDOWN_SEC:.0f}s cooldown: {frac:.0%})")


# --- 5. HALF_SPREAD ----------------------------------------------------------
def _score_lu_gated(games, queue: bool, start_for=lineup_gated_start):
    """(fills, filled $, mark P&L, markout300, markout1800, skips)."""
    fills = filled = pnl = mo3 = mo18 = 0.0
    skips = 0
    for meta, events in games:
        st = start_for(events)
        if st is None:
            skips += 1
            continue
        pb, series = simulate(meta, events, 0.0, start_ts=st, queue=queue)
        if not series:
            continue
        pnl += pb.pnl_mark(fv_at(series, meta["commence_ts"]))
        fills += len(pb.fills)
        filled += sum(f.shares * f.price for f in pb.fills)
        mo3 += sum(markout(f, series, 300.0) for f in pb.fills)
        mo18 += sum(markout(f, series, 1800.0) for f in pb.fills)
    return fills, filled, pnl, mo3, mo18, skips


def half_spread_study(games) -> str:
    rows = ["  half-spread   queue   fills   filled $   mark P&L $"
            "   markout 300s $   markout 1800s $",
            "  -----------   -----   -----   --------   ----------"
            "   --------------   ---------------"]
    saved = mach_five.HALF_SPREAD
    try:
        for hs in HALF_SPREADS:
            mach_five.HALF_SPREAD = hs
            for queue, label in ((False, "opt"), (True, "pess")):
                f, filled, pnl, mo3, mo18, _ = _score_lu_gated(games, queue)
                mark = "  <- current" if abs(hs - saved) < 1e-12 and queue else ""
                rows.append(f"  {hs:11.4f}   {label:>5}   {int(f):5d}   "
                            f"{filled:8.0f}   {pnl:10.2f}   {mo3:14.2f}   "
                            f"{mo18:15.2f}{mark}")
    finally:
        mach_five.HALF_SPREAD = saved
    return "\n".join(rows)


# --- 6. LINEUP_SETTLE grid ---------------------------------------------------
def settle_grid_study(games) -> str:
    rows = ["  polls x eps    games   avg window   mark P&L $   markout 300s $"
            "   post-gate 15m drift",
            "  -----------    -----   ----------   ----------   --------------"
            "   -------------------"]
    for polls in SETTLE_POLLS:
        for eps in SETTLE_EPS:
            starts, drifts = [], []
            for meta, events in games:
                st = gated_start_param(events, polls, eps)
                if st is None:
                    continue
                end = meta["commence_ts"] - STOP_BEFORE_PITCH_MIN * 60.0
                if st >= end:
                    continue
                series = [(e["ts"], e["fv"]) for e in events
                          if e["type"] == "pinnacle"]
                starts.append((meta, events, st, (end - st) / 60.0))
                drifts.append(abs(fv_after(series, st + 900.0)
                                  - fv_at(series, st)))
            f, filled, pnl, mo3, _, _ = _score_lu_gated(
                games, queue=False,
                start_for=lambda ev, p=polls, e=eps: gated_start_param(ev, p, e))
            n = len(starts)
            win = statistics.mean(w for *_, w in starts) if starts else float("nan")
            drift = 100 * statistics.mean(drifts) if drifts else float("nan")
            mark = ("  <- current" if polls == LINEUP_SETTLE_POLLS
                    and abs(eps - LINEUP_SETTLE_EPS) < 1e-12 else "")
            rows.append(f"  {polls} x {eps:.3f}      {n:5d}   {win:8.0f}m   "
                        f"{pnl:10.2f}   {mo3:14.2f}   {drift:18.2f}c{mark}")
    return "\n".join(rows)


# --- 7. STOP_BEFORE_PITCH_MIN ------------------------------------------------
def stop_time_study(games) -> str:
    agg: dict[int, list[float]] = {}
    for meta, events in games:
        st = lineup_gated_start(events)
        if st is None:
            continue
        pb, series = simulate(meta, events, 0.0, start_ts=st)
        for f in pb.fills:
            to_pitch = (meta["commence_ts"] - f.ts) / 60.0
            b = (int(to_pitch // PITCH_BUCKET_MIN) * PITCH_BUCKET_MIN
                 if to_pitch < 30 else 30)
            cell = agg.setdefault(b, [0.0, 0.0, 0.0])
            cell[0] += 1
            cell[1] += f.shares
            cell[2] += markout(f, series, 1800.0)
    rows = ["  mins-to-pitch   fills   markout 1800s ¢/share   markout $",
            "  -------------   -----   ---------------------   ---------"]
    for k in sorted(agg, reverse=True):
        n, shares, mo = agg[k]
        cents = 100.0 * mo / shares if shares else float("nan")
        label = f"{k}+" if k == 30 else f"{k}-{k + PITCH_BUCKET_MIN}"
        rows.append(f"  {label:>13}   {int(n):5d}   {cents:21.2f}   {mo:9.2f}")
    return "\n".join(rows)


def main(argv: list[str]) -> None:
    games = [g for g in (load(p) for p in expand(argv)) if g is not None]
    gated = sum(1 for m, e in games if gated_window(m, e) is not None)
    print(f"{len(games)} game(s), {gated} with a gated quoting window\n")
    print(f"1. PRINT_MAX (current {PRINT_MAX:.0f}) — what followed each print")
    print(print_size_study(games) + "\n")
    print("2. TAPE_WINDOW_SEC x TAPE_ONESIDED_MAX — one-sided flow grid")
    print(flow_grid_study(games) + "\n")
    print("3. DIVERGENCE_MAX — |fv - mid| distribution and predictiveness")
    print(divergence_study(games) + "\n")
    print("4. TOXIC_COOLDOWN_SEC — how long fv keeps moving after a trigger")
    print(reprice_duration_study(games) + "\n")
    print("5. HALF_SPREAD — lu-gated re-runs (opt = front of queue, pess = back)")
    print(half_spread_study(games) + "\n")
    print("6. LINEUP_SETTLE_POLLS x EPS — gate timing grid (optimistic queue)")
    print(settle_grid_study(games) + "\n")
    print(f"7. STOP_BEFORE_PITCH_MIN (current {STOP_BEFORE_PITCH_MIN:.0f}m) — "
          "lu-gated fill markout by minutes-to-pitch")
    print(stop_time_study(games))


if __name__ == "__main__":
    main(sys.argv[1:] or [str(Path(__file__).parent / "recordings")])
