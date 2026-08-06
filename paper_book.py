"""Paper book — simulated maker fills against the real trade tape.

Orders rest in memory. A resting bid fills only when a real print SELLS into
it: same token, taker side SELL, price at or below our bid, and the print is
timestamped after the order had been live for `latency` seconds (models
Pinnacle-poll + requote delay; without it adverse selection vanishes and the
sim lies). We always pay OUR bid price, even when the print was lower.

Deliberately conservative about *when* we fill (a book merely touching our
price is not a fill; only actual flow is). Queue position has two modes per
order, set by `queue_ahead` at post time:

- queue_ahead=0 (default): OPTIMISTIC — we're at the front, every crossing
  print fills us up to our size. Paper P&L is an upper bound.
- queue_ahead=<displayed qty at our level>: PESSIMISTIC — we join the back
  and nobody ahead ever cancels. Prints AT our price consume the queue
  first and only the overflow fills us; a print strictly BELOW our price
  means the level swept, so queue no longer matters. A lower bound.

Truth lives between the two; replay.py prints both sweeps.

Positions are tracked in shares per side; sizes posted are in $ to match
mach_five.quotes. ponytail: no complementary-side matching — a taker BUYING
the other token at >= (1 - our bid) is economically a fill too, but we skip
it; strictly conservative.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LATENCY_SEC = 2.0


@dataclass
class Fill:
    ts: float
    side: str      # "A" | "B"
    price: float   # what we paid per share (our bid)
    shares: float


@dataclass
class Order:
    side: str
    token: str
    price: float
    dollars: float  # remaining $ to spend
    ts: float       # active from ts + latency
    queue_ahead: float = 0.0  # shares resting ahead of us at our price


@dataclass
class PaperBook:
    """Mirrors mach_five.Book's role, but methods take explicit timestamps so
    it works in replay; not a drop-in for mach_five.step."""
    token_a: str
    token_b: str
    latency: float = LATENCY_SEC
    pos_a: float = 0.0   # shares
    pos_b: float = 0.0
    cost: float = 0.0    # total $ paid
    posts: int = 0       # accepted posts — the churn counter (venue load)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)

    def cancel_all(self) -> None:
        self.orders.clear()

    def cancel_side(self, side: str) -> None:
        """Cancel resting orders on one side only — replay's keep-if-unchanged
        requote policy holds the other side's queue spot."""
        self.orders = [o for o in self.orders if o.side != side]

    def post(self, side: str, price: float, dollars: float, ts: float,
             queue_ahead: float = 0.0) -> None:
        """Rest a paper BUY of $`dollars` of `side` YES at `price`.
        `queue_ahead` = shares already displayed at that level (pessimistic
        queue mode); 0 = front of queue (optimistic, the default)."""
        if dollars <= 0.0 or not (0.0 < price < 1.0):
            return
        self.posts += 1
        token = self.token_a if side == "A" else self.token_b
        self.orders.append(Order(side, token, price, dollars, ts,
                                 max(0.0, queue_ahead)))

    def on_trade(self, ts: float, token: str, taker_side: str,
                 price: float, shares: float) -> None:
        """Feed one tape print; fill any resting bid it crosses. A print AT
        an order's price consumes its queue_ahead before filling it; a
        print strictly BELOW means the level swept (queue irrelevant)."""
        if taker_side != "SELL":
            return
        for o in self.orders:
            if shares <= 0.0:
                break
            if (o.token != token or price > o.price + 1e-9
                    or ts < o.ts + self.latency):
                continue
            if price > o.price - 1e-9:          # print at our level: the
                eaten = min(o.queue_ahead, shares)  # queue ahead eats first
                o.queue_ahead -= eaten
                shares -= eaten
                if shares <= 0.0:
                    continue
            got = min(o.dollars / o.price, shares)
            o.dollars -= got * o.price
            shares -= got
            self._apply(Fill(ts, o.side, o.price, got))
        self.orders = [o for o in self.orders if o.dollars > 1e-9]

    def _apply(self, f: Fill) -> None:
        self.fills.append(f)
        self.cost += f.shares * f.price
        if f.side == "A":
            self.pos_a += f.shares
        else:
            self.pos_b += f.shares

    def inventory_dollars(self, fv: float) -> float:
        """Signed $ value of the unhedged leg (complete sets carry no risk),
        in mach_five's convention: +$ = long A. Feed this to quotes()."""
        net = self.pos_a - self.pos_b
        return net * fv if net >= 0 else net * (1.0 - fv)

    def pnl_mark(self, fv: float) -> float:
        """Mark-to-fair P&L in $: value both legs at fair, minus cost."""
        return self.pos_a * fv + self.pos_b * (1.0 - fv) - self.cost
