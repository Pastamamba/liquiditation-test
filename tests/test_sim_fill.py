"""Tests for SimulatedFillEngine + HLClient dry-run mode."""

from __future__ import annotations

import asyncio
import random
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.config import HyperliquidConfig
from src.hl_client import FillEntry, HLClient
from src.market_data import BookLevel, OrderBook
from src.sim_fill import SimulatedFillEngine

# ---------- Fixtures ----------


@pytest.fixture
def hl_config() -> HyperliquidConfig:
    return HyperliquidConfig(
        network="testnet", api_wallet_address="0x" + "1" * 40
    )


@pytest.fixture
def dry_client(hl_config: HyperliquidConfig) -> HLClient:
    """Pure dry-run HLClient — no SDK injected, uses stubs."""
    return HLClient(hl_config, private_key="", dry_run=True)


def _book(mid: float = 1800.0) -> OrderBook:
    half = Decimal("0.5")
    bid = Decimal(str(mid)) - half
    ask = Decimal(str(mid)) + half
    return OrderBook(
        symbol="ETH",
        timestamp_ms=0,
        bids=(BookLevel(price=bid, size=Decimal("1.0")),),
        asks=(BookLevel(price=ask, size=Decimal("1.0")),),
    )


class _StubMarketData:
    """Minimal MarketDataFeed-yhteensopiva stub."""

    def __init__(self, book: OrderBook | None = None) -> None:
        self._book = book

    @property
    def current_mid(self) -> Decimal | None:
        return self._book.mid if self._book is not None else None

    def set_book(self, book: OrderBook | None) -> None:
        self._book = book


# ---------- HLClient dry-run tests ----------


async def test_dry_run_place_order_does_not_call_sdk(
    dry_client: HLClient,
) -> None:
    result = await dry_client.place_order(
        "ETH", "bid", Decimal("1800"), Decimal("0.02")
    )
    assert result.status == "resting"
    assert result.oid is not None
    assert dry_client.dry_state is not None
    assert result.oid in dry_client.dry_state.open_orders


async def test_dry_run_cancel_order_returns_true_and_removes(
    dry_client: HLClient,
) -> None:
    placed = await dry_client.place_order(
        "ETH", "ask", Decimal("1850"), Decimal("0.01")
    )
    assert placed.oid is not None
    ok = await dry_client.cancel_order("ETH", placed.oid)
    assert ok is True
    assert dry_client.dry_state is not None
    assert placed.oid not in dry_client.dry_state.open_orders


async def test_dry_run_cancel_unknown_oid_returns_false(
    dry_client: HLClient,
) -> None:
    ok = await dry_client.cancel_order("ETH", 999_999_999)
    assert ok is False


async def test_dry_run_cancel_all_orders(dry_client: HLClient) -> None:
    for px in (1799, 1800, 1801):
        await dry_client.place_order(
            "ETH", "bid", Decimal(str(px)), Decimal("0.01")
        )
    for px in (1810, 1820):
        await dry_client.place_order(
            "BTC", "bid", Decimal(str(px)), Decimal("0.01")
        )
    cancelled = await dry_client.cancel_all_orders("ETH")
    assert cancelled == 3
    # BTC orders untouched
    btc_open = await dry_client.get_open_orders("BTC")
    assert len(btc_open) == 2


async def test_dry_run_get_open_orders_filters_by_symbol(
    dry_client: HLClient,
) -> None:
    await dry_client.place_order("ETH", "bid", Decimal("1800"), Decimal("0.01"))
    await dry_client.place_order("BTC", "ask", Decimal("60000"), Decimal("0.001"))
    eth = await dry_client.get_open_orders("ETH")
    btc = await dry_client.get_open_orders("BTC")
    both = await dry_client.get_open_orders(None)
    assert len(eth) == 1
    assert len(btc) == 1
    assert len(both) == 2


