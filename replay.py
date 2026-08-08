"""Replay — offline analysis of recorder.py output. Answers "when should
quoting turn on" two ways:

1. Market quality by minutes-to-first-pitch: Pinnacle fv drift, Polymarket
   spread, and traded volume per bucket. Shows where fv settles (lineups in),
   where the book tightens, and where flow arrives.
2. Paper replay: drives mach_five.quotes through a PaperBook against the
   recorded tape, swept over quote-start times, scoring each run by mark-to-
   fair P&L and fill markout (fv drift after each fill — the adverse-
   selection detector; negative markout = we were picked off). The sweep's
   final `lu-gated` row scores the lineup-gated strategy on the same tape:
   quote only after both lineups are confirmed AND Pinnacle has digested
   them (see lineup_gated_start); games that never get there are skipped.
3. Requote-policy comparison: mach_five.step cancel-replaces every tick,
   which surrenders queue priority each time. The extra pessimistic-queue
   tables score KEEP-IF-UNCHANGED (cancel-replace a side only when its
   tick-snapped price moves) against the same tape — queue priority only
   exists in the pessimistic fill model, so that's where the policies
   separate; judge by fills AND markout (the marginal fills land while our
   quote sat still — check they aren't adversely selected), with the
   `posts` column as the churn/venue-load proxy.
4. Touch-pricing comparison (fix-2 validation, sessions 2026-08-06): the
   fv-derived bid usually snaps to one tick BEHIND a five/six-figure
   displayed level, where a pilot-sized order never reaches the front.
   The touch table re-runs the live policy with bids lifted to the touch
   (or a tick inside) via mach_five.touch_bid whenever that keeps a
   minimum edge vs skew-adjusted fair. Verdict 2026-08-07: join 0.3c is
   wired into live step(); the table stays as the re-validation harness
   (its `cap only` row is the honest no-join baseline).

Usage:  python3 replay.py [recordings/*.jsonl]   (no args: recordings/)
Offline check: python3 tests/check_replay.py
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from bisect import bisect_right
from pathlib import Path

from guard import LINEUP_SETTLE_EPS, LINEUP_SETTLE_POLLS

import mach_five
from mach_five import HALF_SPREAD, RESIZE_FRAC, keep_quote, quotes, touch_bid
from paper_book import PaperBook
from us_orders import TICK, snap_bid  # noqa: F401  (TICK re-exported for tests)

# Paper tables always run at RESEARCH sizing, whatever the live constants
# say — mach_five is pilot-sized (40/100 as of 2026-08-04), and inheriting
# that would shrink every table ~50x and break day-over-day comparison.
mach_five.BASE_SIZE, mach_five.MAX_INVENTORY = 2000.0, 5000.0

BUCKET_MIN = 30                              # stats bucket width
HORIZONS = (60.0, 300.0, 1800.0)             # markout horizons, seconds
SWEEP_FROM_MIN = (360, 240, 180, 120, 90, 60, 30)   # quote-start sweep
QUOTE_UNTIL_MIN = 0                          # stop quoting at first pitch
REPLAY_LATENCY_SEC = 2.0                     # order-live delay in the fill sim
TOUCH_EDGES = (0.004, 0.003, 0.002)          # min edge kept when lifting a
                                             # bid to the touch (touch table)
# RESIZE_FRAC (size-drift tolerance) comes from mach_five with keep_quote —
# the live loop and the replay MUST agree on what "unchanged" means
# lineup-gated start parameters come from guard.py — the live loop and the
# replay MUST agree on what "Pinnacle digested the lineups" means


# --- loading ----------------------------------------------------------------
def load(path: str) -> tuple[dict, list[dict]] | None:
    """(meta, events sorted by ts) for one recording, or None if unusable.
    Reads .jsonl and .jsonl.gz (closed slates are archived gzipped)."""
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt") as fh:
            text = fh.read()
    else:
        text = Path(path).read_text()
    meta, events = None, []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "meta":
            meta = obj
        elif "ts" in obj:
            events.append(obj)
    if meta is None or not events:
        return None
    events.sort(key=lambda e: e["ts"])
    return meta, events


def bucket(ts: float, commence: float) -> int:
    """Lower edge (minutes relative to first pitch) of the ts's bucket."""
    minutes = (ts - commence) / 60.0
    return int(math.floor(minutes / BUCKET_MIN) * BUCKET_MIN)


