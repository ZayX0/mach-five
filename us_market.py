"""Polymarket US market data — the venue we can actually trade.

Structurally different from the international exchange clob.py reads:

- ONE instrument per game, not two YES tokens. `marketSides` mark the long
  team (instrument price = P(long team wins)) and the short team;
  `team.ordering` says which is home/away. The long side varies per market,
  so everything is normalized back into the repo's fixed A=home frame here.
- Discovery: GET /v1/search?query=<team> -> events (event `startTime` IS the
  covered game's first pitch, so doubleheaders disambiguate directly — no
  resolution-text parsing) -> nested markets; the moneyline is
  sportsMarketType == "baseball_team_full_game_winner".
- Books: GET /v1/markets/{slug}/book — keyless REST.
- Trades: the markets WebSocket (official `polymarket_us` SDK, Ed25519 auth);
  taker side is explicit on each message, and each trade has an `id`.
  Liveness is actively probed: the server pushes nothing on a quiet market
  (no app-level heartbeats observed), so after QUIET_PING_SEC of silence the
  feed sends a protocol-level ping — pong refreshes `last_rx`, timeout tears
  the session down. `age()` > FEED_STALE_SEC therefore means a genuinely
  unhealthy feed, which is what guard.py's dead man assumes.
- Sports markets trade PRE-GAME; resting orders clear at scheduled start.

Credentials live in the macOS Keychain (services `mach-five-key-id` /
`mach-five-secret-key`) — never in .env, never in logs.

Units (CALIBRATED 2026-08-02 against book-stats deltas over intact tape):
trade `size` is CONTRACTS (each settles at $1) — pre-game markets show
sharesTraded deltas matching tape sums within ~1%. `stats.notionalTraded`
is denominated in CENTS despite its USD tag (ratio to tape px*size ~101).
So price*size on the tape IS dollars. Caveat: during IN-PLAY firehose
periods the WS tape undercounts prints ~2x vs sharesTraded — fine for the
pre-game strategy, don't trust in-play tape volume.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import clob  # for the injectable-transport _get helper (repo convention)
from game_match import _is_team, _nicknames, _norm, _parse_ts

GATEWAY = "https://gateway.polymarket.us"
WINNER_TYPE = "baseball_team_full_game_winner"
EVENT_TIME_TOLERANCE = 2 * 3600  # doubleheader disambiguation, seconds

KEYCHAIN_KEY_ID = "mach-five-key-id"
KEYCHAIN_SECRET = "mach-five-secret-key"


# --- credentials ------------------------------------------------------------
def _keychain(service: str) -> str:
    out = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _credential_file(service: str) -> str:
    """systemd LoadCredential backend: one file per service name under
    $CREDENTIALS_DIRECTORY (the Linux equivalent of the macOS Keychain —
    root-owned, exposed read-only to the unit; never .env, never logs)."""
    path = Path(os.environ["CREDENTIALS_DIRECTORY"]) / service
    return path.read_text().strip()


def _default_secret(service: str) -> str:
    if os.environ.get("CREDENTIALS_DIRECTORY"):
        return _credential_file(service)
    return _keychain(service)


def creds(get_secret: Callable[[str], str] = _default_secret) -> tuple[str, str]:
    """(key_id, secret_key). Backend picked by environment: systemd
    credentials directory when present (Linux server), macOS Keychain
    otherwise. Raises if missing either way."""
    return get_secret(KEYCHAIN_KEY_ID), get_secret(KEYCHAIN_SECRET)


# --- discovery --------------------------------------------------------------
def search_events(team: str, cache: dict[str, list[dict]],
                  get_json=clob._get) -> list[dict]:
    if team not in cache:
        data = get_json(f"{GATEWAY}/v1/search", query=team)
        cache[team] = data.get("events", []) if isinstance(data, dict) else []
    return cache[team]


def match_event(home: str, away: str, game_dt: datetime | None,
                search_cache: dict, get_json=clob._get) -> dict | None:
    """Best open US event covering this game, or None. Same title matching
    as game_match.match_event; time matching uses the event's startTime."""
    candidates: dict[Any, dict] = {}
    for team in (home, away):
        for ev in search_events(team, search_cache, get_json):
            if not ev.get("closed"):
                candidates[ev.get("id") or ev.get("slug")] = ev

    def title_matches(ev: dict) -> bool:
        title = _norm(str(ev.get("title", "")))
        return any(n in title for n in _nicknames(home)) and any(
            n in title for n in _nicknames(away))

    matched = [ev for ev in candidates.values() if title_matches(ev)]
    if not matched or game_dt is None:
        return matched[0] if len(matched) == 1 and game_dt is None else None
    timed = []
    for ev in matched:
        ev_dt = _parse_ts(ev.get("startTime") or ev.get("startDate"))
        if ev_dt is not None:
            timed.append((abs((ev_dt - game_dt).total_seconds()), ev))
    if not timed:
        return None
    dist, ev = min(timed, key=lambda t: t[0])
    return ev if dist <= EVENT_TIME_TOLERANCE else None


