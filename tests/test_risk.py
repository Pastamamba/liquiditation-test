"""Tests for src.risk."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import RiskConfig
from src.hl_client import OrderResult
from src.risk import RiskManager, RiskStatus
from src.state import StateStore


def _risk_cfg(
    *,
    max_loss_pct: float = 10.0,
    daily_max_loss_pct: float = 20.0,
    max_vol_pct_1min: float = 2.0,
    inventory_hard_stop_multiplier: float = 1.2,
    max_api_errors_per_minute: int = 5,
    funding_rate_threshold_8h: float = 0.01,
) -> RiskConfig:
    return RiskConfig(
        max_loss_pct=max_loss_pct,
        daily_max_loss_pct=daily_max_loss_pct,
        max_vol_pct_1min=max_vol_pct_1min,
        inventory_hard_stop_multiplier=inventory_hard_stop_multiplier,
        max_api_errors_per_minute=max_api_errors_per_minute,
        funding_rate_threshold_8h=funding_rate_threshold_8h,
    )


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
    return inv


def _md_mock(
    *,
    mid: Decimal | None = Decimal("1850"),
    seconds_since_msg: float | None = 0.5,
    vol: float | None = None,
) -> MagicMock:
    md = MagicMock()
    md.current_mid = mid
    md.seconds_since_last_message = MagicMock(return_value=seconds_since_msg)
    md.realized_volatility = MagicMock(return_value=vol)
    return md


def _om_mock() -> MagicMock:
    om = MagicMock()
    om.cancel_all = AsyncMock(return_value=0)
    return om


def _hl_mock() -> MagicMock:
    hl = MagicMock()
    hl.place_order = AsyncMock(
        return_value=OrderResult(
            status="filled", oid=999, cloid=None,
            avg_price=Decimal("1850"), filled_size=Decimal("0.05"),
        )
    )
    return hl


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[StateStore]:
    s = StateStore(tmp_path / "risk.db")
    await s.open()
    await s.migrate()
    try:
        yield s
    finally:
        await s.close()


def _make_rm(
    *,
    inv: MagicMock | None = None,
    md: MagicMock | None = None,
    om: MagicMock | None = None,
    hl: MagicMock | None = None,
    store: StateStore | None = None,
    cfg: RiskConfig | None = None,
    capital: Decimal = Decimal("100"),
    on_kill: object = None,
    on_pause: object = None,
    connection_timeout_s: float = 10.0,
) -> RiskManager:
    return RiskManager(
        cfg or _risk_cfg(),
        inv or _inv_mock(),
        md or _md_mock(),
        om or _om_mock(),
        hl or _hl_mock(),
        store,  # type: ignore[arg-type]
        "ETH",
        capital,
        connection_timeout_s=connection_timeout_s,
        on_kill=on_kill,  # type: ignore[arg-type]
        on_pause_change=on_pause,  # type: ignore[arg-type]
    )


# ---------- Initial state ----------


async def test_initial_state(store: StateStore) -> None:
    rm = _make_rm(store=store)
    assert not rm.is_killed
    assert not rm.is_paused
    assert rm.kill_reason is None
    assert rm.pause_reason is None
    assert rm.api_errors_last_60s == 0


# ---------- Connection check ----------


async def test_silent_connection_triggers_kill(store: StateStore) -> None:
    md = _md_mock(seconds_since_msg=15.0)
    rm = _make_rm(store=store, md=md, connection_timeout_s=10.0)
    await rm.check_all()
    assert rm.is_killed
    assert rm.kill_reason is not None
    assert "WS silent" in rm.kill_reason


async def test_no_message_yet_does_not_kill(store: StateStore) -> None:
    """Bot just started, no messages yet → seconds_since_last_message=None."""
    md = _md_mock(seconds_since_msg=None)
    rm = _make_rm(store=store, md=md)
    await rm.check_all()
    assert not rm.is_killed


# ---------- Session PnL kill ----------


async def test_session_loss_triggers_kill(store: StateStore) -> None:
    # capital=100, max_loss=10% → kill at -10 PnL (10% loss)
    inv = _inv_mock(realized_pnl=Decimal("-15"))  # -15% loss
    rm = _make_rm(store=store, inv=inv, capital=Decimal("100"))
    await rm.check_all()
    assert rm.is_killed
    assert "session loss" in (rm.kill_reason or "")


async def test_session_loss_below_threshold_does_not_kill(store: StateStore) -> None:
    inv = _inv_mock(realized_pnl=Decimal("-5"))  # -5%
    rm = _make_rm(store=store, inv=inv, capital=Decimal("100"))
    await rm.check_all()
    assert not rm.is_killed


async def test_session_pnl_includes_unrealized(store: StateStore) -> None:
    # Long 0.05 @ 1850, mid 1700 → unrealized = -7.5 (-7.5%)
    # Plus realized 0 → -7.5% < -10%? No, -7.5 / 100 = -7.5% ≥ -10%, no kill.
    # Bring mid lower to trigger.
    inv = _inv_mock(
        position=Decimal("0.05"),
        avg_entry=Decimal("1850"),
        realized_pnl=Decimal("0"),
    )
    md = _md_mock(mid=Decimal("1500"))  # unrealized = 0.05 * (1500-1850) = -17.5
    rm = _make_rm(store=store, inv=inv, md=md, capital=Decimal("100"))
    await rm.check_all()
    assert rm.is_killed


async def test_session_pnl_subtracts_fees(store: StateStore) -> None:
    # capital=100, realized=-9, fees=2 → effective -11 (>10% loss) → kill
    inv = _inv_mock(realized_pnl=Decimal("-9"), total_fees=Decimal("2"))
    rm = _make_rm(store=store, inv=inv, capital=Decimal("100"))
    await rm.check_all()
    assert rm.is_killed


# ---------- Inventory hard stop ----------


async def test_inventory_hard_stop(store: StateStore) -> None:
    # max_position=0.1, multiplier=1.2 → hard_stop=0.12
    inv = _inv_mock(
        position=Decimal("0.13"),
        max_position=Decimal("0.1"),
    )
    cfg = _risk_cfg(inventory_hard_stop_multiplier=1.2)
    rm = _make_rm(store=store, inv=inv, cfg=cfg)
    await rm.check_all()
    assert rm.is_killed
    assert "hard stop" in (rm.kill_reason or "")


async def test_inventory_short_hard_stop(store: StateStore) -> None:
    inv = _inv_mock(
        position=Decimal("-0.13"),
        max_position=Decimal("0.1"),
    )
    cfg = _risk_cfg(inventory_hard_stop_multiplier=1.2)
    rm = _make_rm(store=store, inv=inv, cfg=cfg)
    await rm.check_all()
    assert rm.is_killed


async def test_inventory_within_limit_no_kill(store: StateStore) -> None:
    inv = _inv_mock(
        position=Decimal("0.11"),  # within 1.2x = 0.12
        max_position=Decimal("0.1"),
    )
    rm = _make_rm(store=store, inv=inv)
    await rm.check_all()
    assert not rm.is_killed


# ---------- Volatility halt + hysteresis ----------


async def test_volatility_pause(store: StateStore) -> None:
    md = _md_mock(vol=3.0)  # > threshold 2.0
    cfg = _risk_cfg(max_vol_pct_1min=2.0)
    rm = _make_rm(store=store, md=md, cfg=cfg)
    await rm.check_all()
    assert rm.is_paused
    assert not rm.is_killed
    assert rm.pause_reason is not None


async def test_volatility_hysteresis_holds_pause(store: StateStore) -> None:
    """Vol drops below threshold but above resume_threshold (0.7x) → still paused."""
    cfg = _risk_cfg(max_vol_pct_1min=2.0)  # resume below 1.4
    md = _md_mock(vol=3.0)
    rm = _make_rm(store=store, md=md, cfg=cfg)
    await rm.check_all()
    assert rm.is_paused
    # Vol drops to 1.5 — between 1.4 and 2.0 → STILL paused
    md.realized_volatility.return_value = 1.5
    await rm.check_all()
    assert rm.is_paused


async def test_volatility_hysteresis_resumes_below_factor(store: StateStore) -> None:
    cfg = _risk_cfg(max_vol_pct_1min=2.0)
    md = _md_mock(vol=3.0)
    rm = _make_rm(store=store, md=md, cfg=cfg)
    await rm.check_all()
    assert rm.is_paused
    # Vol drops to 1.3 — below 1.4 → resume
    md.realized_volatility.return_value = 1.3
    await rm.check_all()
    assert not rm.is_paused


async def test_volatility_no_data_does_not_pause(store: StateStore) -> None:
    md = _md_mock(vol=None)
    rm = _make_rm(store=store, md=md)
    await rm.check_all()
    assert not rm.is_paused


async def test_pause_callback_fires_on_change(store: StateStore) -> None:
    events: list[tuple[bool, str | None]] = []

    def on_pause(paused: bool, reason: str | None) -> None:
        events.append((paused, reason))

    cfg = _risk_cfg(max_vol_pct_1min=2.0)
    md = _md_mock(vol=3.0)
    rm = _make_rm(store=store, md=md, cfg=cfg, on_pause=on_pause)
    await rm.check_all()
    assert events == [(True, events[0][1])]
    md.realized_volatility.return_value = 1.0
    await rm.check_all()
    assert len(events) == 2
    assert events[1] == (False, None)


# ---------- API error rate ----------


async def test_api_error_rate_kill(store: StateStore) -> None:
    cfg = _risk_cfg(max_api_errors_per_minute=3)
    rm = _make_rm(store=store, cfg=cfg)
    for _ in range(5):
        rm.record_api_error("network error")
    await rm.check_all()
    assert rm.is_killed
    assert "API error rate" in (rm.kill_reason or "")


async def test_api_error_below_threshold_no_kill(store: StateStore) -> None:
    cfg = _risk_cfg(max_api_errors_per_minute=10)
    rm = _make_rm(store=store, cfg=cfg)
    for _ in range(5):
        rm.record_api_error("network error")
    await rm.check_all()
    assert not rm.is_killed


async def test_api_error_window_trims_old(store: StateStore) -> None:
    rm = _make_rm(store=store)
    # Manually inject old timestamps
    rm._api_error_timestamps.append(time.monotonic() - 120)  # too old
    rm._api_error_timestamps.append(time.monotonic() - 30)
    rm._api_error_timestamps.append(time.monotonic() - 5)
    assert rm.api_errors_last_60s == 2  # only the last two within 60s


# ---------- Daily PnL ----------


async def test_daily_loss_kill(store: StateStore) -> None:
    inv = _inv_mock(realized_pnl=Decimal("-25"))  # -25% → > daily 20%
    rm = _make_rm(store=store, inv=inv, capital=Decimal("100"))
    await rm.check_all()
    assert rm.is_killed
    # Could be triggered by either session or daily — both apply
    assert rm.kill_reason is not None


# ---------- emergency_close ----------


async def test_emergency_close_cancels_all(store: StateStore) -> None:
    om = _om_mock()
    rm = _make_rm(store=store, om=om)
    await rm.emergency_close()
    om.cancel_all.assert_awaited_once()


async def test_emergency_close_long_position_sell_market(store: StateStore) -> None:
    inv = _inv_mock(
        position=Decimal("0.05"), avg_entry=Decimal("1850"),
    )
    md = _md_mock(mid=Decimal("1850"))
    hl = _hl_mock()
    om = _om_mock()
    rm = _make_rm(store=store, inv=inv, md=md, hl=hl, om=om)
    await rm.emergency_close()
    om.cancel_all.assert_awaited_once()
    hl.place_order.assert_awaited_once()
    args = hl.place_order.await_args
    # symbol, side, price, size  — side="ask" jotta myydään
    assert args.args[0] == "ETH"
    assert args.args[1] == "ask"
    # price below mid (aggressive close)
    assert args.args[2] < Decimal("1850")
    assert args.args[3] == Decimal("0.05")
    # post_only=False (taker)
    assert args.kwargs["post_only"] is False
    assert args.kwargs["reduce_only"] is True


async def test_emergency_close_short_position_buy_market(store: StateStore) -> None:
    inv = _inv_mock(
        position=Decimal("-0.05"), avg_entry=Decimal("1850"),
    )
    md = _md_mock(mid=Decimal("1850"))
    hl = _hl_mock()
    om = _om_mock()
    rm = _make_rm(store=store, inv=inv, md=md, hl=hl, om=om)
    await rm.emergency_close()
    args = hl.place_order.await_args
    assert args.args[1] == "bid"
    assert args.args[2] > Decimal("1850")
    assert args.args[3] == Decimal("0.05")


async def test_emergency_close_flat_no_market_order(store: StateStore) -> None:
    inv = _inv_mock(position=Decimal("0"))
    hl = _hl_mock()
    om = _om_mock()
    rm = _make_rm(store=store, inv=inv, hl=hl, om=om)
    await rm.emergency_close()
    om.cancel_all.assert_awaited_once()
    hl.place_order.assert_not_awaited()


async def test_emergency_close_no_mid_skips_market_order(store: StateStore) -> None:
    inv = _inv_mock(position=Decimal("0.05"))
    md = _md_mock(mid=None)
    hl = _hl_mock()
    rm = _make_rm(store=store, inv=inv, md=md, hl=hl)
    await rm.emergency_close()
    hl.place_order.assert_not_awaited()


# ---------- Kill flow ----------


async def test_trigger_kill_sets_state_and_calls_emergency(store: StateStore) -> None:
    om = _om_mock()
    inv = _inv_mock(position=Decimal("0.05"))
    md = _md_mock(mid=Decimal("1850"))
    hl = _hl_mock()
    rm = _make_rm(store=store, inv=inv, md=md, om=om, hl=hl)
    await rm.trigger_kill("test reason")
    assert rm.is_killed
    assert rm.kill_reason == "test reason"
    om.cancel_all.assert_awaited_once()
    hl.place_order.assert_awaited_once()


async def test_kill_callback_invoked(store: StateStore) -> None:
    captured: list[tuple[str, RiskStatus]] = []

    def on_kill(reason: str, status: RiskStatus) -> None:
        captured.append((reason, status))

    rm = _make_rm(store=store, on_kill=on_kill)
    await rm.trigger_kill("test")
    assert len(captured) == 1
    assert captured[0][0] == "test"
    assert captured[0][1].is_killed


async def test_kill_idempotent(store: StateStore) -> None:
    om = _om_mock()
    rm = _make_rm(store=store, om=om)
    await rm.trigger_kill("first")
    await rm.trigger_kill("second")
    assert rm.kill_reason == "first"  # not overwritten
    # cancel_all called only once
    om.cancel_all.assert_awaited_once()


async def test_kill_short_circuits_check_all(store: StateStore) -> None:
    om = _om_mock()
    rm = _make_rm(store=store, om=om)
    await rm.trigger_kill("first")
    await rm.check_all()
    om.cancel_all.assert_awaited_once()  # not called again from check_all


# ---------- Persistence ----------


async def test_kill_event_recorded_in_state_store(store: StateStore) -> None:
    rm = _make_rm(store=store)
    await rm.trigger_kill("session loss")
    await store.flush()
    events = await store.get_recent_events(level="kill")
    assert len(events) == 1
    assert "session loss" in events[0].message


async def test_pause_change_recorded(store: StateStore) -> None:
    cfg = _risk_cfg(max_vol_pct_1min=2.0)
    md = _md_mock(vol=3.0)
    rm = _make_rm(store=store, md=md, cfg=cfg)
    await rm.check_all()
    await store.flush()
    warnings = await store.get_recent_events(level="warning")
    assert any("is_paused=True" in e.message for e in warnings)


# ---------- Snapshot ----------


async def test_snapshot_status(store: StateStore) -> None:
    inv = _inv_mock(
        position=Decimal("0.03"), avg_entry=Decimal("1820"),
        realized_pnl=Decimal("1.5"), total_fees=Decimal("0.2"),
    )
    md = _md_mock(mid=Decimal("1850"), seconds_since_msg=2.0, vol=0.5)
    rm = _make_rm(store=store, inv=inv, md=md, capital=Decimal("100"))
    status = await rm.check_all()
    # session_pnl = 1.5 - 0.2 + 0.03*(1850-1820) = 1.3 + 0.9 = 2.2
    assert status.session_pnl == Decimal("2.2")
    assert status.session_pnl_pct == Decimal("2.2")
    assert status.realized_volatility_60s == 0.5
    assert status.seconds_since_last_message == 2.0
    assert status.inventory_position == Decimal("0.03")
    assert not status.is_killed


# ---------- Validation ----------


def test_invalid_session_capital_raises(store: StateStore) -> None:
    with pytest.raises(ValueError):
        RiskManager(
            _risk_cfg(),
            _inv_mock(),
            _md_mock(),
            _om_mock(),
            _hl_mock(),
            store,
            "ETH",
            Decimal("0"),
        )


def test_invalid_hysteresis_factor_raises(store: StateStore) -> None:
    with pytest.raises(ValueError):
        RiskManager(
            _risk_cfg(),
            _inv_mock(),
            _md_mock(),
            _om_mock(),
            _hl_mock(),
            store,
            "ETH",
            Decimal("100"),
            vol_hysteresis_factor=1.5,
        )
