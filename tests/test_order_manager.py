"""Tests for src.order_manager."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.hl_client import (
    FillEntry,
    HLNetworkError,
    HLOrderRejectedError,
    OpenOrder,
    OrderResult,
)
from src.order_manager import (
    OrderInfo,
    OrderManager,
    generate_cloid,
)
from src.quote_engine import Quote, QuoteSet
from src.state import StateStore

# ---------- Fixtures / helpers ----------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[StateStore]:
    s = StateStore(tmp_path / "om.db")
    await s.open()
    await s.migrate()
    try:
        yield s
    finally:
        await s.close()


def _hl_mock() -> MagicMock:
    hl = MagicMock()
    hl.place_order = AsyncMock()
    hl.cancel_order = AsyncMock(return_value=True)
    hl.cancel_all_orders = AsyncMock(return_value=0)
    hl.get_open_orders = AsyncMock(return_value=[])
    return hl


def _resting(oid: int) -> OrderResult:
    return OrderResult(
        status="resting", oid=oid, cloid=None,
        avg_price=None, filled_size=None,
    )


def _filled(oid: int, price: str = "1850.5", size: str = "0.02") -> OrderResult:
    return OrderResult(
        status="filled", oid=oid, cloid=None,
        avg_price=Decimal(price), filled_size=Decimal(size),
    )


def _quoteset(
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
    *,
    mid: str = "1850.5",
) -> QuoteSet:
    return QuoteSet(
        bids=tuple(Quote(Decimal(p), Decimal(s)) for p, s in (bids or [])),
        asks=tuple(Quote(Decimal(p), Decimal(s)) for p, s in (asks or [])),
        mid=Decimal(mid),
        effective_spread_bps=5.0,
        skew_adjustment_bps=0.0,
    )


def _make_om(
    hl: MagicMock,
    store: StateStore,
    *,
    price_tolerance: Decimal = Decimal("0.0001"),
    max_order_age_seconds: int = 30,
) -> OrderManager:
    return OrderManager(
        hl, store, "ETH",
        price_tolerance=price_tolerance,
        max_order_age_seconds=max_order_age_seconds,
    )


# ---------- cloid uniqueness ----------


class TestCloid:
    def test_cloid_format(self) -> None:
        cl = generate_cloid()
        assert cl.startswith("0x")
        assert len(cl) == 34  # "0x" + 32 hex chars
        # All hex
        int(cl, 16)  # raises if not hex

    def test_cloid_unique_across_many_calls(self) -> None:
        # Bypass store for this test — only generation.
        om = OrderManager.__new__(OrderManager)
        om._cloid_counter = 0
        cloids = {om._generate_cloid() for _ in range(1000)}
        assert len(cloids) == 1000

    def test_cloid_unique_same_ms(self) -> None:
        # Patch time to return same ms — counter+random should still differ.
        om = OrderManager.__new__(OrderManager)
        om._cloid_counter = 0
        import time as time_mod
        original = time_mod.time
        try:
            time_mod.time = lambda: 1700_000_000.500
            cloids = {om._generate_cloid() for _ in range(100)}
        finally:
            time_mod.time = original
        assert len(cloids) == 100


# ---------- update_quotes happy path ----------


async def test_empty_state_places_all(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.side_effect = [_resting(i) for i in range(1, 11)]
    om = _make_om(hl, store)
    target = _quoteset(
        bids=[("1849.5", "0.02"), ("1849.4", "0.02"),
              ("1849.3", "0.02"), ("1849.2", "0.02"), ("1849.1", "0.02")],
        asks=[("1850.5", "0.02"), ("1850.6", "0.02"),
              ("1850.7", "0.02"), ("1850.8", "0.02"), ("1850.9", "0.02")],
    )
    res = await om.update_quotes(target)
    assert res.placed == 10
    assert res.cancelled == 0
    assert res.kept == 0
    assert om.active_count == 10
    assert hl.place_order.call_count == 10


async def test_no_change_keeps_all(store: StateStore) -> None:
    hl = _hl_mock()
    # First update places 5 bids
    hl.place_order.side_effect = [_resting(i) for i in range(1, 6)]
    om = _make_om(hl, store)
    bids = [(f"184{9-i}.5", "0.02") for i in range(5)]  # 1849.5, 1848.5, ...
    target = _quoteset(bids=bids)
    await om.update_quotes(target)
    place_count_after_first = hl.place_order.call_count
    # Second update with same target → should keep all, no API calls.
    res = await om.update_quotes(target)
    assert res.placed == 0
    assert res.cancelled == 0
    assert res.kept == 5
    assert hl.place_order.call_count == place_count_after_first


async def test_three_prices_changed(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.side_effect = [_resting(i) for i in range(1, 11)]
    om = _make_om(hl, store)
    initial = _quoteset(bids=[
        ("1849.5", "0.02"), ("1849.4", "0.02"),
        ("1849.3", "0.02"), ("1849.2", "0.02"), ("1849.1", "0.02"),
    ])
    await om.update_quotes(initial)
    # Now 3 prices changed (last 3)
    new = _quoteset(bids=[
        ("1849.5", "0.02"), ("1849.4", "0.02"),
        ("1850.0", "0.02"), ("1849.9", "0.02"), ("1849.8", "0.02"),
    ])
    hl.cancel_order.reset_mock()
    hl.place_order.reset_mock()
    res = await om.update_quotes(new)
    assert res.kept == 2
    assert res.cancelled == 3
    assert res.placed == 3


async def test_within_tolerance_kept(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(1)
    om = _make_om(hl, store, price_tolerance=Decimal("0.05"))
    # Place at 1849.5
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    hl.cancel_order.reset_mock()
    hl.place_order.reset_mock()
    # Target at 1849.52 — within 0.05 tolerance
    res = await om.update_quotes(_quoteset(bids=[("1849.52", "0.02")]))
    assert res.kept == 1
    assert res.cancelled == 0
    assert res.placed == 0
    assert hl.cancel_order.call_count == 0
    assert hl.place_order.call_count == 0


async def test_outside_tolerance_replaced(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.side_effect = [_resting(1), _resting(2)]
    om = _make_om(hl, store, price_tolerance=Decimal("0.05"))
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    hl.cancel_order.reset_mock()
    res = await om.update_quotes(_quoteset(bids=[("1849.4", "0.02")]))  # 0.1 away
    assert res.kept == 0
    assert res.cancelled == 1
    assert res.placed == 1


# ---------- post_only flag passed through ----------


async def test_place_uses_post_only(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(1)
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    kwargs = hl.place_order.call_args.kwargs
    # call signature: place_order(symbol, side, price, size, post_only=, client_order_id=)
    assert kwargs.get("post_only") is True


async def test_place_passes_cloid(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(1)
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    cloid = hl.place_order.call_args.kwargs["client_order_id"]
    assert cloid is not None
    assert cloid.startswith("0x")
    assert len(cloid) == 34


# ---------- cancel_all ----------


async def test_cancel_all(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.side_effect = [_resting(i) for i in range(1, 4)]
    hl.cancel_all_orders.return_value = 3
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[
        ("1849.5", "0.02"), ("1849.4", "0.02"), ("1849.3", "0.02"),
    ]))
    assert om.active_count == 3
    n = await om.cancel_all()
    assert n == 3
    assert om.active_count == 0
    hl.cancel_all_orders.assert_called_once_with("ETH")


async def test_cancel_all_when_empty(store: StateStore) -> None:
    hl = _hl_mock()
    hl.cancel_all_orders.return_value = 0
    om = _make_om(hl, store)
    n = await om.cancel_all()
    assert n == 0


# ---------- Suppressed quoteset ----------


async def test_suppressed_quoteset_cancels_active(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.side_effect = [_resting(1)]
    hl.cancel_all_orders.return_value = 1
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    suppressed = QuoteSet(
        bids=(), asks=(), mid=Decimal("1850"),
        effective_spread_bps=100.0, skew_adjustment_bps=0.0,
        suppressed_reason="vol halt",
    )
    res = await om.update_quotes(suppressed)
    assert res.suppressed
    assert res.suppressed_reason == "vol halt"
    assert res.placed == 0
    assert res.cancelled == 1
    assert om.active_count == 0


# ---------- Race conditions: not-found cancel ----------


async def test_cancel_order_not_found_treated_as_cancelled(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(1)
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    # New target → cancel old, place new
    hl.cancel_order.side_effect = HLOrderRejectedError("Order not found")
    hl.place_order.return_value = _resting(2)
    res = await om.update_quotes(_quoteset(bids=[("1850.0", "0.02")]))
    # Even though cancel raised, OrderManager treats it as cancelled.
    assert res.cancelled == 1
    assert res.placed == 1


async def test_cancel_network_error_counts_as_failed(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(1)
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    hl.cancel_order.side_effect = HLNetworkError("conn refused")
    hl.place_order.return_value = _resting(2)
    res = await om.update_quotes(_quoteset(bids=[("1850.0", "0.02")]))
    assert res.failed_cancels == 1


# ---------- Place rejected ----------


async def test_place_rejected_returns_failed(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.side_effect = HLOrderRejectedError("post only would have crossed")
    om = _make_om(hl, store)
    res = await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    assert res.placed == 0
    assert res.failed_placements == 1
    assert om.active_count == 0


async def test_place_rejected_in_response_returns_failed(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = OrderResult(
        status="rejected", oid=None, cloid=None,
        avg_price=None, filled_size=None,
        error="Post only crossed",
    )
    om = _make_om(hl, store)
    res = await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    assert res.placed == 0
    assert res.failed_placements == 1


async def test_place_filled_immediately_not_in_active(store: StateStore) -> None:
    """Jos placen vastauksena tulee 'filled' (esim. ei post-only), älä jää active:in."""
    hl = _hl_mock()
    hl.place_order.return_value = _filled(42)
    om = _make_om(hl, store)
    res = await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    # Spec is open here — we treat immediate-fill as not-active (it's gone).
    assert res.placed == 0  # filled, not resting
    assert om.active_count == 0


# ---------- Fill handling ----------


async def test_handle_fill_removes_from_active(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(42)
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    assert 42 in om.active_orders

    fill = FillEntry(
        timestamp_ms=int((om.active_orders[42].placed_at_ms) + 100),
        symbol="ETH", side="bid", price=Decimal("1849.5"),
        size=Decimal("0.02"), fee=Decimal("0.0001"),
        oid=42, is_maker=True, tid=999,
    )
    info = await om.handle_fill(fill)
    assert info is not None
    assert info.oid == 42
    assert om.active_count == 0


async def test_handle_fill_unknown_oid(store: StateStore) -> None:
    hl = _hl_mock()
    om = _make_om(hl, store)
    fill = FillEntry(
        timestamp_ms=1, symbol="ETH", side="bid", price=Decimal("1850"),
        size=Decimal("0.01"), fee=Decimal("0"), oid=9999, is_maker=True, tid=1,
    )
    info = await om.handle_fill(fill)
    assert info is None  # not in active, not an error


async def test_handle_fill_other_symbol_ignored(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(42)
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    fill = FillEntry(
        timestamp_ms=1, symbol="BTC", side="bid", price=Decimal("60000"),
        size=Decimal("0.001"), fee=Decimal("0"), oid=42, is_maker=True, tid=1,
    )
    info = await om.handle_fill(fill)
    assert info is None
    # ETH order still active
    assert om.active_count == 1


async def test_on_fill_callback_includes_latency(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(42)
    captured: list[tuple[FillEntry, OrderInfo | None, float]] = []

    def on_fill(f: FillEntry, info: OrderInfo | None, latency: float) -> None:
        captured.append((f, info, latency))

    om = OrderManager(hl, store, "ETH", on_fill=on_fill)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    placed_at = om.active_orders[42].placed_at_ms
    fill = FillEntry(
        timestamp_ms=placed_at + 250, symbol="ETH", side="bid",
        price=Decimal("1849.5"), size=Decimal("0.02"),
        fee=Decimal("0"), oid=42, is_maker=True, tid=1,
    )
    await om.handle_fill(fill)
    assert len(captured) == 1
    assert captured[0][2] == pytest.approx(250.0)


# ---------- Reconcile ----------


async def test_reconcile_pulls_open_orders_from_api(store: StateStore) -> None:
    hl = _hl_mock()
    hl.get_open_orders.return_value = [
        OpenOrder(
            oid=10, cloid="0xabc", symbol="ETH", side="bid",
            price=Decimal("1849.5"), size=Decimal("0.02"),
            timestamp_ms=1700_000_000_000,
        ),
        OpenOrder(
            oid=11, cloid=None, symbol="ETH", side="ask",
            price=Decimal("1850.5"), size=Decimal("0.02"),
            timestamp_ms=1700_000_000_000,
        ),
    ]
    om = _make_om(hl, store)
    n = await om.reconcile()
    assert n == 2
    assert om.active_count == 2
    assert 10 in om.active_orders
    assert 11 in om.active_orders


async def test_reconcile_removes_stale_memory(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(99)
    hl.get_open_orders.return_value = []  # API says no orders
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    assert om.active_count == 1
    await om.reconcile()
    # Memory had 99, API has none → memory cleared
    assert om.active_count == 0


# ---------- Stale order cleanup ----------


async def test_cancel_stale_orders(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(42)
    om = _make_om(hl, store, max_order_age_seconds=1)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    # Force order to look old.
    info = om.active_orders[42]
    om._active[42] = OrderInfo(
        oid=info.oid, cloid=info.cloid, symbol=info.symbol,
        side=info.side, price=info.price, size=info.size,
        placed_at_ms=info.placed_at_ms - 5000,  # 5s old
    )
    n = await om.cancel_stale_orders()
    assert n == 1
    assert om.active_count == 0
    hl.cancel_order.assert_called_once()


async def test_no_stale_orders_when_fresh(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(42)
    om = _make_om(hl, store, max_order_age_seconds=60)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    n = await om.cancel_stale_orders()
    assert n == 0
    assert om.active_count == 1


# ---------- State persistence ----------


async def test_placed_orders_persisted(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(42)
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    await store.flush()
    open_records = await store.get_open_orders_db()
    assert len(open_records) == 1
    assert open_records[0].id == "42"
    assert open_records[0].status == "placed"


async def test_cancelled_status_persisted(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.side_effect = [_resting(1), _resting(2)]
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    await om.update_quotes(_quoteset(bids=[("1850.0", "0.02")]))  # cancel + place
    await store.flush()
    # Order 1 should be cancelled, order 2 placed
    open_records = await store.get_open_orders_db()
    open_ids = {r.id for r in open_records}
    assert "1" not in open_ids
    assert "2" in open_ids


async def test_handle_fill_persists_filled_status(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.return_value = _resting(42)
    om = _make_om(hl, store)
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    fill = FillEntry(
        timestamp_ms=int(om.active_orders[42].placed_at_ms + 100),
        symbol="ETH", side="bid", price=Decimal("1849.5"),
        size=Decimal("0.02"), fee=Decimal("0"), oid=42, is_maker=True, tid=1,
    )
    await om.handle_fill(fill)
    await store.flush()
    open_records = await store.get_open_orders_db()
    assert all(r.id != "42" for r in open_records)


# ---------- Edge cases ----------


async def test_only_bids_no_asks(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.side_effect = [_resting(1), _resting(2), _resting(3)]
    om = _make_om(hl, store)
    res = await om.update_quotes(_quoteset(bids=[
        ("1849.5", "0.02"), ("1849.4", "0.02"), ("1849.3", "0.02"),
    ]))
    assert res.placed == 3
    assert all(o.side == "bid" for o in om.active_orders.values())


async def test_only_asks_no_bids(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.side_effect = [_resting(1), _resting(2)]
    om = _make_om(hl, store)
    res = await om.update_quotes(_quoteset(asks=[
        ("1850.5", "0.02"), ("1850.6", "0.02"),
    ]))
    assert res.placed == 2
    assert all(o.side == "ask" for o in om.active_orders.values())


async def test_invalid_price_tolerance_raises() -> None:
    hl = _hl_mock()
    s = MagicMock(spec=StateStore)
    with pytest.raises(ValueError):
        OrderManager(hl, s, "ETH", price_tolerance=Decimal("-1"))


async def test_callbacks_fire_on_place_and_cancel(store: StateStore) -> None:
    hl = _hl_mock()
    hl.place_order.side_effect = [_resting(1), _resting(2)]
    placed: list[OrderInfo] = []
    cancelled: list[OrderInfo] = []
    om = OrderManager(
        hl, store, "ETH",
        on_order_placed=lambda o: placed.append(o),
        on_order_cancelled=lambda o: cancelled.append(o),
    )
    await om.update_quotes(_quoteset(bids=[("1849.5", "0.02")]))
    assert len(placed) == 1
    await om.update_quotes(_quoteset(bids=[("1850.0", "0.02")]))
    assert len(placed) == 2
    assert len(cancelled) == 1