def find_market(game, *, search_cache: dict | None = None,
                get_json=clob._get) -> dict | None:
    """Full US market dict (with marketSides) for this game's moneyline.
    `game` is an odds_feed.Game."""
    search_cache = {} if search_cache is None else search_cache
    ev = match_event(game.home, game.away, game.commence_time,
                     search_cache, get_json)
    if ev is None:
        return None
    ml = [m for m in ev.get("markets", [])
          if m.get("sportsMarketType") == WINNER_TYPE and not m.get("closed")]
    if not ml:
        return None
    # nested search markets omit marketSides; fetch the full record
    data = get_json(f"{GATEWAY}/v1/markets", slug=ml[0]["slug"])
    markets = data.get("markets", []) if isinstance(data, dict) else []
    return markets[0] if markets else None


def long_side(market: dict, home: str) -> str | None:
    """'A' when the instrument's long team is the home team, 'B' when away,
    None when it can't be determined."""
    for s in market.get("marketSides", []):
        if not s.get("long"):
            continue
        team = s.get("team", {}) or {}
        ordering = team.get("ordering")
        if ordering in ("home", "away"):
            return "A" if ordering == "home" else "B"
        name = team.get("name") or s.get("description") or ""
        if name:
            return "A" if _is_team(str(name), home) else "B"
    return None


