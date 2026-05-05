"""Tests for src.market_data."""

from __future__ import annotations

import asyncio
import json
import math
import time
from decimal import Decimal
from typing import Any

from src.market_data import (
    SECONDS_PER_YEAR,
    BookLevel,
    MarketDataFeed,
    OrderBook,
    Trade,
    parse_l2book,
    parse_trade,
)

# ---------- parse_l2book / parse_trade ----------


class TestParseL2Book:
    def test_valid(self) -> None:
        data = {
            "coin": "ETH",
            "time": 1700_000_000_000,
            "levels": [
                [
                    {"px": "1850.4", "sz": "5.2", "n": 3},
                    {"px": "1850.3", "sz": "10.1", "n": 5},
                ],
                [
                    {"px": "1850.6", "sz": "4.8", "n": 2},
                    {"px": "1850.7", "sz": "9.0", "n": 4},
                ],
            ],
        }
        book = parse_l2book(data)
        assert book is not None
        assert book.symbol == "ETH"
        assert book.timestamp_ms == 1700_000_000_000
        assert book.best_bid == BookLevel(Decimal("1850.4"), Decimal("5.2"), 3)
        assert book.best_ask == BookLevel(Decimal("1850.6"), Decimal("4.8"), 2)
        assert book.mid == Decimal("1850.5")
        assert book.spread_bps is not None
        # spread = 0.2/1850.5 * 10000 ≈ 1.08 bps
        assert abs(float(book.spread_bps) - 1.08) < 0.01

    def test_empty_levels(self) -> None:
        book = parse_l2book(
            {"coin": "ETH", "time": 1, "levels": [[], []]}
        )
        assert book is not None
        assert book.bids == ()
        assert book.asks == ()
        assert book.mid is None
        assert book.spread_bps is None

    def test_missing_fields(self) -> None:
        assert parse_l2book(None) is None
        assert parse_l2book({}) is None
        assert parse_l2book({"coin": "ETH"}) is None
        assert parse_l2book({"coin": "ETH", "levels": "x"}) is None

    def test_skips_invalid_level(self) -> None:
        data = {
            "coin": "ETH",
            "time": 1,
            "levels": [
                [{"px": "1850", "sz": "1"}, {"bad": True}],
                [{"px": "1851", "sz": "1"}],
            ],
        }
        book = parse_l2book(data)
        assert book is not None
        assert len(book.bids) == 1
        assert len(book.asks) == 1


class TestParseTrade:
    def test_valid_buy(self) -> None:
        t = parse_trade({
            "coin": "ETH",
            "side": "B",
            "px": "1850.5",
            "sz": "0.1",
            "time": 1700_000_000_000,
            "tid": 42,
        })
        assert t == Trade(
            symbol="ETH",
            side="bid",
            price=Decimal("1850.5"),
            size=Decimal("0.1"),
            timestamp_ms=1700_000_000_000,
            tid=42,
        )

    def test_valid_sell(self) -> None:
        t = parse_trade({
            "coin": "ETH",
            "side": "A",
            "px": "1850.5",
            "sz": "0.1",
            "time": 1,
            "tid": 1,
        })
        assert t is not None
        assert t.side == "ask"

    def test_missing_required(self) -> None:
        assert parse_trade({}) is None
        assert parse_trade({"coin": "ETH"}) is None
        assert parse_trade(None) is None


# ---------- realized_volatility ----------


