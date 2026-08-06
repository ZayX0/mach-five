"""Offline self-check for session_report.py — synthetic journal +
recording, no network."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session_report import (_book_at, _hit, game_report, load_journal,
                            recordings_by_slug)


def check() -> None:
    T = 1_000_000.0
    slug = "aec-mlb-x-y"
    meta = {"type": "meta", "slug": slug, "home": "Chicago Cubs",
            "away": "New York Yankees", "token_a": f"{slug}#A",
            "token_b": f"{slug}#B", "commence_ts": T + 7200}
    events = [
        {"type": "book", "ts": T - 10, "side": "A", "best_bid": 0.49,
         "best_ask": 0.51, "bids": [[0.49, 150.0], [0.485, 200.0]], "asks": []},
        {"type": "book", "ts": T - 10, "side": "B", "best_bid": 0.50,
         "best_ask": 0.52, "bids": [[0.50, 90.0]], "asks": []},
        # inside tick 1's window: at our A px -> "hit"; taker BUY ignored
        {"type": "trade", "ts": T + 10, "token": f"{slug}#A",
         "taker_side": "SELL", "price": 0.485, "size": 100.0},
        {"type": "trade", "ts": T + 11, "token": f"{slug}#A",
         "taker_side": "BUY", "price": 0.485, "size": 999.0},
        # above our px: not reachable
        {"type": "trade", "ts": T + 12, "token": f"{slug}#A",
         "taker_side": "SELL", "price": 0.49, "size": 999.0},
        # after tick 2 (a HOLD): quote pulled, must NOT count
        {"type": "trade", "ts": T + 70, "token": f"{slug}#A",
         "taker_side": "SELL", "price": 0.40, "size": 999.0},
    ]
    journal = [
        {"type": "tick", "ts": T, "slug": slug, "quoting": True, "fv": 0.4906,
         "net_a": 0.0, "kept": 0, "posted": 2, "strays": 0, "open": 2,
         "sides": {"A": {"act": "posted", "px": 0.485, "sz": 40.0,
                         "oid": "O1", "since": T},
                   "B": {"act": "posted", "px": 0.495, "sz": 40.0,
                         "oid": "O2", "since": T}}},
        {"type": "pull", "ts": T + 45, "slug": slug, "reason": "print 9999"},
        {"type": "tick", "ts": T + 60, "slug": slug, "quoting": False,
         "reason": "cooldown (print 9999)", "fv": 0.4906, "net_a": 0.0},
    ]

    # _book_at: latest snapshot at/before ts; nothing before the first
    books = {"A": [(T - 10, 0.49, [[0.49, 150.0], [0.485, 200.0]])]}
    assert _book_at(books["A"], T) == (0.49, [[0.49, 150.0], [0.485, 200.0]])
    assert _book_at(books["A"], T - 100) == (None, [])

    # _hit: (at px, strictly below px) SELLs inside the window only —
    # "below" is the decisive column: it fills regardless of queue
    trades = [e for e in events if e["type"] == "trade"]
    at, below = _hit(trades, "A", 0.485, T, T + 60)
    assert abs(at - 48.5) < 1e-9 and below == 0.0, (at, below)
    at2, below2 = _hit(trades, "A", 0.49, T, T + 60)     # px one tick up:
    assert abs(below2 - 48.5) < 1e-9, below2             # same print is BELOW
    assert _hit(trades, "B", 0.495, T, T + 60) == (0.0, 0.0)

    rep = game_report(slug, journal, meta, events)
    assert "1 quoting / 1 holding" in rep, rep
    assert "hit $48" in rep or "hit $49" in rep, rep     # ~$48.5 at our px
    assert "SWEPT" not in rep and "BELOW our px" in rep, rep
    assert "150 at lvl" not in rep                        # A px is 2nd level
    assert "200 at lvl" in rep, rep                       # displayed at 0.485
    assert "-0.5c vs bid" in rep, rep                     # a tick behind touch
    assert "PULL" in rep and "queue age lost: A 1m, B 1m" in rep, rep
    assert "cooldown (print 9999) x1" in rep, rep

    # loader + slug matcher work off real files
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "j.jsonl").write_text(
            "\n".join(json.dumps(e) for e in journal) + "\ngarbage\n")
        (d / "day").mkdir()
        (d / "day" / "g.jsonl").write_text(
            "\n".join(json.dumps(e) for e in [meta] + events) + "\n")
        j = load_journal(str(d / "j.jsonl"))
        assert len(j) == 3 and j[0]["type"] == "tick"
        recs = recordings_by_slug([str(d / "day")], {slug})
        assert slug in recs and recs[slug][0]["home"] == "Chicago Cubs"
    print("ok")


if __name__ == "__main__":
    check()