# --- book (keyless) ---------------------------------------------------------
def _levels(entries: list[dict]) -> list[tuple[float, float]]:
    out = []
    for e in entries or []:
        try:
            out.append((float(e["px"]["value"]), float(e["qty"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def market_book(slug: str, get_json=clob._get) -> dict:
    """Raw instrument-frame book + state + stats for one US market."""
    data = get_json(f"{GATEWAY}/v1/markets/{slug}/book")
    md = data.get("marketData", {}) if isinstance(data, dict) else {}
    return {
        "bids": sorted(_levels(md.get("bids", [])), reverse=True),
        "asks": sorted(_levels(md.get("offers", []))),
        "state": md.get("state"),
        "stats": md.get("stats"),
    }


def book_a_frame(raw: dict, long: str) -> dict:
    """Normalize an instrument-frame book into the A=home frame. When the
    long team is away ('B'), A's book is the complement mirror: buying A at p
    == selling the instrument at 1-p."""
    if long == "A":
        bids, asks = raw["bids"], raw["asks"]
    else:
        bids = sorted(((round(1.0 - p, 4), q) for p, q in raw["asks"]), reverse=True)
        asks = sorted((round(1.0 - p, 4), q) for p, q in raw["bids"])
    return {
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "bids": bids, "asks": asks,
        "state": raw.get("state"), "stats": raw.get("stats"),
    }


def mirror(book: dict) -> dict:
    """The complementary side's view of an A-frame book (B bids = 1 - A asks)."""
    bids = sorted(((round(1.0 - p, 4), q) for p, q in book["asks"]), reverse=True)
    asks = sorted((round(1.0 - p, 4), q) for p, q in book["bids"])
    return {"best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            "bids": bids, "asks": asks}


# --- trades (WebSocket) -----------------------------------------------------
def _trade_ts(iso: str) -> float:
    """Epoch seconds from the WS tradeTime (nanosecond precision ISO)."""
    trimmed = re.sub(r"(\.\d{1,6})\d*", r"\1", str(iso)).replace("Z", "+00:00")
    return datetime.fromisoformat(trimmed).timestamp()


def trade_line(trade: dict, long: str, token_a: str, token_b: str) -> dict | None:
    """Normalize one WS trade into the recorder's schema.

    Every print is emitted as the side a seller crossed (`taker_side` is
    always SELL by construction) — a taker BUYING the instrument is a seller
    of the complement side at (1 - px). That's exactly the event the paper
    fill rule consumes, and it preserves the international recordings'
    format so replay.py runs unchanged.
    """
    try:
        px = float(trade["price"]["value"])
        qty = float(trade["quantity"]["value"])
        ts = _trade_ts(trade["tradeTime"])
        taker = trade.get("taker", {}).get("side")
    except (KeyError, TypeError, ValueError):
        return None
    if taker not in ("ORDER_SIDE_BUY", "ORDER_SIDE_SELL"):
        return None
    token_long = token_a if long == "A" else token_b
    token_short = token_b if long == "A" else token_a
    if taker == "ORDER_SIDE_SELL":   # seller hit the long side's bids
        token, price = token_long, px
    else:                            # buyer == seller of the complement side
        token, price = token_short, round(1.0 - px, 4)
    return {"type": "trade", "ts": ts, "token": token, "taker_side": "SELL",
            "price": price, "size": qty, "id": trade.get("id"),
            "instrument_px": px, "taker_instrument_side": taker}


class TradeFeed(threading.Thread):
    """Background markets-WS consumer. Guarded top to bottom: any error is
    logged and the session reconnects; it can never take the recorder down.
    Trades that print while disconnected are lost (no replay on reconnect) —
    the book stats snapshots bound how much a gap missed."""

    RECONNECT_SEC = 10.0
    STALE_SEC = 180.0  # no messages AND no pongs for this long = dead
    QUIET_PING_SEC = 10.0   # silence before actively probing the socket
    PING_TIMEOUT_SEC = 5.0  # unanswered protocol ping = dead transport

    def __init__(self, key_id: str, secret_key: str,
                 on_trade: Callable[[dict], None], log: Callable[[str], None] = print):
        super().__init__(daemon=True, name="us-trade-feed")
        self._key_id, self._secret = key_id, secret_key
        self._on_trade, self._log = on_trade, log
        self._slugs: set[str] = set()
        self._dirty = False
        self._lock = threading.Lock()
        self.last_rx = 0.0  # epoch of the last inbound WS message (any kind)

    def age(self, now: float) -> float:
        """Seconds since the feed last heard ANYTHING (heartbeats count).
        inf until the first message — treat an unstarted feed as stale."""
        return now - self.last_rx if self.last_rx else float("inf")

    def watch(self, slugs: set[str]) -> None:
        """Set the slugs to subscribe to; no-op when unchanged."""
        with self._lock:
            if slugs != self._slugs:
                self._slugs = set(slugs)
                self._dirty = True

    def run(self) -> None:
        while True:
            try:
                asyncio.run(self._session())
            except Exception as e:
                self._log(f"trade feed reconnecting: {type(e).__name__}: {e}")
            time.sleep(self.RECONNECT_SEC)

    @staticmethod
    def _conn_error(connected: bool, last_rx: float, now: float,
                    stale_sec: float) -> str | None:
        """None while the session looks alive, else why it must be torn down.
        The SDK's message loop swallows a closed connection (it only emits a
        'close' event and exits), so without this check a dead socket leaves
        the session spinning forever and the tape silently stops."""
        if not connected:
            return "connection closed"
        if now - last_rx > stale_sec:
            return f"no messages for {now - last_rx:.0f}s"
        return None

    async def _keepalive(self, conn, now: float) -> None:
        """Actively ping the transport when the stream has been quiet.

        A settled or overnight market pushes nothing — indistinguishable
        from a dead socket by listening alone (the 2026-08-02 log showed
        the staleness check force-reconnecting a healthy-but-silent feed
        every 3 minutes). An RFC-6455 ping must be ponged by any live
        peer regardless of subscription activity: a pong counts as life
        (refreshes last_rx, so age() stays honest for guard.py's dead
        man), a timeout tears the session down."""
        if conn is None or now - self.last_rx <= self.QUIET_PING_SEC:
            return
        try:
            pong = await conn.ping()
            await asyncio.wait_for(pong, self.PING_TIMEOUT_SEC)
        except Exception as e:
            raise ConnectionError(
                f"ping unanswered after {now - self.last_rx:.0f}s "
                f"silence ({type(e).__name__})") from e
        self.last_rx = time.time()

    async def _session(self) -> None:
        from polymarket_us import PolymarketUS
        ws = PolymarketUS(key_id=self._key_id, secret_key=self._secret).ws.markets()
        ws.on("trade", self._handle)
        self.last_rx = time.time()

        def _rx(*_a) -> None:
            self.last_rx = time.time()

        ws.on("message", _rx)  # any inbound message (heartbeats too) is life
        await ws.connect()
        self._log("trade feed connected")
        with self._lock:
            self._dirty = True  # (re)subscribe on every fresh session
        req = 0
        while True:
            todo = None
            with self._lock:
                if self._dirty:
                    todo, self._dirty = sorted(self._slugs), False
            if todo is not None:
                req += 1
                if req > 1:
                    await ws.unsubscribe(f"t{req - 1}")
                if todo:
                    await ws.subscribe_trades(f"t{req}", todo)
                    self._log(f"trade feed watching {len(todo)} market(s)")
            # ws._ws: the SDK doesn't expose the raw connection, and the
            # protocol-level ping lives there
            await self._keepalive(ws._ws, time.time())
            err = self._conn_error(ws.is_connected, self.last_rx,
                                   time.time(), self.STALE_SEC)
            if err:
                raise ConnectionError(err)
            await asyncio.sleep(1.0)

    def _handle(self, msg: dict) -> None:
        try:
            trade = msg.get("trade")
            if trade:
                self._on_trade(trade)
        except Exception as e:  # a bad message must not kill the socket
            self._log(f"trade handler error: {type(e).__name__}: {e}")
