"""Pick the pilot market: the volume leader among upcoming games.

Ranks every Pinnacle game whose first pitch falls inside the eligibility
window by the US venue's cumulative traded notional
(`stats.notionalTraded`, in CENTS — calibrated 2026-08-02) and prints the
winner as `<slug> <commence_epoch>` on stdout for a launcher script to
consume. The ranking table goes to stderr so the launcher can log it
without polluting the machine-readable line. Exits 1 when no game
qualifies. Costs one Odds API credit per run.

Window rationale: MIN_LEAD keeps enough runway for the lineup gate to
open before the first-pitch stop; MAX_AHEAD keeps the pick inside the
same evening's slate instead of grabbing tomorrow's early game.
"""
import sys
import time

import odds_feed
import us_market

MIN_LEAD_SEC = 2.0 * 3600
MAX_AHEAD_SEC = 10.0 * 3600


def rank(games, now, market_for, notional_for,
         min_lead=MIN_LEAD_SEC, max_ahead=MAX_AHEAD_SEC):
    """[(slug, commence_epoch, notional_cents)] sorted best-first.
    `market_for(game)` -> US market dict or None; `notional_for(slug)` ->
    cumulative traded cents (0 on any failure). Pure — injectable for the
    offline check."""
    rows = []
    for g in games:
        if g.commence_time is None:
            continue
        pitch = g.commence_time.timestamp()
        if not (now + min_lead <= pitch <= now + max_ahead):
            continue
        m = market_for(g)
        if not m or not m.get("slug"):
            continue
        rows.append((m["slug"], pitch, notional_for(m["slug"])))
    rows.sort(key=lambda r: (-r[2], r[1]))
    return rows


def notional_cents(stats: dict | None) -> int:
    """Cumulative traded cents from a book's stats blob; 0 on any failure.
    The gateway wraps the value in a money dict ({"value": "...",
    "currency": "USD"}) whose number is still CENTS despite the label
    (recalibrated 2026-08-05: shares x price matches value/100)."""
    try:
        raw = (stats or {}).get("notionalTraded") or 0
        if isinstance(raw, dict):
            raw = raw.get("value") or 0
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def _notional(slug: str) -> int:
    try:
        return notional_cents(us_market.market_book(slug).get("stats"))
    except Exception:
        return 0


def ranked_rows(now: float) -> list[tuple[str, float, int]]:
    """The live ranking — one Odds API credit plus a gateway book fetch
    per candidate. Shared entry point for the CLI below and
    pilot_launch.py's pick step."""
    cache: dict = {}
    return rank(odds_feed.pinnacle_moneylines().values(), now,
                lambda g: us_market.find_market(g, search_cache=cache),
                _notional)


def main() -> int:
    now = time.time()
    rows = ranked_rows(now)
    for slug, pitch, cents in rows:
        mins = (pitch - now) / 60.0
        print(f"  {slug:40s} pitch in {mins:5.0f}m   traded ${cents / 100:>10,.0f}",
              file=sys.stderr)
    if not rows:
        print("no eligible game in the window", file=sys.stderr)
        return 1
    slug, pitch, _ = rows[0]
    print(f"{slug} {int(pitch)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