# --- 1. market quality ------------------------------------------------------
def market_stats(meta: dict, events: list[dict], agg: dict | None = None) -> dict:
    """bucket -> accumulators; pass `agg` to merge several games."""
    out = agg if agg is not None else {}
    commence = meta["commence_ts"]
    last_fv = None
    for e in events:
        if e["type"] not in ("pinnacle", "book", "trade"):
            continue
        b = out.setdefault(bucket(e["ts"], commence), {
            "fv_move": 0.0, "fv_n": 0, "spread": 0.0, "spread_n": 0,
            "vol": 0.0, "trades": 0})
        if e["type"] == "pinnacle":
            if last_fv is not None:
                b["fv_move"] += abs(e["fv"] - last_fv)
                b["fv_n"] += 1
            last_fv = e["fv"]
        elif e["type"] == "book" and e.get("side") == "A":
            if e.get("best_bid") is not None and e.get("best_ask") is not None:
                b["spread"] += e["best_ask"] - e["best_bid"]
                b["spread_n"] += 1
        elif e["type"] == "trade":
            b["vol"] += e["price"] * e["size"]
            b["trades"] += 1
    return out


# --- 2. paper replay --------------------------------------------------------
def _level_qty(bids: list, price: float) -> float:
    """Displayed shares at exactly `price` in a [[price, qty], ...] ladder."""
    return sum(q for p, q in bids if abs(p - price) < 1e-6)


def _keep(pb: PaperBook, side: str, price: float, size: float,
          resize_frac: float | None) -> bool:
    """Keep-if-unchanged on the paper book: hold `side`'s resting order (and
    its partially-eaten queue spot) when the policy says so. The decision
    itself is mach_five.keep_quote — shared so backtest and live agree."""
    o = next((o for o in pb.orders if o.side == side), None)
    return o is not None and keep_quote(o.price, o.dollars, price, size,
                                        resize_frac)


def simulate(
    meta: dict, events: list[dict],
    quote_from_min: float, quote_until_min: float = QUOTE_UNTIL_MIN,
    start_ts: float | None = None, queue: bool = False,
    reprice_only: bool = False, resize_frac: float | None = None,
    touch_edge: float | None = None, improve: bool = False,
) -> tuple[PaperBook, list[tuple[float, float]]]:
    """Replay one game, quoting from `quote_from_min` before first pitch to
    `quote_until_min` before. `start_ts` (absolute epoch) overrides the
    clock-based start — the lineup-gated strategy. Returns (book, fv series).

    queue=False: optimistic fills (front of queue, raw quote prices).
    queue=True: pessimistic — bids snap DOWN to the venue tick grid and
    join behind everything displayed at that level in the latest recorded
    book snapshot (no snapshot yet -> queue 0). Truth is in between.

    reprice_only=True: keep-if-unchanged requote policy (see _keep) instead
    of mach_five.step's cancel-replace-every-tick. A kept order retains its
    latency clock and its eaten-down queue_ahead; a replaced side rejoins
    the back of the displayed queue. Prices are snapped even when
    queue=False — the venue grid is what defines "the price moved".

    touch_edge (a min edge in price units, e.g. 0.004): touch pricing —
    each bid runs through mach_five.touch_bid against the latest recorded
    top-of-book (lift to the displayed touch, or one tick inside it when
    `improve`, whenever that still clears touch_edge below skew-adjusted
    fair; always capped one tick below the ask, the live step() cap).
    None (default) leaves every historical table byte-identical."""
    commence = meta["commence_ts"]
    start = commence - quote_from_min * 60.0 if start_ts is None else start_ts
    end = commence - quote_until_min * 60.0
    pb = PaperBook(meta["token_a"], meta["token_b"], latency=REPLAY_LATENCY_SEC)
    fv_series: list[tuple[float, float]] = []
    books: dict[str, list] = {"A": [], "B": []}   # latest displayed bids
    tops: dict[str, tuple] = {"A": (None, None), "B": (None, None)}
    for e in events:
        ts = e["ts"]
        if ts > end and pb.orders:          # window over: pull quotes
            pb.cancel_all()
        if e["type"] == "book" and e.get("side") in books:
            books[e["side"]] = e.get("bids") or []
            tops[e["side"]] = (e.get("best_bid"), e.get("best_ask"))
        elif e["type"] == "pinnacle":
            fv = e["fv"]
            fv_series.append((ts, fv))
            if start <= ts <= end:          # requote per policy
                (pa, sa), (pb_in_a, sb) = quotes(fv, pb.inventory_dollars(fv))
                pb_price = 1.0 - pb_in_a
                if touch_edge is not None:  # skew-adjusted fair = desired
                    pa = touch_bid(         # (unsnapped) + HALF_SPREAD
                        pa, pa + HALF_SPREAD, *tops["A"], touch_edge, improve)
                    pb_price = touch_bid(
                        pb_price, pb_price + HALF_SPREAD, *tops["B"],
                        touch_edge, improve)
                if queue or reprice_only:
                    pa, pb_price = snap_bid(pa), snap_bid(pb_price)
                for side, price, size in (("A", pa, sa), ("B", pb_price, sb)):
                    if reprice_only and _keep(pb, side, price, size,
                                              resize_frac):
                        continue            # hold the queue spot
                    pb.cancel_side(side)    # cancel-replace, as mach_five.step
                    pb.post(side, price, size, ts,
                            _level_qty(books[side], price) if queue else 0.0)
        elif e["type"] == "trade":
            pb.on_trade(ts, e["token"], e["taker_side"], e["price"], e["size"])
    return pb, fv_series


