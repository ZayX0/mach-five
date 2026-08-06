"""Offline self-check for replay.py — synthetic recording, no network."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mach_five
from paper_book import PaperBook
from replay import (RESIZE_FRAC, _fmt_fill_buckets, _fmt_lineup_buckets,
                    _fmt_stats, _fmt_sweep, _keep, bucket, expand, fv_at,
                    lineup_complete_ts, lineup_gated_start, markout,
                    market_stats, simulate, snap_bid)


def check() -> None:
    # importing replay pins RESEARCH sizing — paper tables must not shrink
    # when mach_five carries pilot-sized live constants
    assert (mach_five.BASE_SIZE, mach_five.MAX_INVENTORY) == (2000.0, 5000.0)
    T = 1_000_000.0  # first pitch
    meta = {"type": "meta", "market_id": "x", "token_a": "tokA",
            "token_b": "tokB", "condition_id": "0xc", "commence_ts": T}
    events = [
        {"type": "pinnacle", "ts": T - 7200, "fv": 0.5},
        {"type": "book", "ts": T - 7190, "side": "A",
         "best_bid": 0.48, "best_ask": 0.52, "bids": [], "asks": []},
        # a seller crosses our 0.494 bid (= 0.5 - HALF_SPREAD) after latency
        {"type": "trade", "ts": T - 7000, "token": "tokA",
         "taker_side": "SELL", "price": 0.49, "size": 100.0, "tx": "0x1"},
        {"type": "pinnacle", "ts": T - 3600, "fv": 0.55},
        {"type": "lineup", "ts": T - 10000, "side": "B", "game_pk": 111},
        {"type": "lineup", "ts": T - 8000, "side": "A", "game_pk": 111},
        {"type": "pinnacle", "ts": T - 60, "fv": 0.55},
    ]
    events.sort(key=lambda e: e["ts"])

    # stats: fv drift lands in the move's bucket; volume in the trade's
    agg = market_stats(meta, events)
    assert agg[-120]["spread_n"] == 1 and abs(agg[-120]["spread"] - 0.04) < 1e-9
    assert agg[-120]["trades"] == 1 and abs(agg[-120]["vol"] - 49.0) < 1e-6
    assert agg[-60]["fv_n"] == 1 and abs(agg[-60]["fv_move"] - 0.05) < 1e-9

    # wide window: the T-7000 print fills our A bid at 0.494
    pb, series = simulate(meta, events, quote_from_min=240)
    assert len(pb.fills) == 1 and pb.fills[0].side == "A"
    assert abs(pb.fills[0].price - 0.494) < 1e-9, pb.fills[0]

    # markout: flat fv at +60s -> capture the half spread; a horizon long
    # enough to cross the T-3600 repricing (fill+3600s > T-3600) sees fair
    # move our way and the fill look better
    f = pb.fills[0]
    assert abs(markout(f, series, 60.0) - (0.5 - 0.494) * 100.0) < 1e-9
    assert abs(markout(f, series, 3600.0) - (0.55 - 0.494) * 100.0) < 1e-9
    assert pb.pnl_mark(fv_at(series, T)) > 0.0

    # narrow window starts after the only print: nothing fills
    pb2, _ = simulate(meta, events, quote_from_min=59)
    assert not pb2.fills

    # fv_at: before, between, after
    assert fv_at(series, 0.0) == 0.5
    assert fv_at(series, T - 5000) == 0.5
    assert fv_at(series, T) == 0.55

    # lineup completion = when the SECOND side was observed; the fill at
    # T-7000 is +16..17min after it -> bucket +0 (relative-to-lineup frame)
    assert lineup_complete_ts(events) == T - 8000
    assert lineup_complete_ts([e for e in events if e["type"] != "lineup"]) is None
    lu_table = _fmt_lineup_buckets([(meta, events)])
    assert "vs lineup" in lu_table and "+0m" in lu_table, lu_table
    no_lu = _fmt_lineup_buckets([(meta, [e for e in events
                                         if e["type"] != "lineup"])])
    assert "no recordings" in no_lu

    # lineup lines don't pollute the market-quality buckets
    assert bucket(T - 8000, T) not in agg or agg[bucket(T - 8000, T)]["trades"] == 0

    # lineup-gated start: needs both lineups AND 3 calm fv polls after
    lu_ev = [{"type": "lineup", "ts": 100.0, "side": "B", "game_pk": 1},
             {"type": "lineup", "ts": 200.0, "side": "A", "game_pk": 1}]
    fvs = lambda pts: [{"type": "pinnacle", "ts": t, "fv": v} for t, v in pts]
    settled = lu_ev + fvs([(150, 0.50), (260, 0.52), (320, 0.521),
                           (380, 0.521), (440, 0.5215)])
    assert lineup_gated_start(settled) == 440.0        # 3rd calm delta
    churning = lu_ev + fvs([(260, 0.50), (320, 0.51), (380, 0.52),
                            (440, 0.53), (500, 0.54)])
    assert lineup_gated_start(churning) is None        # never settles
    assert lineup_gated_start(fvs([(260, 0.5), (320, 0.5)])) is None  # no lineups
    # main events: fv jumps 0.05 after lineups, never 3 calm polls -> the
    # gated strategy skips this game and the sweep row says so
    assert lineup_gated_start(events) is None

    # tick snapping: bids round DOWN to the half-cent grid
    assert snap_bid(0.494) == 0.49 and snap_bid(0.475) == 0.475
    assert snap_bid(0.4749) == 0.47

    # pessimistic queue mode: our 0.494 bid snaps to 0.49 and joins behind
    # the 150 shares displayed there; optimistic mode fills everything
    ev_q = [
        {"type": "book", "ts": T - 7300, "side": "A",
         "bids": [[0.49, 150.0]], "asks": []},
        {"type": "pinnacle", "ts": T - 7200, "fv": 0.5},
        {"type": "trade", "ts": T - 7000, "token": "tokA",
         "taker_side": "SELL", "price": 0.49, "size": 100.0},
        {"type": "trade", "ts": T - 6900, "token": "tokA",
         "taker_side": "SELL", "price": 0.49, "size": 100.0},
    ]
    pbq, _ = simulate(meta, ev_q, quote_from_min=240, queue=True)
    assert len(pbq.fills) == 1 and abs(pbq.pos_a - 50.0) < 1e-6, pbq.fills
    assert pbq.fills[0].price == 0.49            # filled at the snapped bid
    pbo, _ = simulate(meta, ev_q, quote_from_min=240)
    assert abs(pbo.pos_a - 200.0) < 1e-6, pbo.pos_a

    # _keep: price still and size alive -> hold; anything else -> replace
    kb = PaperBook("tokA", "tokB", latency=0.0)
    kb.post("A", 0.49, 1000.0, ts=0.0)
    assert _keep(kb, "A", 0.49, 1000.0, None)
    assert not _keep(kb, "A", 0.495, 1000.0, None)   # moved one tick
    assert not _keep(kb, "A", 0.49, 0.0, None)       # skew shut the side off
    assert not _keep(kb, "B", 0.51, 1000.0, None)    # nothing resting
    assert _keep(kb, "A", 0.49, 1200.0, RESIZE_FRAC)      # within 25%
    assert not _keep(kb, "A", 0.49, 2000.0, RESIZE_FRAC)  # drifted > 25%

    # keep-if-unchanged requote policy: an unchanged snapped price holds the
    # resting order and its eaten-down queue across ticks; cancel-replace
    # rejoins behind the full displayed size and misses the fill
    ev_k = [
        {"type": "book", "ts": T - 7300, "side": "A",
         "bids": [[0.49, 150.0]], "asks": []},
        {"type": "pinnacle", "ts": T - 7200, "fv": 0.5},
        {"type": "trade", "ts": T - 7100, "token": "tokA",
         "taker_side": "SELL", "price": 0.49, "size": 100.0},  # queue 150->50
        {"type": "pinnacle", "ts": T - 7080, "fv": 0.5},       # unchanged tick
        {"type": "trade", "ts": T - 7000, "token": "tokA",
         "taker_side": "SELL", "price": 0.49, "size": 100.0},
    ]
    keep, _ = simulate(meta, ev_k, quote_from_min=240, queue=True,
                       reprice_only=True)
    base, _ = simulate(meta, ev_k, quote_from_min=240, queue=True)
    assert abs(keep.pos_a - 50.0) < 1e-6, keep.pos_a   # 50 to queue, 50 to us
    assert base.pos_a == 0.0                 # repost re-acquired the full 150
    assert keep.posts == 2 and base.posts == 4         # churn: 2 sides/tick

    # a >= 1-tick fv move DOES reprice under the keep policy (both sides)
    ev_m = sorted(ev_k + [{"type": "pinnacle", "ts": T - 6900, "fv": 0.52}],
                  key=lambda e: e["ts"])
    keep2, _ = simulate(meta, ev_m, quote_from_min=240, queue=True,
                        reprice_only=True)
    assert keep2.posts == 4, keep2.posts

    # report formatting shouldn't blow up
    assert "t-pitch" in _fmt_stats(agg)
    sweep = _fmt_sweep([(meta, events)])
    assert "quote-from" in sweep and "posts" in sweep
    assert "lu-gated" in sweep and "(skips 1/1)" in sweep, sweep
    assert "lu-gated" in _fmt_sweep([(meta, events)], queue=True,
                                    reprice_only=True,
                                    resize_frac=RESIZE_FRAC)
    assert "fill time" in _fmt_fill_buckets([(meta, events)])

    # expand: a directory recurses into day folders but skips intl/ unless
    # that folder is named explicitly; plain files pass through
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "08-01-2026").mkdir()
        (d / "intl").mkdir()
        (d / "08-01-2026" / "g1.jsonl").write_text("{}\n")
        (d / "top.jsonl").write_text("{}\n")
        (d / "intl" / "old.jsonl").write_text("{}\n")
        got = expand([tmp])
        assert [Path(p).name for p in got] == ["g1.jsonl", "top.jsonl"], got
        assert [Path(p).name for p in expand([str(d / "intl")])] == ["old.jsonl"]
        assert expand([str(d / "top.jsonl")]) == [str(d / "top.jsonl")]
    print("ok")


if __name__ == "__main__":
    check()
