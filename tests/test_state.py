"""Tests for src.state.StateStore."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from src.state import (
    EventRecord,
    FillRecord,
    OrderRecord,
    PnLSnapshot,
    StateStore,
)


def _make_fill(
    *,
    id: str = "f1",
    timestamp_ms: int = 1000,
    symbol: str = "ETH",
    side: str = "bid",
    price: Decimal = Decimal("1850.55"),
    size: Decimal = Decimal("0.02"),
    fee: Decimal = Decimal("0.0001"),
    order_id: str = "o1",
    is_maker: bool = True,
) -> FillRecord:
    return FillRecord(
        id=id,
        timestamp_ms=timestamp_ms,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        price=price,
        size=size,
        fee=fee,
        order_id=order_id,
        is_maker=is_maker,
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[StateStore]:
    db = tmp_path / "test.db"
    s = StateStore(db)
    await s.open()
    await s.migrate()
    try:
        yield s
    finally:
        await s.close()


async def test_migrate_creates_all_tables(store: StateStore) -> None:
    assert store._conn is not None
    async with store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cur:
        rows = await cur.fetchall()
    names = {r[0] for r in rows}
    assert {"fills", "orders", "pnl_snapshots", "events"}.issubset(names)


async def test_wal_mode_enabled(store: StateStore) -> None:
    assert store._conn is not None
    async with store._conn.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0].lower() == "wal"


async def test_record_and_read_fill(store: StateStore) -> None:
    fill = _make_fill()
    await store.record_fill(fill)
    await store.flush()
    fills = await store.get_recent_fills()
    assert len(fills) == 1
    assert fills[0] == fill


async def test_decimal_precision_preserved(store: StateStore) -> None:
    high_precision = Decimal("0.00012345")
    big_price = Decimal("1234.56789012345")
    fill = _make_fill(
        id="precision-test",
        price=big_price,
        size=high_precision,
        fee=Decimal("-0.000001"),
    )
    await store.record_fill(fill)
    await store.flush()
    [back] = await store.get_recent_fills()
    assert back.size == high_precision
    assert back.price == big_price
    assert back.fee == Decimal("-0.000001")
    # Sanity: round-trip via str must be identical (ei float-precision-tappiota).
    assert str(back.size) == "0.00012345"


async def test_filter_by_symbol(store: StateStore) -> None:
    await store.record_fill(_make_fill(id="eth-1", symbol="ETH"))
    await store.record_fill(_make_fill(id="btc-1", symbol="BTC", timestamp_ms=2000))
    await store.flush()
    eth = await store.get_recent_fills(symbol="ETH")
    btc = await store.get_recent_fills(symbol="BTC")
    assert [f.id for f in eth] == ["eth-1"]
    assert [f.id for f in btc] == ["btc-1"]


async def test_recent_fills_ordered_newest_first(store: StateStore) -> None:
    for i in range(5):
        await store.record_fill(_make_fill(id=f"f{i}", timestamp_ms=1000 + i))
    await store.flush()
    fills = await store.get_recent_fills()
    # Newest first
    assert [f.id for f in fills] == ["f4", "f3", "f2", "f1", "f0"]


async def test_recent_fills_limit(store: StateStore) -> None:
    for i in range(20):
        await store.record_fill(_make_fill(id=f"f{i}", timestamp_ms=1000 + i))
    await store.flush()
    fills = await store.get_recent_fills(limit=3)
    assert len(fills) == 3


async def test_concurrent_writes_no_corruption(store: StateStore) -> None:
    n = 100

    async def write_one(i: int) -> None:
        await store.record_fill(
            _make_fill(
                id=f"f{i:03d}",
                timestamp_ms=10_000 + i,
                price=Decimal(f"{1000 + i}.{i:02d}"),
                size=Decimal("0.01"),
                order_id=f"o{i}",
            )
        )

    await asyncio.gather(*[write_one(i) for i in range(n)])
    await store.flush()

    fills = await store.get_recent_fills(limit=n)
    assert len(fills) == n
    ids = {f.id for f in fills}
    assert ids == {f"f{i:03d}" for i in range(n)}
    # Hinta-Decimalit ovat yksilöllisiä → varmista ettei kirjoitukset ole yli/alikirjoittaneet toisiaan.
    prices = {str(f.price) for f in fills}
    assert len(prices) == n


async def test_record_order_and_get_open(store: StateStore) -> None:
    placed = OrderRecord(
        id="ord-1",
        client_order_id="cli-1",
        timestamp_ms=1000,
        symbol="ETH",
        side="bid",
        price=Decimal("1850.50"),
        size=Decimal("0.02"),
        status="placed",
    )
    filled = OrderRecord(
        id="ord-2",
        client_order_id="cli-2",
        timestamp_ms=1100,
        symbol="ETH",
        side="ask",
        price=Decimal("1851.00"),
        size=Decimal("0.02"),
        status="filled",
    )
    await store.record_order(placed)
    await store.record_order(filled)
    await store.flush()

    open_orders = await store.get_open_orders_db()
    assert len(open_orders) == 1
    assert open_orders[0].id == "ord-1"
    assert open_orders[0].price == Decimal("1850.50")


async def test_order_upsert_overwrites_status(store: StateStore) -> None:
    o = OrderRecord(
        id="ord-1",
        client_order_id="cli-1",
        timestamp_ms=1000,
        symbol="ETH",
        side="bid",
        price=Decimal("1850.50"),
        size=Decimal("0.02"),
        status="placed",
    )
    await store.record_order(o)
    await store.flush()
    assert len(await store.get_open_orders_db()) == 1

    cancelled = OrderRecord(**{**o.__dict__, "status": "cancelled", "cancel_timestamp_ms": 2000})
    await store.record_order(cancelled)
    await store.flush()
    assert len(await store.get_open_orders_db()) == 0


async def test_pnl_snapshot_roundtrip(store: StateStore) -> None:
    snap = PnLSnapshot(
        timestamp_ms=1700_000_000_000,
        realized_pnl=Decimal("12.345"),
        unrealized_pnl=Decimal("-3.21"),
        inventory=Decimal("0.0123"),
        capital=Decimal("509.135"),
        spread_pnl=Decimal("15.55"),
        rebate_earned=Decimal("0"),
    )
    await store.record_pnl_snapshot(snap)
    await store.flush()
    [back] = await store.get_pnl_history()
    assert back == snap


async def test_pnl_snapshot_since_filter(store: StateStore) -> None:
    for ts in (1000, 2000, 3000, 4000):
        await store.record_pnl_snapshot(
            PnLSnapshot(
                timestamp_ms=ts,
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                inventory=Decimal("0"),
                capital=Decimal("500"),
                spread_pnl=Decimal("0"),
                rebate_earned=Decimal("0"),
            )
        )
    await store.flush()
    recent = await store.get_pnl_history(since_ms=2500)
    assert {s.timestamp_ms for s in recent} == {3000, 4000}


async def test_event_with_data_json(store: StateStore) -> None:
    e = EventRecord(
        timestamp_ms=1000,
        level="warning",
        component="risk",
        message="vol halt",
        data={"realized_vol": 0.025, "threshold": 0.02},
    )
    await store.record_event(e)
    await store.flush()
    [back] = await store.get_recent_events()
    assert back == e


async def test_event_filter_by_level(store: StateStore) -> None:
    await store.record_event(
        EventRecord(timestamp_ms=1000, level="info", component="x", message="ok")
    )
    await store.record_event(
        EventRecord(timestamp_ms=1100, level="kill", component="risk", message="dead")
    )
    await store.record_event(
        EventRecord(timestamp_ms=1200, level="warning", component="x", message="meh")
    )
    await store.flush()
    kills = await store.get_recent_events(level="kill")
    assert [e.message for e in kills] == ["dead"]


async def test_close_is_idempotent(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "x.db")
    await s.open()
    await s.migrate()
    await s.close()
    await s.close()  # ei saa kaatua


async def test_record_after_close_raises(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "x.db")
    await s.open()
    await s.migrate()
    await s.close()
    with pytest.raises(RuntimeError):
        await s.record_fill(_make_fill())


async def test_persistence_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "persistent.db"
    s1 = StateStore(db)
    await s1.open()
    await s1.migrate()
    await s1.record_fill(_make_fill(id="persistent-1", price=Decimal("9999.99")))
    await s1.close()

    s2 = StateStore(db)
    await s2.open()
    fills = await s2.get_recent_fills()
    await s2.close()
    assert len(fills) == 1
    assert fills[0].id == "persistent-1"
    assert fills[0].price == Decimal("9999.99")


async def test_async_context_manager(tmp_path: Path) -> None:
    db = tmp_path / "ctx.db"
    async with StateStore(db) as s:
        await s.record_fill(_make_fill(id="ctx-1"))
        await s.flush()
        fills = await s.get_recent_fills()
    assert len(fills) == 1

    # Yhteyden tulisi olla suljettu — verifioidaan että tiedosto on käytettävissä.
    async with (
        aiosqlite.connect(db) as conn,
        conn.execute("SELECT COUNT(*) FROM fills") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1