def lineup_complete_ts(events: list[dict]) -> float | None:
    """When BOTH sides' confirmed lineups had been observed (recorder
    `lineup` lines), or None if the recording never saw both."""
    seen: set = set()
    for e in events:
        if e.get("type") == "lineup":
            seen.add(e.get("side"))
            if seen == {"A", "B"}:
                return e["ts"]
    return None


def lineup_gated_start(events: list[dict]) -> float | None:
    """When the lineup-gated strategy may start quoting: both lineups
    observed AND Pinnacle has digested them — LINEUP_SETTLE_POLLS
    consecutive fv polls after completion, each moving < LINEUP_SETTLE_EPS.
    None when the recording never gets there (no lineups seen, or fv never
    settles): that game is skipped, not quoted."""
    lu = lineup_complete_ts(events)
    if lu is None:
        return None
    calm, prev = 0, None
    for e in events:                      # events are sorted by ts
        if e["type"] != "pinnacle" or e["ts"] <= lu:
            continue
        if prev is not None and abs(e["fv"] - prev) < LINEUP_SETTLE_EPS:
            calm += 1
            if calm >= LINEUP_SETTLE_POLLS:
                return e["ts"]
        else:
            calm = 0
        prev = e["fv"]
    return None


def fv_at(series: list[tuple[float, float]], ts: float) -> float:
    """Last fv at or before ts (first fv if ts precedes the series)."""
    i = bisect_right(series, (ts, float("inf")))
    return series[max(0, i - 1)][1]


def markout(fill, series: list[tuple[float, float]], horizon: float) -> float:
    """$ edge of one fill marked at fair `horizon` seconds later.
    Positive = we bought below where fair settled; negative = picked off."""
    fv = fv_at(series, fill.ts + horizon)
    fair = fv if fill.side == "A" else 1.0 - fv
    return (fair - fill.price) * fill.shares


# --- report -----------------------------------------------------------------
def _fmt_stats(agg: dict) -> str:
    rows = ["  t-pitch   fv-drift/poll   PM spread   traded $   prints",
            "  -------   -------------   ---------   --------   ------"]
    for b in sorted(agg):
        s = agg[b]
        drift = s["fv_move"] / s["fv_n"] if s["fv_n"] else float("nan")
        spread = s["spread"] / s["spread_n"] if s["spread_n"] else float("nan")
        rows.append(f"  {b:+5d}m   {drift:13.4f}   {spread:9.3f}"
                    f"   {s['vol']:8.0f}   {s['trades']:6d}")
    return "\n".join(rows)


def _sweep_row(games: list[tuple[dict, list[dict]]], label: str,
               start_for, queue: bool = False, reprice_only: bool = False,
               resize_frac: float | None = None,
               touch_edge: float | None = None, improve: bool = False) -> str:
    """One sweep row. `start_for(meta, events)` -> absolute quote-on ts, or
    None to skip that game (the lineup-gated strategy skips games whose
    lineups never post/settle on tape; clock strategies never skip)."""
    fills = filled = pnl = 0.0
    mo = [0.0] * len(HORIZONS)
    posts = skipped = 0
    for meta, events in games:
        st = start_for(meta, events)
        if st is None:
            skipped += 1
            continue
        pb, series = simulate(meta, events, 0.0, start_ts=st, queue=queue,
                              reprice_only=reprice_only,
                              resize_frac=resize_frac,
                              touch_edge=touch_edge, improve=improve)
        if not series:
            continue
        end = meta["commence_ts"] - QUOTE_UNTIL_MIN * 60.0
        pnl += pb.pnl_mark(fv_at(series, end))
        fills += len(pb.fills)
        filled += sum(f.shares * f.price for f in pb.fills)
        posts += pb.posts
        for i, h in enumerate(HORIZONS):
            mo[i] += sum(markout(f, series, h) for f in pb.fills)
    cells = "   ".join(f"{m:{9 + len(str(int(h)))}.2f}"
                       for m, h in zip(mo, HORIZONS))
    skip = f"   (skips {skipped}/{len(games)})" if skipped else ""
    return (f"  {label:>9}   {int(fills):5d}   {posts:6d}   {filled:8.0f}"
            f"   {pnl:10.2f}   {cells}{skip}")