async def test_dry_run_simulate_fill_creates_fill_and_updates_position(
    dry_client: HLClient,
) -> None:
    placed = await dry_client.place_order(
        "ETH", "bid", Decimal("1800"), Decimal("0.05")
    )
    assert placed.oid is not None
    fill = await dry_client.simulate_fill(placed.oid)
    assert fill is not None
    assert fill.side == "bid"
    assert fill.price == Decimal("1800")
    assert fill.size == Decimal("0.05")
    assert fill.is_maker is True
    assert dry_client.dry_state is not None
    assert dry_client.dry_state.position_size == Decimal("0.05")
    assert placed.oid not in dry_client.dry_state.open_orders


async def test_dry_run_simulate_fill_unknown_oid_returns_none(
    dry_client: HLClient,
) -> None:
    fill = await dry_client.simulate_fill(123_456)
    assert fill is None


async def test_dry_run_get_position_uses_simulated_state(
    dry_client: HLClient,
) -> None:
    placed = await dry_client.place_order(
        "ETH", "bid", Decimal("1800"), Decimal("0.05")
    )
    assert placed.oid is not None
    await dry_client.simulate_fill(placed.oid)
    pos = await dry_client.get_position("ETH")
    assert pos.size == Decimal("0.05")
    assert pos.entry_price == Decimal("1800")


async def test_dry_run_get_user_fills_returns_recorded(
    dry_client: HLClient,
) -> None:
    placed = await dry_client.place_order(
        "ETH", "ask", Decimal("1850"), Decimal("0.02")
    )
    assert placed.oid is not None
    await dry_client.simulate_fill(placed.oid)
    fills = await dry_client.get_user_fills(0)
    assert len(fills) == 1
    assert fills[0].symbol == "ETH"


async def test_dry_run_position_weighted_average_on_same_direction(
    dry_client: HLClient,
) -> None:
    o1 = await dry_client.place_order(
        "ETH", "bid", Decimal("1800"), Decimal("0.05")
    )
    o2 = await dry_client.place_order(
        "ETH", "bid", Decimal("1820"), Decimal("0.05")
    )
    assert o1.oid is not None and o2.oid is not None
    await dry_client.simulate_fill(o1.oid)
    await dry_client.simulate_fill(o2.oid)
    pos = await dry_client.get_position("ETH")
    assert pos.size == Decimal("0.10")
    assert pos.entry_price == Decimal("1810")  # weighted average


async def test_dry_run_position_close_resets_avg(dry_client: HLClient) -> None:
    o_buy = await dry_client.place_order(
        "ETH", "bid", Decimal("1800"), Decimal("0.05")
    )
    o_sell = await dry_client.place_order(
        "ETH", "ask", Decimal("1820"), Decimal("0.05")
    )
    assert o_buy.oid is not None and o_sell.oid is not None
    await dry_client.simulate_fill(o_buy.oid)
    await dry_client.simulate_fill(o_sell.oid)
    pos = await dry_client.get_position("ETH")
    assert pos.size == Decimal("0")
    assert pos.entry_price == Decimal("0")


async def test_dry_run_simulate_fill_in_non_dry_raises() -> None:
    config = HyperliquidConfig(
        network="testnet", api_wallet_address="0x" + "1" * 40
    )
    info_mock = MagicMock()
    info_mock.meta.return_value = {
        "universe": [{"name": "ETH", "szDecimals": 4}]
    }
    exch_mock = MagicMock()
    client = HLClient(config, private_key="", info=info_mock, exchange=exch_mock)
    with pytest.raises(RuntimeError, match="dry_run=True"):
        await client.simulate_fill(1)


async def test_dry_run_size_rounds_to_zero_rejected(
    dry_client: HLClient,
) -> None:
    from src.hl_client import HLOrderRejectedError

    with pytest.raises(HLOrderRejectedError):
        await dry_client.place_order(
            "ETH", "bid", Decimal("1800"), Decimal("0.000001")
        )


async def test_dry_run_constructor_does_not_require_private_key(
    hl_config: HyperliquidConfig,
) -> None:
    # ei nosta HLAuthError:ia
    client = HLClient(hl_config, private_key="", dry_run=True)
    assert client.dry_run is True


# ---------- SimulatedFillEngine tests ----------