class TestRealizedVolatility:
    def test_too_few_points(self) -> None:
        feed = MarketDataFeed("ETH")
        assert feed.realized_volatility(60) is None
        feed._mid_history.append((time.monotonic(), Decimal("1850")))
        feed._mid_history.append((time.monotonic(), Decimal("1851")))
        assert feed.realized_volatility(60) is None  # need 3+

    def test_constant_price_zero_vol(self) -> None:
        feed = MarketDataFeed("ETH")
        now = time.monotonic()
        for i in range(5):
            feed._mid_history.append((now - 4 + i, Decimal("1850")))
        vol = feed.realized_volatility(60)
        assert vol is not None
        assert vol == 0.0

    def test_known_vol_value(self) -> None:
        # With log returns alternating ±x at dt=1s, std = x * sqrt(N/(N-1)) (sample std).
        # Here we choose flat 1% step pattern → log_returns ≈ ±ln(1.01) ≈ ±0.00995.
        # With 6 prices [100,101,100,101,100,101] → 5 log_returns, std ≈ 0.0109.
        feed = MarketDataFeed("ETH")
        now = time.monotonic()
        prices = ["100", "101", "100", "101", "100", "101"]
        for i, p in enumerate(prices):
            feed._mid_history.append((now - 5 + i, Decimal(p)))
        vol = feed.realized_volatility(window_seconds=60)
        assert vol is not None
        # std of log-returns ≈ 0.01089 (computed below as ground truth)
        import numpy as np
        log_p = np.log(np.array([float(x) for x in prices]))
        log_r = np.diff(log_p)
        expected_std = float(np.std(log_r, ddof=1))
        expected_vol = expected_std * math.sqrt(SECONDS_PER_YEAR / 1.0)
        assert abs(vol - expected_vol) < 1e-6

    def test_window_filters_old_data(self) -> None:
        feed = MarketDataFeed("ETH")
        now = time.monotonic()
        # Old data outside window — high volatility
        for i in range(5):
            feed._mid_history.append((now - 1000 + i, Decimal(str(100 + i * 10))))
        # Recent data inside window — flat
        for i in range(5):
            feed._mid_history.append((now - 4 + i, Decimal("1850")))
        vol = feed.realized_volatility(window_seconds=10)
        assert vol == 0.0  # only flat recent data considered

    def test_negative_window_returns_none(self) -> None:
        feed = MarketDataFeed("ETH")
        assert feed.realized_volatility(window_seconds=0) is None
        assert feed.realized_volatility(window_seconds=-1) is None


# ---------- Fake WS infrastructure ----------