def _fmt_sweep(games: list[tuple[dict, list[dict]]], queue: bool = False,
               reprice_only: bool = False,
               resize_frac: float | None = None) -> str:
    rows = ["  quote-from   fills    posts   filled $   mark P&L $   " +
            "   ".join(f"markout {int(h)}s $" for h in HORIZONS),
            "  ----------   -----   ------   --------   ----------   " +
            "   ".join("-" * (9 + len(str(int(h)))) for h in HORIZONS)]
    for qf in SWEEP_FROM_MIN:
        rows.append(_sweep_row(
            games, f"{qf}m",
            lambda meta, events, qf=qf: meta["commence_ts"] - qf * 60.0,
            queue=queue, reprice_only=reprice_only, resize_frac=resize_frac))
    rows.append(_sweep_row(
        games, "lu-gated",
        lambda meta, events: lineup_gated_start(events), queue=queue,
        reprice_only=reprice_only, resize_frac=resize_frac))
    return "\n".join(rows)


def _fmt_touch(games: list[tuple[dict, list[dict]]]) -> str:
    """Touch-pricing comparison at the LIVE policy settings (lu-gated
    start, pessimistic queue, keep-if-unchanged + resize): baseline
    fv-derived bids vs lifting to the displayed touch ('join') or one tick
    inside it ('imp'), per TOUCH_EDGES minimum retained edge. The question
    fix-2 asks: do the extra fills survive their markout, and what does
    tracking the touch cost in posts (each reprice rejoins the queue)?"""
    start = lambda meta, events: lineup_gated_start(events)  # noqa: E731
    rows = ["    pricing   fills    posts   filled $   mark P&L $   " +
            "   ".join(f"markout {int(h)}s $" for h in HORIZONS),
            "  ---------   -----   ------   --------   ----------   " +
            "   ".join("-" * (9 + len(str(int(h)))) for h in HORIZONS)]
    rows.append(_sweep_row(games, "baseline", start, queue=True,
                           reprice_only=True, resize_frac=RESIZE_FRAC))
    # cap only: an unreachable min edge disables lifting but keeps the
    # ask cap — the honest baseline. Fills it removes vs `baseline` were
    # fictional: bids at/through the recorded ask that the live venue
    # silently drops (the session-2211 pathology). Judge join/imp rows
    # against THIS row, not the uncapped one.
    rows.append(_sweep_row(games, "cap only", start, queue=True,
                           reprice_only=True, resize_frac=RESIZE_FRAC,
                           touch_edge=1.0))
    for improve, tag in ((False, "join"), (True, "imp")):
        for edge in TOUCH_EDGES:
            rows.append(_sweep_row(
                games, f"{tag} {edge * 100:.1f}c", start, queue=True,
                reprice_only=True, resize_frac=RESIZE_FRAC,
                touch_edge=edge, improve=improve))
    return "\n".join(rows)


def _fmt_fill_buckets(games: list[tuple[dict, list[dict]]]) -> str:
    """Markout by WHEN the fill happened, for the widest quote window —
    the direct answer to 'when is quoting profitable'."""
    agg: dict[int, list[float]] = {}
    h = HORIZONS[-1]
    for meta, events in games:
        pb, series = simulate(meta, events, max(SWEEP_FROM_MIN))
        for f in pb.fills:
            b = agg.setdefault(bucket(f.ts, meta["commence_ts"]), [0.0, 0.0, 0.0])
            b[0] += 1
            b[1] += f.shares
            b[2] += markout(f, series, h)
    rows = [f"  fill time   fills   markout {int(h)}s ¢/share   markout $",
            "  ---------   -----   -------------------   ---------"]
    for k in sorted(agg):
        n, shares, mo = agg[k]
        cents = 100.0 * mo / shares if shares else float("nan")
        rows.append(f"  {k:+7d}m   {int(n):5d}   {cents:19.2f}   {mo:9.2f}")
    return "\n".join(rows)