async def test_sim_fill_requires_dry_run_client(
    hl_config: HyperliquidConfig,
) -> None:
    info_mock = MagicMock()
    info_mock.meta.return_value = {
        "universe": [{"name": "ETH", "szDecimals": 4}]
    }
    non_dry = HLClient(
        hl_config, private_key="", info=info_mock, exchange=MagicMock()
    )
    md = _StubMarketData()
    with pytest.raises(RuntimeError, match="dry_run=True"):
        SimulatedFillEngine(
            hl_client=non_dry,
            market_data=md,  # type: ignore[arg-type]
            symbol="ETH",
            on_fill=_noop_fill,
        )


async def _noop_fill(_fill: FillEntry) -> bool:
    return True


async def test_sim_fill_validates_probability(dry_client: HLClient) -> None:
    md = _StubMarketData()
    with pytest.raises(ValueError, match="fill_probability"):
        SimulatedFillEngine(
            hl_client=dry_client,
            market_data=md,  # type: ignore[arg-type]
            symbol="ETH",
            on_fill=_noop_fill,
            fill_probability=0.0,
        )


async def test_sim_fill_no_mid_returns_zero_fills(
    dry_client: HLClient,
) -> None:
    md = _StubMarketData(book=None)
    engine = SimulatedFillEngine(
        hl_client=dry_client,
        market_data=md,  # type: ignore[arg-type]
        symbol="ETH",
        on_fill=_noop_fill,
        fill_probability=1.0,
    )
    filled = await engine.tick()
    assert filled == 0


async def test_sim_fill_aggressive_bid_fills(dry_client: HLClient) -> None:
    """Bid >= mid → täyttyy probabilistisesti (tässä prob=1.0)."""
    md = _StubMarketData(_book(1800.0))
    fills_received: list[FillEntry] = []

    async def collect(fill: FillEntry) -> bool:
        fills_received.append(fill)
        return True

    placed = await dry_client.place_order(
        "ETH", "bid", Decimal("1801"), Decimal("0.05")
    )
    assert placed.oid is not None
    engine = SimulatedFillEngine(
        hl_client=dry_client,
        market_data=md,  # type: ignore[arg-type]
        symbol="ETH",
        on_fill=collect,
        fill_probability=1.0,
        rng=random.Random(42),
    )
    filled = await engine.tick()
    assert filled == 1
    assert len(fills_received) == 1
    assert fills_received[0].oid == placed.oid


async def test_sim_fill_passive_bid_does_not_fill(
    dry_client: HLClient,
) -> None:
    """Bid < mid → ei täyty (passive maker)."""
    md = _StubMarketData(_book(1800.0))
    placed = await dry_client.place_order(
        "ETH", "bid", Decimal("1795"), Decimal("0.05")
    )
    assert placed.oid is not None

    engine = SimulatedFillEngine(
        hl_client=dry_client,
        market_data=md,  # type: ignore[arg-type]
        symbol="ETH",
        on_fill=_noop_fill,
        fill_probability=1.0,
    )
    filled = await engine.tick()
    assert filled == 0
    assert dry_client.dry_state is not None
    assert placed.oid in dry_client.dry_state.open_orders


async def test_sim_fill_aggressive_ask_fills(dry_client: HLClient) -> None:
    """Ask <= mid → täyttyy."""
    md = _StubMarketData(_book(1800.0))
    placed = await dry_client.place_order(
        "ETH", "ask", Decimal("1799"), Decimal("0.05")
    )
    assert placed.oid is not None

    engine = SimulatedFillEngine(
        hl_client=dry_client,
        market_data=md,  # type: ignore[arg-type]
        symbol="ETH",
        on_fill=_noop_fill,
        fill_probability=1.0,
        rng=random.Random(0),
    )
    filled = await engine.tick()
    assert filled == 1


async def test_sim_fill_passive_ask_does_not_fill(
    dry_client: HLClient,
) -> None:
    """Ask > mid → ei täyty."""
    md = _StubMarketData(_book(1800.0))
    placed = await dry_client.place_order(
        "ETH", "ask", Decimal("1810"), Decimal("0.05")
    )
    assert placed.oid is not None

    engine = SimulatedFillEngine(
        hl_client=dry_client,
        market_data=md,  # type: ignore[arg-type]
        symbol="ETH",
        on_fill=_noop_fill,
        fill_probability=1.0,
    )
    filled = await engine.tick()
    assert filled == 0


