"""Tests for src.metrics."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.hl_client import FillEntry
from src.metrics import (
    MetricsCollector,
    MetricsSummary,
    compute_adverse_bps,
    percentile,
)
from src.order_manager import OrderInfo
from src.state import StateStore

# ---------- Fixtures / mocks ----------


def _inv_mock(
    *,
    position: Decimal = Decimal("0"),
    avg_entry: Decimal = Decimal("0"),
    realized_pnl: Decimal = Decimal("0"),
    total_fees: Decimal = Decimal("0"),
    max_position: Decimal = Decimal("0.1"),
) -> MagicMock:
    inv = MagicMock()
    inv.current_position = position
    inv.avg_entry_price = avg_entry
    inv.realized_pnl = realized_pnl
    inv.total_fees = total_fees
    inv.max_position = max_position
    inv.position_value_usd = MagicMock(
        return_value=abs(position) * (avg_entry or Decimal("0"))
    )
    return inv


def _md_mock(
    *,
    mid: Decimal | None = Decimal("1850"),
    seconds_since_msg: float | None = 0.5,
    vol: float | None = 0.4,
) -> MagicMock:
    md = MagicMock()
    md.current_mid = mid
    md.seconds_since_last_message = MagicMock(return_value=seconds_since_msg)
    md.realized_volatility = MagicMock(return_value=vol)
    return md


def _om_mock(
    *,
    active: dict[int, OrderInfo] | None = None,
) -> MagicMock:
    om = MagicMock()
    om.active_orders = active or {}
    return om


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[StateStore]:
    s = StateStore(tmp_path / "metrics.db")
    await s.open()
    await s.migrate()
    try:
        yield s
    finally:
        await s.close()


def _make_collector(
    *,
    inv: MagicMock | None = None,
    md: MagicMock | None = None,
    om: MagicMock | None = None,
    store: StateStore,
    capital: Decimal = Decimal("100"),
    interval_s: float = 10.0,
    adverse_window_s: float = 10.0,
) -> MetricsCollector:
    return MetricsCollector(
        store,
        inv or _inv_mock(),
        md or _md_mock(),
        om or _om_mock(),
        symbol="ETH",
        capital_usdc=capital,
        interval_s=interval_s,
        adverse_window_s=adverse_window_s,
    )


def _fill(
    *,
    side: str = "bid",
    price: str = "1850",
    size: str = "0.02",
    fee: str = "0",
    oid: int = 1,
    tid: int = 1,
    is_maker: bool = True,
) -> FillEntry:
    return FillEntry(
        timestamp_ms=1700_000_000_000,
        symbol="ETH",
        side=side,  # type: ignore[arg-type]
        price=Decimal(price),
        size=Decimal(size),
        fee=Decimal(fee),
        oid=oid,
        is_maker=is_maker,
        tid=tid,
    )


# ---------- Pure helpers ----------


class TestComputeAdverseBps:
    def test_bid_drop_is_adverse(self) -> None:
        # bought 1850, mid drops to 1845 → adverse
        bps = compute_adverse_bps("bid", Decimal("1850"), Decimal("1845"))
        assert bps == pytest.approx(5 / 1850 * 10000, rel=1e-6)
        assert bps > 0

    def test_bid_rise_is_favorable(self) -> None:
        # bought 1850, mid rises → negative adverse (good)
        bps = compute_adverse_bps("bid", Decimal("1850"), Decimal("1855"))
        assert bps < 0

    def test_ask_rise_is_adverse(self) -> None:
        # sold 1850, mid rises to 1855 → adverse
        bps = compute_adverse_bps("ask", Decimal("1850"), Decimal("1855"))
        assert bps == pytest.approx(5 / 1850 * 10000, rel=1e-6)

    def test_ask_drop_is_favorable(self) -> None:
        bps = compute_adverse_bps("ask", Decimal("1850"), Decimal("1845"))
        assert bps < 0

    def test_zero_fill_mid_returns_zero(self) -> None:
        assert compute_adverse_bps("bid", Decimal("0"), Decimal("100")) == 0.0


class TestPercentile:
    def test_empty_list(self) -> None:
        assert percentile([], 50) == 0.0

    def test_single_value(self) -> None:
        assert percentile([10.0], 50) == 10.0
        assert percentile([10.0], 99) == 10.0

    def test_known_distribution(self) -> None:
        samples = [float(i) for i in range(1, 101)]
        assert percentile(samples, 50) == pytest.approx(50.5)
        assert percentile(samples, 95) == pytest.approx(95.05, abs=0.5)
        assert percentile(samples, 99) == pytest.approx(99.01, abs=0.5)


# ---------- record_quote_latency_ms ----------


async def test_quote_latency_recorded_and_percentiles(store: StateStore) -> None:
    mc = _make_collector(store=store)
    for i in range(1, 101):
        mc.record_quote_latency_ms(float(i))
    s = mc.get_summary()
    assert s.quote_latency_p50_ms == pytest.approx(50.5)
    assert s.quote_latency_p95_ms == pytest.approx(95.05, abs=0.5)
    assert s.quote_latency_p99_ms == pytest.approx(99.01, abs=0.5)


async def test_quote_latency_negative_ignored(store: StateStore) -> None:
    mc = _make_collector(store=store)
    mc.record_quote_latency_ms(-1.0)
    mc.record_quote_latency_ms(5.0)
    assert len(mc.latency_samples) == 1


async def test_no_latency_yet_zeros(store: StateStore) -> None:
    mc = _make_collector(store=store)
    s = mc.get_summary()
    assert s.quote_latency_p50_ms == 0.0
    assert s.quote_latency_p95_ms == 0.0
    assert s.quote_latency_p99_ms == 0.0


# ---------- on_fill / on_cancel counters ----------


async def test_fill_counters_per_side(store: StateStore) -> None:
    mc = _make_collector(store=store)
    mc.on_fill(_fill(side="bid", tid=1))
    mc.on_fill(_fill(side="bid", tid=2))
    mc.on_fill(_fill(side="ask", tid=3))
    s = mc.get_summary()
    assert s.fill_count_bid_session == 2
    assert s.fill_count_ask_session == 1


async def test_cancel_counter(store: StateStore) -> None:
    mc = _make_collector(store=store)
    mc.on_cancel()
    mc.on_cancel()
    mc.on_cancel()
    s = mc.get_summary()
    assert s.cancel_count_session == 3


# ---------- Adverse selection ----------


async def test_adverse_selection_drains_after_window(store: StateStore) -> None:
    md = _md_mock(mid=Decimal("1850"))
    mc = _make_collector(store=store, md=md, adverse_window_s=1.0)
    # Record a bid fill at mid=1850
    mc.on_fill(_fill(side="bid", tid=1))
    # Force time advance + new mid
    md.current_mid = Decimal("1845")
    drained = mc.process_pending_adverse(
        now_mono=mc._pending_adverse[0].recorded_at_mono + 2.0,
    )
    assert drained == 1
    assert len(mc.adverse_history) == 1
    # Bid bought at 1850, mid dropped to 1845 → adverse +27 bps
    assert mc.adverse_history[0] == pytest.approx(5 / 1850 * 10000, rel=1e-6)


async def test_adverse_selection_not_drained_within_window(store: StateStore) -> None:
    md = _md_mock(mid=Decimal("1850"))
    mc = _make_collector(store=store, md=md, adverse_window_s=10.0)
    mc.on_fill(_fill(side="bid", tid=1))
    drained = mc.process_pending_adverse(
        now_mono=mc._pending_adverse[0].recorded_at_mono + 1.0,
    )
    assert drained == 0


async def test_adverse_selection_average_in_summary(store: StateStore) -> None:
    md = _md_mock(mid=Decimal("1850"))
    mc = _make_collector(store=store, md=md, adverse_window_s=1.0)
    # Two bid fills, both at 1850
    mc.on_fill(_fill(side="bid", tid=1))
    mc.on_fill(_fill(side="bid", tid=2))
    # Mid drops to 1840 → adverse for both
    md.current_mid = Decimal("1840")
    base_t = mc._pending_adverse[0].recorded_at_mono
    mc.process_pending_adverse(now_mono=base_t + 5.0)
    s = mc.get_summary()
    expected_bps = 10 / 1850 * 10000
    assert s.adverse_selection_bps == pytest.approx(expected_bps, rel=1e-6)
    assert s.sample_count_adverse == 2


async def test_adverse_selection_synthetic_sequence(store: StateStore) -> None:
    """Tunnetut arvot: bid@1800 → mid 1810 (favorable -55 bps);
    ask@1820 → mid 1840 (adverse +109 bps).

    Manipuloimme pending-listaa suoraan jotta voimme antaa eri post_mid:n
    kummallekin fillille.
    """
    from src.metrics import _PendingAdverse

    md = _md_mock(mid=Decimal("1800"))
    mc = _make_collector(store=store, md=md, adverse_window_s=1.0)

    # Lisää pending-entry 1 manuaalisesti — bid @ fill_mid=1800
    mc._pending_adverse.append(
        _PendingAdverse(
            fill_side="bid", fill_mid=Decimal("1800"), recorded_at_mono=100.0
        )
    )
    # Drain ensimmäinen: post_mid=1810 → adverse for bid = (1800-1810)/1800 = -55.55 bps
    drained1 = mc.process_pending_adverse(now_mono=200.0, current_mid=Decimal("1810"))
    assert drained1 == 1

    # Pending-entry 2 — ask @ fill_mid=1820
    mc._pending_adverse.append(
        _PendingAdverse(
            fill_side="ask", fill_mid=Decimal("1820"), recorded_at_mono=300.0
        )
    )
    # Drain toinen: post_mid=1840 → adverse for ask = (1840-1820)/1820 = +109.89 bps
    drained2 = mc.process_pending_adverse(now_mono=400.0, current_mid=Decimal("1840"))
    assert drained2 == 1

    # First: bid 1800, post 1810 → adverse = (1800-1810)/1800*10000 = -55.55 bps
    # Second: ask 1820, post 1840 → adverse = (1840-1820)/1820*10000 = +109.89 bps
    assert mc.adverse_history[0] == pytest.approx(-55.555, abs=0.1)
    assert mc.adverse_history[1] == pytest.approx(109.89, abs=0.1)


async def test_adverse_selection_ignores_when_no_mid(store: StateStore) -> None:
    md = _md_mock(mid=None)
    mc = _make_collector(store=store, md=md)
    mc.on_fill(_fill(side="bid", tid=1))
    # No mid was available at fill time, so nothing was queued
    assert len(mc._pending_adverse) == 0


# ---------- get_summary aggregation ----------


async def test_summary_with_pnl(store: StateStore) -> None:
    inv = _inv_mock(
        position=Decimal("0.05"), avg_entry=Decimal("1820"),
        realized_pnl=Decimal("2.0"), total_fees=Decimal("0.5"),
    )
    md = _md_mock(mid=Decimal("1850"), vol=0.6)
    mc = _make_collector(store=store, inv=inv, md=md, capital=Decimal("100"))
    s = mc.get_summary()
    # unrealized = 0.05 * (1850-1820) = 1.5
    assert s.unrealized_pnl == Decimal("1.5")
    assert s.realized_pnl == Decimal("2.0")
    # net = 2 - 0.5 + 0 + 1.5 = 3.0
    assert s.net_pnl == Decimal("3.0")
    assert s.spread_pnl == Decimal("2.0")  # gross = realized
    assert s.total_fees == Decimal("0.5")
    assert s.capital == Decimal("100")
    assert s.realized_volatility_60s == 0.6


async def test_summary_active_order_counts(store: StateStore) -> None:
    om = _om_mock(active={
        1: OrderInfo(oid=1, cloid="x", symbol="ETH", side="bid",
                     price=Decimal("1849"), size=Decimal("0.02"),
                     placed_at_ms=1),
        2: OrderInfo(oid=2, cloid="y", symbol="ETH", side="bid",
                     price=Decimal("1848"), size=Decimal("0.02"),
                     placed_at_ms=1),
        3: OrderInfo(oid=3, cloid="z", symbol="ETH", side="ask",
                     price=Decimal("1851"), size=Decimal("0.02"),
                     placed_at_ms=1),
    })
    mc = _make_collector(store=store, om=om)
    s = mc.get_summary()
    assert s.active_bid_count == 2
    assert s.active_ask_count == 1


async def test_summary_websocket_lag_passed_through(store: StateStore) -> None:
    md = _md_mock(seconds_since_msg=2.5)
    mc = _make_collector(store=store, md=md)
    s = mc.get_summary()
    assert s.websocket_lag_seconds == 2.5


async def test_rebate_added(store: StateStore) -> None:
    inv = _inv_mock(realized_pnl=Decimal("5"), total_fees=Decimal("1"))
    mc = _make_collector(store=store, inv=inv)
    mc.add_rebate(Decimal("0.3"))
    s = mc.get_summary()
    assert s.rebate_earned == Decimal("0.3")
    # net = realized - fees + rebate + unrealized(0) = 5 - 1 + 0.3 = 4.3
    assert s.net_pnl == Decimal("4.3")


# ---------- Persistence ----------


async def test_snapshot_persists_to_state_store(store: StateStore) -> None:
    inv = _inv_mock(
        position=Decimal("0.02"), avg_entry=Decimal("1850"),
        realized_pnl=Decimal("1.5"), total_fees=Decimal("0.2"),
    )
    md = _md_mock(mid=Decimal("1860"))
    mc = _make_collector(store=store, inv=inv, md=md, capital=Decimal("100"))
    summary = await mc.snapshot_now()
    await store.flush()
    history = await store.get_pnl_history(limit=10)
    assert len(history) == 1
    rec = history[0]
    assert rec.realized_pnl == summary.realized_pnl
    assert rec.unrealized_pnl == summary.unrealized_pnl
    assert rec.inventory == summary.inventory_position
    assert rec.spread_pnl == summary.spread_pnl
    assert rec.capital == Decimal("100")


async def test_snapshot_drains_adverse_first(store: StateStore) -> None:
    md = _md_mock(mid=Decimal("1850"))
    mc = _make_collector(store=store, md=md, adverse_window_s=0.0001)
    mc.on_fill(_fill(side="bid", tid=1))
    # Sleep enough to age the pending fill
    import asyncio as aio
    await aio.sleep(0.01)
    md.current_mid = Decimal("1840")
    summary = await mc.snapshot_now()
    assert summary.sample_count_adverse == 1


# ---------- Daily rollover ----------


async def test_daily_rollover_resets_session_counters(store: StateStore) -> None:
    from datetime import UTC as UTC_TZ
    from datetime import datetime as dt
    from datetime import timedelta

    mc = _make_collector(store=store)
    mc.on_fill(_fill(side="bid", tid=1))
    mc.on_fill(_fill(side="ask", tid=2))
    mc.on_cancel()
    mc.record_quote_latency_ms(5.0)
    # Force daily rollover by setting prev day
    mc._daily_start_day = dt.now(UTC_TZ).date() - timedelta(days=1)
    await mc._check_daily_rollover()
    assert mc.fill_count_bid == 0
    assert mc.fill_count_ask == 0
    assert mc.cancel_count == 0
    assert len(mc.latency_samples) == 0


async def test_daily_rollover_persists_summary(store: StateStore) -> None:
    from datetime import UTC as UTC_TZ
    from datetime import datetime as dt
    from datetime import timedelta

    mc = _make_collector(store=store)
    mc.on_fill(_fill(side="bid", tid=1))
    mc._daily_start_day = dt.now(UTC_TZ).date() - timedelta(days=1)
    await mc._check_daily_rollover()
    await store.flush()
    events = await store.get_recent_events(level="info")
    assert any("daily_summary" in e.message for e in events)


async def test_no_rollover_same_day(store: StateStore) -> None:
    mc = _make_collector(store=store)
    mc.on_fill(_fill(side="bid", tid=1))
    await mc._check_daily_rollover()
    # Same day → no reset
    assert mc.fill_count_bid == 1


# ---------- Validation / lifecycle ----------


async def test_invalid_interval_raises(store: StateStore) -> None:
    with pytest.raises(ValueError):
        MetricsCollector(
            store, _inv_mock(), _md_mock(), _om_mock(),
            symbol="ETH", capital_usdc=Decimal("100"), interval_s=0,
        )


async def test_summary_is_metricssummary(store: StateStore) -> None:
    mc = _make_collector(store=store)
    s = mc.get_summary()
    assert isinstance(s, MetricsSummary)