def _fmt_lineup_buckets(games: list[tuple[dict, list[dict]]]) -> str:
    """Markout by fill time relative to LINEUP COMPLETION (both sides
    posted), widest quote window. Same math as _fmt_fill_buckets with the
    origin moved from first pitch to the lineup event — if fv settling is
    really 'lineups posted' in disguise, this table shows it directly.
    Games whose recording never saw both lineups are excluded."""
    agg: dict[int, list[float]] = {}
    h = HORIZONS[-1]
    n_games = 0
    for meta, events in games:
        lu = lineup_complete_ts(events)
        if lu is None:
            continue
        n_games += 1
        pb, series = simulate(meta, events, max(SWEEP_FROM_MIN))
        for f in pb.fills:
            b = agg.setdefault(bucket(f.ts, lu), [0.0, 0.0, 0.0])
            b[0] += 1
            b[1] += f.shares
            b[2] += markout(f, series, h)
    if not n_games:
        return "  (no recordings with both lineups observed)"
    rows = [f"  vs lineup   fills   markout {int(h)}s ¢/share   markout $"
            f"      ({n_games} game(s) with lineup data)",
            "  ---------   -----   -------------------   ---------"]
    for k in sorted(agg):
        n, shares, mo = agg[k]
        cents = 100.0 * mo / shares if shares else float("nan")
        rows.append(f"  {k:+7d}m   {int(n):5d}   {cents:19.2f}   {mo:9.2f}")
    return "\n".join(rows)


def expand(argv: list[str]) -> list[str]:
    """CLI args -> recording files. A directory pulls in every *.jsonl and
    *.jsonl.gz beneath it (recordings/ nests one folder per slate day;
    archived days are gzipped), EXCEPT an `intl` subfolder — that's the
    other venue; name it explicitly to replay it (and never mix the two
    in one run)."""
    paths: list[str] = []
    for a in argv:
        p = Path(a)
        if p.is_dir():
            paths += sorted(str(f) for pat in ("*.jsonl", "*.jsonl.gz")
                            for f in p.rglob(pat)
                            if "intl" not in f.relative_to(p).parts)
        else:
            paths.append(a)
    return paths


def main(argv: list[str]) -> None:
    paths = expand(argv)
    games = []
    for p in paths:
        loaded = load(p)
        if loaded is None:
            print(f"skipping {p}: no meta/events")
            continue
        games.append(loaded)
    if not games:
        print("no recordings found — run recorder.py first")
        return
    print(f"{len(games)} game(s)\n")
    agg: dict = {}
    for meta, events in games:
        market_stats(meta, events, agg)
    print("Market quality by minutes-to-first-pitch")
    print(_fmt_stats(agg) + "\n")
    print("Paper replay sweep over quote-start time (stop at first pitch)")
    print("OPTIMISTIC queue (front of line) — upper bound")
    print(_fmt_sweep(games) + "\n")
    print("PESSIMISTIC queue (tick-snapped, join behind displayed size,")
    print("nobody ahead cancels) — lower bound; truth is in between")
    print(_fmt_sweep(games, queue=True) + "\n")
    print("PESSIMISTIC queue, KEEP-IF-UNCHANGED: cancel-replace a side only")
    print("when its snapped price moves (stale size keeps resting). Compare")
    print("fills/markout/posts against the cancel-replace table above")
    print(_fmt_sweep(games, queue=True, reprice_only=True) + "\n")
    print("PESSIMISTIC queue, keep unless price moves OR desired size drifts")
    print(f">{int(RESIZE_FRAC * 100)}% (skew stays live at the cost of the queue spot)")
    print(_fmt_sweep(games, queue=True, reprice_only=True,
                     resize_frac=RESIZE_FRAC) + "\n")
    print("TOUCH PRICING (lu-gated, pessimistic queue, keep-if-unchanged +")
    print("resize — the live policy): lift a bid to the displayed touch")
    print("('join') or one tick inside ('imp') when it keeps >= min edge vs")
    print("skew-adjusted fair; bids capped a tick below the ask (live cap).")
    print("Judge marginal fills by markout per filled $, churn by posts")
    print(_fmt_touch(games) + "\n")
    print("Markout by fill time (widest window)")
    print(_fmt_fill_buckets(games) + "\n")
    print("Markout by fill time relative to lineup completion")
    print(_fmt_lineup_buckets(games))


if __name__ == "__main__":
    main(sys.argv[1:] or [str(Path(__file__).parent / "recordings")])