async def test_sim_fill_probability_zero_passes_no_orders(
    dry_client: HLClient,
) -> None:
    """Edge case: deterministic rng aina > prob → ei täyttöjä."""
    md = _StubMarketData(_book(1800.0))
    await dry_client.place_order(
        "ETH", "bid", Decimal("1810"), Decimal("0.05")
    )
    rng = random.Random()
    rng.random = lambda: 0.99  # type: ignore[method-assign]

    engine = SimulatedFillEngine(
        hl_client=dry_client,
        market_data=md,  # type: ignore[arg-type]
        symbol="ETH",
        on_fill=_noop_fill,
        fill_probability=0.5,  # 0.99 > 0.5 → no fill
        rng=rng,
    )
    filled = await engine.tick()
    assert filled == 0


async def test_sim_fill_only_fills_target_symbol(dry_client: HLClient) -> None:
    md = _StubMarketData(_book(1800.0))
    eth_placed = await dry_client.place_order(
        "ETH", "bid", Decimal("1810"), Decimal("0.05")
    )
    btc_placed = await dry_client.place_order(
        "BTC", "bid", Decimal("70000"), Decimal("0.001")
    )
    assert eth_placed.oid is not None and btc_placed.oid is not None

    engine = SimulatedFillEngine(
        hl_client=dry_client,
        market_data=md,  # type: ignore[arg-type]
        symbol="ETH",
        on_fill=_noop_fill,
        fill_probability=1.0,
    )
    filled = await engine.tick()
    assert filled == 1
    # BTC order should still be open
    assert dry_client.dry_state is not None
    assert btc_placed.oid in dry_client.dry_state.open_orders


async def test_sim_fill_run_lifecycle(dry_client: HLClient) -> None:
    md = _StubMarketData(_book(1800.0))
    fills_received: list[FillEntry] = []

    async def collect(fill: FillEntry) -> bool:
        fills_received.append(fill)
        return True

    await dry_client.place_order(
        "ETH", "bid", Decimal("1810"), Decimal("0.05")
    )
    engine = SimulatedFillEngine(
        hl_client=dry_client,
        market_data=md,  # type: ignore[arg-type]
        symbol="ETH",
        on_fill=collect,
        fill_probability=1.0,
        check_interval_s=0.05,
    )
    await engine.start()
    await asyncio.sleep(0.2)
    await engine.stop()
    assert engine.fill_count >= 1
    assert len(fills_received) >= 1


async def test_sim_fill_fee_computed_from_notional(
    dry_client: HLClient,
) -> None:
    placed = await dry_client.place_order(
        "ETH", "bid", Decimal("2000"), Decimal("0.1")
    )
    assert placed.oid is not None
    fill = await dry_client.simulate_fill(placed.oid, fee_bps=2.0)
    assert fill is not None
    # notional = 2000 * 0.1 = 200; fee = 200 * 2bp = 200 * 0.0002 = 0.04
    assert fill.fee == Decimal("0.0400")


async def test_sim_fill_invalid_check_interval_raises(
    dry_client: HLClient,
) -> None:
    md = _StubMarketData()
    with pytest.raises(ValueError, match="check_interval_s"):
        SimulatedFillEngine(
            hl_client=dry_client,
            market_data=md,  # type: ignore[arg-type]
            symbol="ETH",
            on_fill=_noop_fill,
            check_interval_s=0,
        )


async def test_sim_fill_callback_failure_does_not_crash(
    dry_client: HLClient,
) -> None:
    md = _StubMarketData(_book(1800.0))

    async def bad_callback(_fill: FillEntry) -> bool:
        raise RuntimeError("boom")

    placed = await dry_client.place_order(
        "ETH", "bid", Decimal("1810"), Decimal("0.05")
    )
    assert placed.oid is not None

    engine = SimulatedFillEngine(
        hl_client=dry_client,
        market_data=md,  # type: ignore[arg-type]
        symbol="ETH",
        on_fill=bad_callback,
        fill_probability=1.0,
    )
    # tick() doesn't crash even though callback raised
    filled = await engine.tick()
    # fill was generated but dispatch failed, so fill_count not incremented
    assert filled == 0
