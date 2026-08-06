"""Offline self-check for paper_book.py — simulated fills, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper_book import PaperBook


def check() -> None:
    pb = PaperBook("tokA", "tokB", latency=2.0)
    pb.post("A", 0.567, 567.0, ts=100.0)     # 1000 shares wanted
    pb.post("B", 0.427, 213.5, ts=100.0)     # 500 shares wanted
    pb.post("A", 0.5, 0.0, ts=100.0)         # zero size: dropped
    assert len(pb.orders) == 2

    pb.on_trade(101.0, "tokA", "SELL", 0.56, 100.0)   # inside latency: no fill
    pb.on_trade(103.0, "tokA", "BUY", 0.56, 100.0)    # taker BUY: no fill
    pb.on_trade(103.0, "tokA", "SELL", 0.58, 100.0)   # above our bid: no fill
    pb.on_trade(103.0, "tokX", "SELL", 0.50, 100.0)   # other market: no fill
    assert not pb.fills

    pb.on_trade(104.0, "tokA", "SELL", 0.55, 400.0)   # partial fill at OUR price
    assert len(pb.fills) == 1 and pb.pos_a == 400.0
    assert pb.fills[0].price == 0.567                  # pay our bid, not the print
    pb.on_trade(105.0, "tokA", "SELL", 0.567, 900.0)  # rest of A (600), order gone
    assert abs(pb.pos_a - 1000.0) < 1e-6, pb.pos_a
    assert all(o.side != "A" for o in pb.orders)

    # long A -> positive inventory; more A flow can't fill (no A order rests)
    fv = 0.573
    assert pb.inventory_dollars(fv) > 0.0

    pb.on_trade(106.0, "tokB", "SELL", 0.42, 500.0)   # B side fills: 500 shares
    assert abs(pb.pos_b - 500.0) < 1e-6 and not pb.orders

    # complete sets are riskless: net 500 A shares is the only exposure
    net_expo = pb.inventory_dollars(fv)
    assert abs(net_expo - 500.0 * fv) < 1e-4, net_expo

    # bought both legs below $1 -> the 500 complete sets lock in profit;
    # pnl at fair beats the sets' edge alone
    set_edge = 500.0 * (1.0 - (0.567 + 0.427))
    assert pb.pnl_mark(fv) > 0.0
    assert pb.pnl_mark(fv) > set_edge - 1e-9

    # cancel clears resting orders only, not positions
    pb.post("A", 0.5, 100.0, ts=200.0)
    pb.cancel_all()
    assert not pb.orders and abs(pb.pos_a - 1000.0) < 1e-6

    # pessimistic queue: 100 shares displayed ahead of our 200-share bid
    q = PaperBook("tokA", "tokB", latency=2.0)
    q.post("A", 0.47, 94.0, ts=0.0, queue_ahead=100.0)
    q.on_trade(10.0, "tokA", "SELL", 0.47, 60.0)     # eaten by the queue
    assert not q.fills and q.orders[0].queue_ahead == 40.0
    q.on_trade(11.0, "tokA", "SELL", 0.47, 90.0)     # 40 to queue, 50 to us
    assert len(q.fills) == 1 and abs(q.pos_a - 50.0) < 1e-9, q.pos_a
    assert q.orders[0].queue_ahead == 0.0
    q.on_trade(12.0, "tokA", "SELL", 0.465, 500.0)   # below our level: sweep
    assert abs(q.pos_a - 200.0) < 1e-9 and not q.orders

    # a print below our price fills even with queue still ahead (level swept)
    q2 = PaperBook("tokA", "tokB", latency=0.0)
    q2.post("A", 0.47, 47.0, ts=0.0, queue_ahead=1000.0)
    q2.on_trade(1.0, "tokA", "SELL", 0.46, 100.0)
    assert abs(q2.pos_a - 100.0) < 1e-9 and q2.fills[0].price == 0.47

    # cancel_side clears one side only; posts counts ACCEPTED orders
    c = PaperBook("tokA", "tokB", latency=0.0)
    c.post("A", 0.5, 50.0, ts=0.0)
    c.post("B", 0.4, 40.0, ts=0.0)
    c.post("A", 0.5, 0.0, ts=0.0)                    # rejected: not counted
    assert c.posts == 2
    c.cancel_side("A")
    assert [o.side for o in c.orders] == ["B"] and c.posts == 2

    # queue retention — the point of keep-if-unchanged: a HELD order keeps
    # its eaten-down queue; a cancel-repost rejoins behind the full display
    held = PaperBook("tokA", "tokB", latency=0.0)
    repost = PaperBook("tokA", "tokB", latency=0.0)
    for b in (held, repost):
        b.post("A", 0.47, 94.0, ts=0.0, queue_ahead=100.0)
        b.on_trade(1.0, "tokA", "SELL", 0.47, 80.0)   # queue 100 -> 20
    assert held.orders[0].queue_ahead == 20.0
    repost.cancel_side("A")
    repost.post("A", 0.47, 94.0, ts=1.5, queue_ahead=100.0)  # back of line
    for b in (held, repost):
        b.on_trade(2.0, "tokA", "SELL", 0.47, 50.0)
    assert abs(held.pos_a - 30.0) < 1e-9, held.pos_a   # 20 eaten, 30 to us
    assert repost.pos_a == 0.0                         # all 50 to the queue
    print("ok")


if __name__ == "__main__":
    check()
