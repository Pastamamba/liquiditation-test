"""WebSocket market-data feed Hyperliquid public-feedille.

Yhdistää WS:ään, subscribe `l2Book(symbol)`, `trades(symbol)`, `allMids`.
Pitää viimeisen L2-snapshotin muistissa, laskee mid-pricen, säilyttää 60s+
vol-historiaa, käsittelee disconnectit exponential backoffilla.

Callbackit ajetaan erillisessä taskissa `asyncio.Queue`:n läpi jotta WS-loop
ei blokkaannu hitaista handlereista.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import numpy as np
import websockets

OrderSide = Literal["bid", "ask"]

WS_URL_MAINNET = "wss://api.hyperliquid.xyz/ws"
WS_URL_TESTNET = "wss://api.hyperliquid-testnet.xyz/ws"

SECONDS_PER_YEAR = 365.25 * 24 * 3600

DEFAULT_PING_INTERVAL_S = 15.0
DEFAULT_SILENCE_TIMEOUT_S = 5.0
DEFAULT_INITIAL_BACKOFF_S = 1.0
DEFAULT_MAX_BACKOFF_S = 60.0
DEFAULT_MID_HISTORY = 1000
DEFAULT_CB_QUEUE_MAX = 1000

_logger = logging.getLogger(__name__)


# ---------- Data types ----------


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal
    n_orders: int = 0


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    timestamp_ms: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / Decimal("2")
        return None

    @property
    def spread_bps(self) -> Decimal | None:
        m = self.mid
        if m is None or m == 0:
            return None
        return (self.asks[0].price - self.bids[0].price) / m * Decimal("10000")


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: OrderSide
    price: Decimal
    size: Decimal
    timestamp_ms: int
    tid: int


# ---------- Parsers ----------


def _parse_book_level(d: Any) -> BookLevel | None:
    if not isinstance(d, dict):
        return None
    try:
        return BookLevel(
            price=Decimal(str(d["px"])),
            size=Decimal(str(d["sz"])),
            n_orders=int(d.get("n", 0)),
        )
    except (KeyError, ValueError):
        return None


def parse_l2book(data: Any) -> OrderBook | None:
    """Parse Hyperliquid l2Book-channel-data → OrderBook."""
    if not isinstance(data, dict):
        return None
    coin = data.get("coin")
    levels = data.get("levels")
    if not isinstance(coin, str) or not isinstance(levels, list) or len(levels) < 2:
        return None
    bids_raw = levels[0] if isinstance(levels[0], list) else []
    asks_raw = levels[1] if isinstance(levels[1], list) else []
    bids: list[BookLevel] = []
    for d in bids_raw:
        lvl = _parse_book_level(d)
        if lvl is not None:
            bids.append(lvl)
    asks: list[BookLevel] = []
    for d in asks_raw:
        lvl = _parse_book_level(d)
        if lvl is not None:
            asks.append(lvl)
    return OrderBook(
        symbol=coin,
        timestamp_ms=int(data.get("time", 0)),
        bids=tuple(bids),
        asks=tuple(asks),
    )


def parse_trade(data: Any) -> Trade | None:
    """Parse Hyperliquid trades-channel-arrayn yksittäinen trade-dict."""
    if not isinstance(data, dict):
        return None
    try:
        return Trade(
            symbol=str(data["coin"]),
            side="bid" if data.get("side", "B") == "B" else "ask",
            price=Decimal(str(data["px"])),
            size=Decimal(str(data["sz"])),
            timestamp_ms=int(data["time"]),
            tid=int(data.get("tid", 0)),
        )
    except (KeyError, ValueError):
        return None


# ---------- MarketDataFeed ----------

ConnectFactory = Callable[[str], Awaitable[Any]]
_BookCallback = Callable[[OrderBook], Awaitable[None] | None]
_TradeCallback = Callable[[Trade], Awaitable[None] | None]
_DisconnectCallback = Callable[[], Awaitable[None] | None]


class MarketDataFeed:
    """WebSocket-pohjainen Hyperliquid market-data-feed.

    Args:
        symbol: kohde-symboli (esim. "ETH").
        network: testnet | mainnet.
        on_book_update: callback joka L2-päivitykselle (sync tai async).
        on_trade: callback joka trade-tapahtumalle.
        on_disconnect: callback aina kun WS katkeaa.
        connect_factory: testaus-injektio (palauttaa fake WS).
        ping_interval_s: kuinka usein lähetetään ping.
        silence_timeout_s: jos ei viestejä N sekuntia → disconnect.
        initial_backoff_s, max_backoff_s: reconnect-backoff.
        mid_history_max: deque-koko realized vol -laskennalle.
        cb_queue_maxsize: callback-queue max-koko.
    """

    def __init__(
        self,
        symbol: str,
        *,
        network: Literal["testnet", "mainnet"] = "testnet",
        on_book_update: _BookCallback | None = None,
        on_trade: _TradeCallback | None = None,
        on_disconnect: _DisconnectCallback | None = None,
        connect_factory: ConnectFactory | None = None,
        ping_interval_s: float = DEFAULT_PING_INTERVAL_S,
        silence_timeout_s: float = DEFAULT_SILENCE_TIMEOUT_S,
        initial_backoff_s: float = DEFAULT_INITIAL_BACKOFF_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
        mid_history_max: int = DEFAULT_MID_HISTORY,
        cb_queue_maxsize: int = DEFAULT_CB_QUEUE_MAX,
    ) -> None:
        self.symbol = symbol
        self.network = network
        self._url = WS_URL_MAINNET if network == "mainnet" else WS_URL_TESTNET
        self._on_book_update = on_book_update
        self._on_trade = on_trade
        self._on_disconnect = on_disconnect
        self._connect_factory = connect_factory

        self._ping_interval = ping_interval_s
        self._silence_timeout = silence_timeout_s
        self._initial_backoff = initial_backoff_s
        self._max_backoff = max_backoff_s

        self._current_book: OrderBook | None = None
        self._mid_history: deque[tuple[float, Decimal]] = deque(maxlen=mid_history_max)
        self._all_mids: dict[str, Decimal] = {}

        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._main_task: asyncio.Task[None] | None = None
        self._cb_task: asyncio.Task[None] | None = None
        self._cb_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(
            maxsize=cb_queue_maxsize
        )
        self._last_message_at: float = 0.0

    # ----- Public properties -----

    @property
    def current_book(self) -> OrderBook | None:
        return self._current_book

    @property
    def current_mid(self) -> Decimal | None:
        return self._current_book.mid if self._current_book is not None else None

    @property
    def is_connected(self) -> bool:
        return self._connected_event.is_set()

    @property
    def all_mids(self) -> dict[str, Decimal]:
        return dict(self._all_mids)

    def realized_volatility(self, window_seconds: float = 60.0) -> float | None:
        """Annualisoitu realized vol log-returneista.

        Käytetään `np.std(log_returns, ddof=1) * sqrt(SECONDS_PER_YEAR / mean_dt)`.
        Palauttaa None jos data:a < 3 pistettä ikkunassa.
        """
        if window_seconds <= 0:
            return None
        now = time.monotonic()
        cutoff = now - window_seconds
        sample = [(t, p) for t, p in self._mid_history if t >= cutoff]
        if len(sample) < 3:
            return None
        prices = np.array([float(p) for _, p in sample], dtype=np.float64)
        if np.any(prices <= 0):
            return None
        log_prices = np.log(prices)
        log_returns = np.diff(log_prices)
        if log_returns.size < 2:
            return None
        times = np.array([t for t, _ in sample], dtype=np.float64)
        time_deltas = np.diff(times)
        avg_dt = float(np.mean(time_deltas))
        if avg_dt <= 0:
            return None
        std_returns = float(np.std(log_returns, ddof=1))
        return std_returns * math.sqrt(SECONDS_PER_YEAR / avg_dt)

    # ----- Lifecycle -----

    async def start(self) -> None:
        if self._main_task is not None:
            return
        self._stop_event.clear()
        self._cb_task = asyncio.create_task(
            self._callback_loop(), name="md-callbacks"
        )
        self._main_task = asyncio.create_task(
            self._connect_loop(), name="md-main"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._main_task is not None:
            self._main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._main_task
            self._main_task = None
        with contextlib.suppress(asyncio.QueueFull):
            self._cb_queue.put_nowait(("__stop__", None))
        if self._cb_task is not None:
            try:
                await asyncio.wait_for(self._cb_task, timeout=2.0)
            except TimeoutError:
                self._cb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._cb_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cb_task = None

    # ----- Connect loop -----

    async def _connect_loop(self) -> None:
        backoff = self._initial_backoff
        while not self._stop_event.is_set():
            ws: Any = None
            try:
                ws = await self._open_ws()
                self._connected_event.set()
                backoff = self._initial_backoff
                _logger.info("WS connected to %s", self._url)
                await self._subscribe(ws)
                await self._run_session(ws)
            except asyncio.CancelledError:
                if ws is not None and hasattr(ws, "close"):
                    with contextlib.suppress(Exception):
                        await ws.close()
                raise
            except Exception as exc:
                _logger.warning("WS session error: %s", exc)
            finally:
                self._connected_event.clear()
                if ws is not None and hasattr(ws, "close"):
                    with contextlib.suppress(Exception):
                        await ws.close()
                if self._on_disconnect is not None and not self._stop_event.is_set():
                    with contextlib.suppress(asyncio.QueueFull):
                        self._cb_queue.put_nowait(("disconnect", None))

            if self._stop_event.is_set():
                break

            sleep_s = min(backoff, self._max_backoff)
            _logger.info("WS reconnecting in %.1fs", sleep_s)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=sleep_s
                )
                break
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self._max_backoff)

    async def _open_ws(self) -> Any:
        if self._connect_factory is not None:
            return await self._connect_factory(self._url)
        return await websockets.connect(
            self._url, ping_interval=None, ping_timeout=None, close_timeout=2
        )

    async def _subscribe(self, ws: Any) -> None:
        for sub in (
            {"type": "l2Book", "coin": self.symbol},
            {"type": "trades", "coin": self.symbol},
            {"type": "allMids"},
        ):
            await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    async def _run_session(self, ws: Any) -> None:
        self._last_message_at = time.monotonic()
        ping_task = asyncio.create_task(self._ping_loop(ws), name="md-ping")
        try:
            while not self._stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=self._silence_timeout
                    )
                except TimeoutError as exc:
                    elapsed = time.monotonic() - self._last_message_at
                    raise ConnectionError(
                        f"WS silent for {elapsed:.1f}s"
                    ) from exc
                self._last_message_at = time.monotonic()
                await self._handle_message(msg)
        finally:
            ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ping_task

    async def _ping_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                await ws.send(json.dumps({"method": "ping"}))
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            msg = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            _logger.debug("WS bad message: %r", raw)
            return
        if not isinstance(msg, dict):
            return
        channel = msg.get("channel")
        data = msg.get("data")

        if channel == "l2Book":
            book = parse_l2book(data)
            if book is not None and book.symbol == self.symbol:
                self._current_book = book
                m = book.mid
                if m is not None:
                    self._mid_history.append((time.monotonic(), m))
                if self._on_book_update is not None:
                    self._enqueue_callback("book", book)
        elif channel == "trades":
            if isinstance(data, list):
                for raw_t in data:
                    t = parse_trade(raw_t)
                    if t is None or t.symbol != self.symbol:
                        continue
                    if self._on_trade is not None:
                        self._enqueue_callback("trade", t)
        elif channel == "allMids":
            mids = (data or {}).get("mids", {}) if isinstance(data, dict) else {}
            self._all_mids = {
                str(k): Decimal(str(v))
                for k, v in mids.items()
                if isinstance(k, str)
            }
        elif channel in ("subscriptionResponse", "pong"):
            _logger.debug("WS control: %s %s", channel, data)
        # else: muut kanavat (esim. error) → ignore

    def _enqueue_callback(self, kind: str, payload: Any) -> None:
        try:
            self._cb_queue.put_nowait((kind, payload))
        except asyncio.QueueFull:
            _logger.warning("MD callback queue full, dropping %s", kind)

    async def _callback_loop(self) -> None:
        while True:
            kind, payload = await self._cb_queue.get()
            try:
                if kind == "__stop__":
                    return
                cb = self._dispatch_callback(kind)
                if cb is None:
                    continue
                result = cb() if kind == "disconnect" else cb(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                _logger.exception("MD callback error: kind=%s", kind)

    def _dispatch_callback(self, kind: str) -> Callable[..., Any] | None:
        if kind == "book":
            return self._on_book_update
        if kind == "trade":
            return self._on_trade
        if kind == "disconnect":
            return self._on_disconnect
        return None