class FakeWS:
    """Minimal mock WebSocket — async send/recv/close, queue-based recv."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False
        self.close_count = 0

    async def send(self, msg: str) -> None:
        if self.closed:
            raise ConnectionError("ws closed")
        self.sent.append(msg)

    async def recv(self) -> str:
        if self.closed:
            raise ConnectionError("ws closed")
        item = await self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        return str(item)

    async def close(self) -> None:
        self.closed = True
        self.close_count += 1

    def push(self, msg: dict[str, Any]) -> None:
        self._inbox.put_nowait(json.dumps(msg))

    def push_raw(self, raw: str) -> None:
        self._inbox.put_nowait(raw)

    def push_error(self, exc: BaseException) -> None:
        self._inbox.put_nowait(exc)


def _l2_msg(symbol: str, bid: str, ask: str, *, time_ms: int = 1) -> dict[str, Any]:
    return {
        "channel": "l2Book",
        "data": {
            "coin": symbol,
            "time": time_ms,
            "levels": [
                [{"px": bid, "sz": "1.0", "n": 1}],
                [{"px": ask, "sz": "1.0", "n": 1}],
            ],
        },
    }


def _trade_msg(symbol: str, side: str, px: str, sz: str) -> dict[str, Any]:
    return {
        "channel": "trades",
        "data": [
            {
                "coin": symbol,
                "side": side,
                "px": px,
                "sz": sz,
                "time": 1,
                "tid": 1,
            }
        ],
    }


async def _wait_until(
    predicate: Any, deadline_s: float = 1.0, interval: float = 0.01
) -> bool:
    end = asyncio.get_event_loop().time() + deadline_s
    while asyncio.get_event_loop().time() < end:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


# ---------- MarketDataFeed lifecycle / callbacks ----------


async def test_subscribe_messages_sent() -> None:
    ws = FakeWS()

    async def factory(_url: str) -> Any:
        return ws

    feed = MarketDataFeed("ETH", connect_factory=factory)
    await feed.start()
    assert await _wait_until(lambda: len(ws.sent) >= 3)
    parsed = [json.loads(s) for s in ws.sent[:3]]
    types = [p["subscription"]["type"] for p in parsed]
    assert types == ["l2Book", "trades", "allMids"]
    assert parsed[0]["subscription"]["coin"] == "ETH"
    await feed.stop()


async def test_book_callback_invoked() -> None:
    ws = FakeWS()
    received: list[OrderBook] = []

    def on_book(book: OrderBook) -> None:
        received.append(book)

    async def factory(_url: str) -> Any:
        return ws

    feed = MarketDataFeed("ETH", on_book_update=on_book, connect_factory=factory)
    await feed.start()
    ws.push(_l2_msg("ETH", "1850.4", "1850.6"))
    assert await _wait_until(lambda: len(received) == 1)
    assert received[0].mid == Decimal("1850.5")
    assert feed.current_mid == Decimal("1850.5")
    assert feed.current_book is not None
    await feed.stop()


async def test_book_for_other_symbol_ignored() -> None:
    ws = FakeWS()
    received: list[OrderBook] = []

    def on_book(book: OrderBook) -> None:
        received.append(book)

    async def factory(_url: str) -> Any:
        return ws

    feed = MarketDataFeed("ETH", on_book_update=on_book, connect_factory=factory)
    await feed.start()
    ws.push(_l2_msg("BTC", "60000", "60001"))
    ws.push(_l2_msg("ETH", "1850.4", "1850.6"))
    assert await _wait_until(lambda: len(received) == 1)
    assert received[0].symbol == "ETH"
    await feed.stop()


async def test_trade_callback_invoked() -> None:
    ws = FakeWS()
    trades: list[Trade] = []

    def on_trade(t: Trade) -> None:
        trades.append(t)

    async def factory(_url: str) -> Any:
        return ws

    feed = MarketDataFeed("ETH", on_trade=on_trade, connect_factory=factory)
    await feed.start()
    ws.push(_trade_msg("ETH", "B", "1850.5", "0.1"))
    assert await _wait_until(lambda: len(trades) == 1)
    assert trades[0].side == "bid"
    assert trades[0].price == Decimal("1850.5")
    await feed.stop()


async def test_async_callback_supported() -> None:
    ws = FakeWS()
    received: list[OrderBook] = []

    async def on_book(book: OrderBook) -> None:
        received.append(book)

    async def factory(_url: str) -> Any:
        return ws

    feed = MarketDataFeed("ETH", on_book_update=on_book, connect_factory=factory)
    await feed.start()
    ws.push(_l2_msg("ETH", "1850.4", "1850.6"))
    assert await _wait_until(lambda: len(received) == 1)
    await feed.stop()


async def test_all_mids_stored() -> None:
    ws = FakeWS()

    async def factory(_url: str) -> Any:
        return ws

    feed = MarketDataFeed("ETH", connect_factory=factory)
    await feed.start()
    ws.push({"channel": "allMids", "data": {"mids": {"ETH": "1850.55", "BTC": "60100"}}})
    assert await _wait_until(lambda: "ETH" in feed.all_mids)
    assert feed.all_mids["ETH"] == Decimal("1850.55")
    assert feed.all_mids["BTC"] == Decimal("60100")
    await feed.stop()


async def test_mid_history_appended() -> None:
    ws = FakeWS()

    async def factory(_url: str) -> Any:
        return ws

    feed = MarketDataFeed("ETH", connect_factory=factory)
    await feed.start()
    for i in range(5):
        ws.push(_l2_msg("ETH", f"{1850 + i}", f"{1851 + i}"))
    assert await _wait_until(lambda: len(feed._mid_history) == 5)
    await feed.stop()


async def test_callback_exception_does_not_kill_loop() -> None:
    ws = FakeWS()
    count = 0

    def on_book(book: OrderBook) -> None:
        nonlocal count
        count += 1
        if count == 1:
            raise RuntimeError("boom")

    async def factory(_url: str) -> Any:
        return ws

    feed = MarketDataFeed("ETH", on_book_update=on_book, connect_factory=factory)
    await feed.start()
    ws.push(_l2_msg("ETH", "1850.4", "1850.6"))
    ws.push(_l2_msg("ETH", "1850.5", "1850.7"))
    assert await _wait_until(lambda: count == 2)
    await feed.stop()


async def test_disconnect_callback_on_silence() -> None:
    ws = FakeWS()
    disconnects: list[None] = []

    async def on_disconnect() -> None:
        disconnects.append(None)

    # Counter to make factory return one ws then a 2nd; otherwise reconnect-loop spins forever.
    calls = 0
    second_ws = FakeWS()

    async def factory(_url: str) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ws
        return second_ws

    feed = MarketDataFeed(
        "ETH",
        on_disconnect=on_disconnect,
        connect_factory=factory,
        silence_timeout_s=0.05,  # 50ms silence → disconnect
        initial_backoff_s=0.05,
        max_backoff_s=0.05,
    )
    await feed.start()
    # Don't push anything → silence triggers disconnect → reconnect → silence again.
    assert await _wait_until(lambda: len(disconnects) >= 1, deadline_s=2.0)
    await feed.stop()


async def test_reconnect_on_recv_error() -> None:
    first = FakeWS()
    second = FakeWS()
    received: list[OrderBook] = []

    def on_book(book: OrderBook) -> None:
        received.append(book)

    calls = 0

    async def factory(_url: str) -> Any:
        nonlocal calls
        calls += 1
        return first if calls == 1 else second

    feed = MarketDataFeed(
        "ETH",
        on_book_update=on_book,
        connect_factory=factory,
        silence_timeout_s=2.0,
        initial_backoff_s=0.05,
        max_backoff_s=0.05,
    )
    await feed.start()
    # Push valid book on first → success.
    first.push(_l2_msg("ETH", "1850.4", "1850.6"))
    # Then force connection error.
    first.push_error(ConnectionError("conn closed"))
    # After reconnect, push another book on second.
    await asyncio.sleep(0.2)
    second.push(_l2_msg("ETH", "1900.4", "1900.6"))
    assert await _wait_until(lambda: len(received) >= 2, deadline_s=2.0)
    assert calls >= 2
    assert first.close_count >= 1
    await feed.stop()


async def test_stop_halts_reconnect() -> None:
    calls = 0

    async def factory(_url: str) -> Any:
        nonlocal calls
        calls += 1
        ws = FakeWS()
        # No messages → silence → disconnect → reconnect
        return ws

    feed = MarketDataFeed(
        "ETH",
        connect_factory=factory,
        silence_timeout_s=0.05,
        initial_backoff_s=0.05,
        max_backoff_s=0.05,
    )
    await feed.start()
    await asyncio.sleep(0.15)
    await feed.stop()
    snapshot = calls
    await asyncio.sleep(0.2)
    # After stop, no further reconnects.
    assert calls == snapshot


async def test_subscribe_uses_correct_symbol() -> None:
    ws = FakeWS()

    async def factory(_url: str) -> Any:
        return ws

    feed = MarketDataFeed("BTC", connect_factory=factory)
    await feed.start()
    assert await _wait_until(lambda: len(ws.sent) >= 3)
    parsed = [json.loads(s) for s in ws.sent[:3]]
    assert parsed[0]["subscription"] == {"type": "l2Book", "coin": "BTC"}
    assert parsed[1]["subscription"] == {"type": "trades", "coin": "BTC"}
    assert parsed[2]["subscription"] == {"type": "allMids"}
    await feed.stop()


async def test_url_per_network() -> None:
    feed_test = MarketDataFeed("ETH", network="testnet")
    feed_main = MarketDataFeed("ETH", network="mainnet")
    assert "testnet" in feed_test._url
    assert "testnet" not in feed_main._url


async def test_bad_message_does_not_crash() -> None:
    ws = FakeWS()

    async def factory(_url: str) -> Any:
        return ws

    feed = MarketDataFeed("ETH", connect_factory=factory)
    await feed.start()
    ws.push_raw("not-json")
    ws.push_raw(json.dumps([1, 2, 3]))  # array, not dict
    ws.push(_l2_msg("ETH", "1850.4", "1850.6"))
    assert await _wait_until(lambda: feed.current_mid is not None)
    await feed.stop()
