"""Session report — offline "why no fill" analysis of one live session.

Joins a session journal (journals/session-*.jsonl, written by
mach_five.run(); pure observability) with the recorder's market data for
the same games. Per quoting tick and side it shows: the keep/post
decision, where our bid rested vs the touch, how much size was displayed
at our level, and the $ of taker-sells that printed at/below our price
while that quote rested ("hit $" — flow that a front-of-queue order
would have filled). The summary quantifies the two structural questions
from the session-3 post-mortem: how much reachable flow the displayed
queue ate, and how much queue age each guard pull threw away.

Read-only; nothing here touches live behavior.

Usage:  python3 session_report.py journals/session-<stamp>.jsonl [recordings ...]
        (recording args are files or day folders; default: recordings/)
Offline check: python3 tests/check_session_report.py
"""
from __future__ import annotations

import json
import sys
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

import replay
from replay import _level_qty

TICK_GAP_MAX = 90.0     # assume a quote rested until next tick or this long


def load_journal(path: str) -> list[dict]:
    """Journal lines sorted by ts (bad lines skipped)."""
    out = []
    for line in Path(path).read_text().splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "ts" in obj:
            out.append(obj)
    out.sort(key=lambda e: e["ts"])
    return out


def recordings_by_slug(paths: list[str], slugs: set[str]) -> dict[str, tuple]:
    """slug -> (meta, events) for recordings covering the journaled games."""
    out: dict[str, tuple] = {}
    for p in replay.expand(paths):
        loaded = replay.load(p)
        if loaded and loaded[0].get("slug") in slugs:
            out[loaded[0]["slug"]] = loaded
    return out


def _series(events: list[dict]) -> tuple[dict, list]:
    """({side: [(ts, best_bid, bids)]}, [trade events]) from a recording."""
    books: dict[str, list] = {"A": [], "B": []}
    trades = []
    for e in events:
        if e.get("type") == "book" and e.get("side") in books:
            books[e["side"]].append((e["ts"], e.get("best_bid"),
                                     e.get("bids") or []))
        elif e.get("type") == "trade":
            trades.append(e)
    return books, trades


def _book_at(series: list, ts: float):
    """Latest (best_bid, bids) at or before ts; (None, []) if none yet."""
    i = bisect_right(series, (ts, float("inf"), []))
    return (series[i - 1][1], series[i - 1][2]) if i else (None, [])


def _hit(trades: list[dict], side: str, px: float, t0: float, t1: float) -> float:
    """$ of taker-SELL prints at/below px on `side`'s token in [t0, t1)."""
    return sum(t["price"] * t["size"] for t in trades
               if t0 <= t["ts"] < t1 and t.get("taker_side") == "SELL"
               and t.get("token", "").endswith(f"#{side}")
               and t["price"] <= px + 1e-9)


def _hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def game_report(slug: str, entries: list[dict], meta: dict,
                events: list[dict]) -> str:
    """The per-tick table + summary for one game."""
    books, trades = _series(events)
    ticks = [e for e in entries if e.get("type") == "tick"]
    pulls = [e for e in entries if e.get("type") == "pull"]
    rows, holds = [], {}
    hit_total = {"A": 0.0, "B": 0.0}
    acts = {"A": {}, "B": {}}
    for i, t in enumerate(ticks):
        ts = t["ts"]
        if not t.get("quoting"):
            holds[t.get("reason", "?")] = holds.get(t.get("reason", "?"), 0) + 1
            rows.append(f"  {_hms(ts)}  HOLD  {t.get('reason', '?')}")
            continue
        t1 = min(ticks[i + 1]["ts"] if i + 1 < len(ticks) else ts + TICK_GAP_MAX,
                 ts + TICK_GAP_MAX)
        cells = []
        for side in ("A", "B"):
            s = (t.get("sides") or {}).get(side)
            if s is None or s.get("act") == "none":
                cells.append(f"{side} --")
                acts[side]["none"] = acts[side].get("none", 0) + 1
                continue
            acts[side][s["act"]] = acts[side].get(s["act"], 0) + 1
            px = s["px"]
            best, bids = _book_at(books[side], ts)
            dist = f"{(px - best) * 100:+.1f}c" if best is not None else "n/a"
            ahead = _level_qty(bids, px)
            hit = _hit(trades, side, px, ts, t1)
            hit_total[side] += hit
            cells.append(f"{side} {s['act'][:4]} {px:.3f} ({dist} vs bid, "
                         f"{ahead:8,.0f} at lvl, hit ${hit:,.0f})")
        rows.append(f"  {_hms(ts)}  Q     " + "   ".join(cells))
    # pulls: queue age thrown away = pull ts - each side's `since`
    pull_lines = []
    for p in pulls:
        prior = [t for t in ticks if t["ts"] <= p["ts"] and t.get("quoting")]
        ages = []
        if prior:
            for side in ("A", "B"):
                s = (prior[-1].get("sides") or {}).get(side) or {}
                if s.get("since"):
                    ages.append(f"{side} {(p['ts'] - s['since']) / 60:.0f}m")
        pull_lines.append(f"  {_hms(p['ts'])}  PULL  {p['reason']}"
                          f"   (queue age lost: {', '.join(ages) or 'n/a'})")
    q = sum(1 for t in ticks if t.get("quoting"))
    out = [f"{slug}  ({meta.get('away')} @ {meta.get('home')})",
           f"  ticks: {q} quoting / {len(ticks) - q} holding "
           + (f"(holds: {', '.join(f'{k} x{v}' for k, v in holds.items())})"
              if holds else ""),
           f"  acts: A {acts['A']}   B {acts['B']}",
           f"  reachable flow while resting (front-of-queue would fill): "
           f"A ${hit_total['A']:,.0f}   B ${hit_total['B']:,.0f}"]
    out += pull_lines
    return "\n".join(out + rows)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    entries = load_journal(argv[0])
    slugs = {e["slug"] for e in entries if "slug" in e}
    recs = recordings_by_slug(argv[1:] or ["recordings"], slugs)
    if not entries:
        print("empty journal")
        return 1
    for slug in sorted(slugs):
        game = [e for e in entries if e.get("slug") == slug]
        if slug not in recs:
            print(f"{slug}: no recording found (searched "
                  f"{argv[1:] or ['recordings']}) — journal-only summary")
            q = sum(1 for e in game if e.get("quoting"))
            print(f"  ticks: {q} quoting / {len(game) - q} other\n")
            continue
        meta, events = recs[slug]
        print(game_report(slug, game, meta, events) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
