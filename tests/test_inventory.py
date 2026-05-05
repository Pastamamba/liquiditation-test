"""Tests for src.inventory."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import HyperliquidConfig
from src.hl_client import FillEntry, Position
from src.inventory import (
    InventoryManager,
    InventorySnapshot,
    parse_user_fill,
)
from src.state import StateStore


def _fill(
    *,
    side: str = "bid",
    price: str = "1850.5",
    size: str = "0.1",
    fee: str = "0.0028",
    oid: int = 1,
    tid: int = 1,
    is_maker: bool = True,
    timestamp_ms: int = 1700_000_000_000,
    symbol: str = "ETH",
) -> FillEntry:
    return FillEntry(
        timestamp_ms=timestamp_ms,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        price=Decimal(price),
        size=Decimal(size),
        fee=Decimal(fee),
        oid=oid,
        is_maker=is_maker,
        tid=tid,
    )


def _make_hl_mock(
    position_size: Decimal = Decimal("0"),
    entry_price: Decimal = Decimal("0"),
) -> MagicMock:
    hl = MagicMock()
    hl._config = HyperliquidConfig(
        network="testnet",
        api_wallet_address="0x" + "1" * 40,
    )
    hl.get_position = AsyncMock(
        return_value=Position(
            symbol="ETH",
            size=position_size,
            entry_price=entry_price,
            unrealized_pnl=Decimal("0"),
        )
    )
    return hl


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[StateStore]:
    s = StateStore(tmp_path / "inv.db")
    await s.open()
    await s.migrate()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def hl_flat() -> MagicMock:
    return _make_hl_mock()


# ---------- parse_user_fill ----------


class TestParseUserFill:
    def test_valid(self) -> None:
        f = parse_user_fill({
            "coin": "ETH",
            "side": "B",
            "px": "1850.5",
            "sz": "0.02",
            "fee": "0.0028",
            "time": 1700_000_000_000,
            "oid": 42,
            "tid": 99,
            "crossed": False,
        })
        assert f is not None
        assert f.symbol == "ETH"
        assert f.side == "bid"
        assert f.price == Decimal("1850.5")
        assert f.tid == 99
        assert f.is_maker is True

    def test_taker(self) -> None:
        f = parse_user_fill({
            "coin": "ETH",
            "side": "A",
            "px": "1850",
            "sz": "0.01",
            "time": 1,
            "oid": 1,
            "tid": 2,
            "crossed": True,
        })
        assert f is not None
        assert f.side == "ask"
        assert f.is_maker is False

    def test_symbol_filter(self) -> None:
        d = {"coin": "BTC", "side": "B", "px": "60000", "sz": "0.01", "time": 1, "oid": 1, "tid": 1}
        assert parse_user_fill(d, symbol_filter="BTC") is not None
        assert parse_user_fill(d, symbol_filter="ETH") is None

    def test_invalid_returns_none(self) -> None:
        assert parse_user_fill(None) is None
        assert parse_user_fill({}) is None
        assert parse_user_fill({"coin": "ETH"}) is None


# ---------- FIFO PnL & lots ----------


async def test_starts_flat(hl_flat: MagicMock, store: StateStore) -> None:
    inv = InventoryManager("ETH", Decimal("0.1"), hl_flat, store)
    assert inv.current_position == Decimal("0")
    assert inv.avg_entry_price == Decimal("0")
    assert inv.realized_pnl == Decimal("0")
    assert inv.is_flat
    assert not inv.is_long
    assert not inv.is_short


async def test_single_buy_creates_long_lot(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    await inv.apply_fill(_fill(side="bid", price="1850", size="0.05", tid=1))
    assert inv.current_position == Decimal("0.05")
    assert inv.avg_entry_price == Decimal("1850")
    assert inv.is_long
    assert inv.realized_pnl == Decimal("0")


async def test_two_buys_weighted_average(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    await inv.apply_fill(_fill(side="bid", price="1800", size="0.1", tid=1))
    await inv.apply_fill(_fill(side="bid", price="1900", size="0.1", tid=2))
    # Weighted avg: (1800*0.1 + 1900*0.1)/0.2 = 1850
    assert inv.current_position == Decimal("0.2")
    assert inv.avg_entry_price == Decimal("1850")


async def test_three_buys_unequal_weighted_average(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    await inv.apply_fill(_fill(side="bid", price="1000", size="0.1", tid=1))
    await inv.apply_fill(_fill(side="bid", price="2000", size="0.3", tid=2))
    # avg = (1000*0.1 + 2000*0.3)/0.4 = 700/0.4 = 1750
    assert inv.avg_entry_price == Decimal("1750")


async def test_partial_close_realizes_pnl(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    # Buy 0.1 @ 1800
    await inv.apply_fill(_fill(side="bid", price="1800", size="0.1", tid=1, fee="0"))
    # Sell 0.04 @ 1850 → realize (1850-1800)*0.04 = 2.0
    await inv.apply_fill(_fill(side="ask", price="1850", size="0.04", tid=2, fee="0"))
    assert inv.current_position == Decimal("0.06")
    assert inv.realized_pnl == Decimal("2.0")
    assert inv.avg_entry_price == Decimal("1800")  # remaining lot


async def test_full_close_returns_to_flat(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    await inv.apply_fill(_fill(side="bid", price="1800", size="0.1", tid=1, fee="0"))
    await inv.apply_fill(_fill(side="ask", price="1900", size="0.1", tid=2, fee="0"))
    assert inv.is_flat
    assert inv.realized_pnl == Decimal("10.0")
    assert inv.avg_entry_price == Decimal("0")


async def test_fifo_matching_across_multiple_lots(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    await inv.apply_fill(_fill(side="bid", price="1000", size="0.1", tid=1, fee="0"))
    await inv.apply_fill(_fill(side="bid", price="2000", size="0.1", tid=2, fee="0"))
    # Sell 0.15 @ 1500 → close 0.1 @ 1000 (PnL +50), then 0.05 @ 2000 (PnL -25) → +25
    await inv.apply_fill(_fill(side="ask", price="1500", size="0.15", tid=3, fee="0"))
    assert inv.current_position == Decimal("0.05")
    assert inv.realized_pnl == Decimal("25.0")
    assert inv.avg_entry_price == Decimal("2000")


async def test_sell_more_than_long_flips_to_short(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    await inv.apply_fill(_fill(side="bid", price="1800", size="0.1", tid=1, fee="0"))
    # Sell 0.15 @ 1900 → close 0.1 @ 1800 (PnL +10), then short 0.05 @ 1900
    await inv.apply_fill(_fill(side="ask", price="1900", size="0.15", tid=2, fee="0"))
    assert inv.current_position == Decimal("-0.05")
    assert inv.is_short
    assert inv.realized_pnl == Decimal("10.0")
    assert inv.avg_entry_price == Decimal("1900")


async def test_short_position_buy_realizes_pnl(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    # Sell 0.1 @ 1900 → short
    await inv.apply_fill(_fill(side="ask", price="1900", size="0.1", tid=1, fee="0"))
    assert inv.is_short
    # Buy back 0.06 @ 1850 → realize (1900-1850)*0.06 = 3.0
    await inv.apply_fill(_fill(side="bid", price="1850", size="0.06", tid=2, fee="0"))
    assert inv.current_position == Decimal("-0.04")
    assert inv.realized_pnl == Decimal("3.0")


async def test_fees_accumulate(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    await inv.apply_fill(_fill(tid=1, fee="0.001"))
    await inv.apply_fill(_fill(tid=2, fee="0.002"))
    assert inv.total_fees == Decimal("0.003")


async def test_apply_fill_idempotent_by_tid(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    fill = _fill(tid=42, side="bid", price="1800", size="0.05")
    assert await inv.apply_fill(fill) is True
    assert await inv.apply_fill(fill) is False  # dedup'd
    assert inv.current_position == Decimal("0.05")


# ---------- Skew & quoting helpers ----------


async def test_skew_calculation(hl_flat: MagicMock, store: StateStore) -> None:
    inv = InventoryManager("ETH", Decimal("0.1"), hl_flat, store)
    await inv.apply_fill(_fill(side="bid", size="0.05", tid=1))
    assert inv.inventory_skew == Decimal("0.5")


async def test_skew_clamped_to_one(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("0.1"), hl_flat, store)
    await inv.apply_fill(_fill(side="bid", size="0.5", tid=1))  # over max
    assert inv.inventory_skew == Decimal("1")


async def test_skew_negative_short(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("0.1"), hl_flat, store)
    await inv.apply_fill(_fill(side="ask", size="0.05", tid=1))
    assert inv.inventory_skew == Decimal("-0.5")


async def test_can_quote_blocks_at_max(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("0.1"), hl_flat, store)
    assert inv.can_quote_bid()
    assert inv.can_quote_ask()
    await inv.apply_fill(_fill(side="bid", size="0.1", tid=1))
    # At max long → no more bids
    assert not inv.can_quote_bid()
    assert inv.can_quote_ask()


async def test_can_quote_blocks_at_max_short(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("0.1"), hl_flat, store)
    await inv.apply_fill(_fill(side="ask", size="0.1", tid=1))
    assert inv.can_quote_bid()
    assert not inv.can_quote_ask()


async def test_position_value_usd(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    await inv.apply_fill(_fill(side="bid", price="1800", size="0.05", tid=1))
    assert inv.position_value_usd() == Decimal("90.00")  # 0.05 * 1800
    assert inv.position_value_usd(Decimal("2000")) == Decimal("100.00")


# ---------- Reconcile ----------


async def test_reconcile_match_no_change(
    store: StateStore,
) -> None:
    hl = _make_hl_mock(position_size=Decimal("0"), entry_price=Decimal("0"))
    inv = InventoryManager("ETH", Decimal("1"), hl, store)
    ok = await inv.reconcile()
    assert ok is True
    assert inv.current_position == Decimal("0")


async def test_reconcile_corrects_mismatch(store: StateStore) -> None:
    # Memory thinks flat, API says we have 0.05 long @ 1850.
    hl = _make_hl_mock(
        position_size=Decimal("0.05"),
        entry_price=Decimal("1850"),
    )
    inv = InventoryManager("ETH", Decimal("1"), hl, store)
    ok = await inv.reconcile()
    assert ok is False  # mismatch, corrected
    assert inv.current_position == Decimal("0.05")
    assert inv.avg_entry_price == Decimal("1850")


async def test_reconcile_corrects_short_mismatch(
    store: StateStore,
) -> None:
    hl = _make_hl_mock(
        position_size=Decimal("-0.05"),
        entry_price=Decimal("1900"),
    )
    inv = InventoryManager("ETH", Decimal("1"), hl, store)
    ok = await inv.reconcile()
    assert ok is False
    assert inv.current_position == Decimal("-0.05")
    assert inv.is_short


async def test_reconcile_within_tolerance(store: StateStore) -> None:
    hl = _make_hl_mock(
        position_size=Decimal("1e-10"),  # tiny diff, within tolerance
        entry_price=Decimal("1850"),
    )
    inv = InventoryManager(
        "ETH", Decimal("1"), hl, store,
        reconcile_tolerance=Decimal("1e-9"),
    )
    ok = await inv.reconcile()
    assert ok is True


# ---------- Callbacks & state store integration ----------


async def test_on_inventory_change_called(
    hl_flat: MagicMock, store: StateStore
) -> None:
    snapshots: list[InventorySnapshot] = []

    def on_change(snap: InventorySnapshot) -> None:
        snapshots.append(snap)

    inv = InventoryManager(
        "ETH", Decimal("1"), hl_flat, store, on_inventory_change=on_change
    )
    await inv.apply_fill(_fill(tid=1))
    assert len(snapshots) == 1
    assert snapshots[0].position == Decimal("0.1")


async def test_on_inventory_change_async_callback(
    hl_flat: MagicMock, store: StateStore
) -> None:
    snapshots: list[InventorySnapshot] = []

    async def on_change(snap: InventorySnapshot) -> None:
        snapshots.append(snap)

    inv = InventoryManager(
        "ETH", Decimal("1"), hl_flat, store, on_inventory_change=on_change
    )
    await inv.apply_fill(_fill(tid=1))
    assert len(snapshots) == 1


async def test_fill_persisted_to_state_store(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    await inv.apply_fill(_fill(tid=42, price="1850", size="0.05"))
    await store.flush()
    fills = await store.get_recent_fills()
    assert len(fills) == 1
    assert fills[0].id == "42"
    assert fills[0].price == Decimal("1850")
    assert fills[0].size == Decimal("0.05")


async def test_idempotent_persistence(
    hl_flat: MagicMock, store: StateStore
) -> None:
    inv = InventoryManager("ETH", Decimal("1"), hl_flat, store)
    fill = _fill(tid=42)
    await inv.apply_fill(fill)
    await inv.apply_fill(fill)  # no-op
    await store.flush()
    fills = await store.get_recent_fills()
    assert len(fills) == 1


# ---------- WebSocket message handling ----------


class FakeWS:
    def __init__(self) -> None:
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    async def send(self, msg: str) -> None:
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(msg)

    async def recv(self) -> str:
        if self.closed:
            raise ConnectionError("closed")
        item = await self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        return str(item)

    async def close(self) -> None:
        self.closed = True

    def push(self, msg: dict[str, Any]) -> None:
        self._inbox.put_nowait(json.dumps(msg))


async def _wait_until(
    predicate: Any, deadline_s: float = 1.0, interval: float = 0.01
) -> bool:
    end = asyncio.get_event_loop().time() + deadline_s
    while asyncio.get_event_loop().time() < end:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


async def test_ws_subscribe_message_sent(
    hl_flat: MagicMock, store: StateStore
) -> None:
    ws = FakeWS()

    async def factory(_url: str) -> Any:
        return ws

    inv = InventoryManager(
        "ETH", Decimal("1"), hl_flat, store,
        connect_factory=factory,
        silence_timeout_s=2.0,
    )
    await inv.start()
    assert await _wait_until(lambda: len(ws.sent) >= 1)
    parsed = json.loads(ws.sent[0])
    assert parsed["subscription"]["type"] == "userFills"
    assert parsed["subscription"]["user"] == "0x" + "1" * 40
    await inv.stop()


async def test_ws_snapshot_marks_seen_no_apply(
    hl_flat: MagicMock, store: StateStore
) -> None:
    ws = FakeWS()

    async def factory(_url: str) -> Any:
        return ws

    inv = InventoryManager(
        "ETH", Decimal("1"), hl_flat, store,
        connect_factory=factory,
        silence_timeout_s=2.0,
    )
    await inv.start()
    # Snapshot with one fill — should NOT apply (position from API was flat).
    ws.push({
        "channel": "userFills",
        "data": {
            "isSnapshot": True,
            "user": "0x" + "1" * 40,
            "fills": [
                {"coin": "ETH", "side": "B", "px": "1800", "sz": "0.05",
                 "fee": "0", "time": 1, "oid": 1, "tid": 100, "crossed": False},
            ],
        },
    })
    await asyncio.sleep(0.1)
    assert inv.current_position == Decimal("0")
    assert 100 in inv._seen_tids
    # If incremental fill with same tid arrives, it gets dedup'd
    ws.push({
        "channel": "userFills",
        "data": {
            "isSnapshot": False,
            "user": "0x" + "1" * 40,
            "fills": [
                {"coin": "ETH", "side": "B", "px": "1800", "sz": "0.05",
                 "fee": "0", "time": 1, "oid": 1, "tid": 100, "crossed": False},
            ],
        },
    })
    await asyncio.sleep(0.1)
    assert inv.current_position == Decimal("0")  # dedup'd
    await inv.stop()


async def test_ws_incremental_fill_applied(
    hl_flat: MagicMock, store: StateStore
) -> None:
    ws = FakeWS()

    async def factory(_url: str) -> Any:
        return ws

    inv = InventoryManager(
        "ETH", Decimal("1"), hl_flat, store,
        connect_factory=factory,
        silence_timeout_s=2.0,
    )
    await inv.start()
    # No snapshot first — incremental message goes through
    ws.push({
        "channel": "userFills",
        "data": {
            "isSnapshot": False,
            "user": "0x" + "1" * 40,
            "fills": [
                {"coin": "ETH", "side": "B", "px": "1800", "sz": "0.07",
                 "fee": "0", "time": 1, "oid": 1, "tid": 200, "crossed": False},
            ],
        },
    })
    assert await _wait_until(lambda: inv.current_position == Decimal("0.07"))
    await inv.stop()


async def test_ws_other_symbol_ignored(
    hl_flat: MagicMock, store: StateStore
) -> None:
    ws = FakeWS()

    async def factory(_url: str) -> Any:
        return ws

    inv = InventoryManager(
        "ETH", Decimal("1"), hl_flat, store,
        connect_factory=factory,
        silence_timeout_s=2.0,
    )
    await inv.start()
    ws.push({
        "channel": "userFills",
        "data": {
            "isSnapshot": False,
            "user": "0x" + "1" * 40,
            "fills": [
                {"coin": "BTC", "side": "B", "px": "60000", "sz": "0.001",
                 "fee": "0", "time": 1, "oid": 1, "tid": 300, "crossed": False},
            ],
        },
    })
    await asyncio.sleep(0.1)
    assert inv.current_position == Decimal("0")
    await inv.stop()


async def test_initial_reconcile_on_start(store: StateStore) -> None:
    hl = _make_hl_mock(
        position_size=Decimal("0.03"),
        entry_price=Decimal("1820"),
    )
    ws = FakeWS()

    async def factory(_url: str) -> Any:
        return ws

    inv = InventoryManager(
        "ETH", Decimal("1"), hl, store,
        connect_factory=factory,
        silence_timeout_s=2.0,
    )
    await inv.start()
    # After start, reconcile should have set position to API value.
    assert inv.current_position == Decimal("0.03")
    assert inv.avg_entry_price == Decimal("1820")
    await inv.stop()
