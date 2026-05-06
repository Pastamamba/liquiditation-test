"""Integration smoke test for STEP 13 — runs MMBot a few seconds with mocks.

Tarkoitus: varmistaa että orchestrator käynnistyy, kytkee callbackit, pyörittää
quote_loop-cycleä ja sammuu siististi ilman exceptioneita. Ei testaa oikeaa
markkinakäyttäytymistä — pelkkä smoke + lifecycle-tarkistus.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import (
    BotConfig,
    HyperliquidConfig,
    OperationsConfig,
    RiskConfig,
    StorageConfig,
    TelegramConfig,
    TradingConfig,
)
from src.hl_client import AssetMeta, FillEntry, HLClient, OrderResult, Position
from src.inventory import InventoryManager
from src.main import MMBot, _banner
from src.market_data import BookLevel, MarketDataFeed, OrderBook
from src.metrics import MetricsCollector
from src.order_manager import OrderManager
from src.quote_engine import QuoteEngine
from src.risk import RiskManager
from src.state import StateStore

# ---------- Fakes / fixtures ----------


def _make_config(tmp_db: Path) -> BotConfig:
    return BotConfig(
        hyperliquid=HyperliquidConfig(
            network="testnet",
            api_wallet_address="0x" + "ab" * 20,
        ),
        trading=TradingConfig(
            symbol="ETH",
            capital_usdc=Decimal("100"),
            max_position_size=Decimal("0.1"),
            spread_bps=5.0,
            num_levels=2,
            level_spacing_bps=3.0,
            order_size=Decimal("0.02"),
            skew_factor=0.5,
            quote_refresh_ms=200,
            max_order_age_seconds=30,
        ),
        risk=RiskConfig(
            max_loss_pct=10.0,
            daily_max_loss_pct=20.0,
            max_vol_pct_1min=2.0,
            inventory_hard_stop_multiplier=1.2,
            max_api_errors_per_minute=5,
            funding_rate_threshold_8h=0.01,
        ),
        telegram=TelegramConfig(),
        storage=StorageConfig(
            db_path=tmp_db,
            log_path=tmp_db.parent / "test.log",
            metrics_interval_seconds=1,
        ),
        operations=OperationsConfig(dry_run=True),
    )


def _make_book(mid: float = 1800.0) -> OrderBook:
    half_spread = Decimal("0.5")
    bid = Decimal(str(mid)) - half_spread
    ask = Decimal(str(mid)) + half_spread
    return OrderBook(
        symbol="ETH",
        timestamp_ms=0,
        bids=(BookLevel(price=bid, size=Decimal("1.0")),),
        asks=(BookLevel(price=ask, size=Decimal("1.0")),),
    )


class _FakeWS:
    """Minimal fake WebSocket — tasses ei tarvita oikeita viestejä."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        # Block until cancelled — testissä _stop_event:llä lopetetaan.
        await asyncio.sleep(60.0)
        return ""

    async def close(self) -> None:
        self._closed = True


def _ws_factory() -> Any:
    async def factory(_url: str) -> Any:
        return _FakeWS()
    return factory


def _mock_hl_client(meta: AssetMeta) -> Any:
    """AsyncMock HLClient korvaamaan oikea SDK."""
    mock = MagicMock(spec=HLClient)
    mock.get_asset_meta = AsyncMock(return_value=meta)
    mock.get_position = AsyncMock(
        return_value=Position(
            symbol="ETH",
            size=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
        )
    )
    mock.get_open_orders = AsyncMock(return_value=[])
    mock.get_user_fills = AsyncMock(return_value=[])
    mock.get_funding_rate = AsyncMock(return_value=Decimal("0"))
    mock.cancel_all_orders = AsyncMock(return_value=0)
    mock.cancel_order = AsyncMock(return_value=True)
    mock.place_order = AsyncMock(
        return_value=OrderResult(
            status="resting",
            oid=1,
            cloid=None,
            avg_price=None,
            filled_size=None,
        )
    )
    # Fake config attribute used by InventoryManager
    mock._config = MagicMock(api_wallet_address="0x" + "ab" * 20)
    return mock


@pytest.fixture
async def state_store(tmp_path: Path) -> AsyncIterator[StateStore]:
    db_path = tmp_path / "test_integration.db"
    store = StateStore(db_path)
    await store.open()
    await store.migrate()
    yield store
    await store.close()


@pytest.fixture
def asset_meta() -> AssetMeta:
    return AssetMeta(
        symbol="ETH",
        asset_id=1,
        sz_decimals=4,
        max_leverage=50,
        is_delisted=False,
    )


# ---------- Tests ----------


def test_banner_testnet_label() -> None:
    cfg = _make_config(Path("/tmp/x.db"))
    text = _banner(cfg)
    assert "TESTNET" in text
    assert "ETH" in text
    assert "100" in text


def test_banner_mainnet_label() -> None:
    cfg = _make_config(Path("/tmp/x.db"))
    cfg.hyperliquid.network = "mainnet"
    text = _banner(cfg)
    assert "MAINNET" in text


async def test_mmbot_setup_and_shutdown(
    tmp_path: Path, state_store: StateStore, asset_meta: AssetMeta
) -> None:
    """Bot käynnistyy, ajaa hetken, sammuu siististi."""
    config = _make_config(tmp_path / "smoke.db")
    hl = _mock_hl_client(asset_meta)

    md = MarketDataFeed(
        symbol="ETH",
        network="testnet",
        connect_factory=_ws_factory(),
        silence_timeout_s=120.0,
    )
    inv = InventoryManager(
        symbol="ETH",
        max_position=Decimal("0.1"),
        hl_client=hl,
        state_store=state_store,
        connect_factory=_ws_factory(),
        silence_timeout_s=120.0,
    )
    qe = QuoteEngine(config.trading, sz_decimals=asset_meta.sz_decimals)
    om = OrderManager(
        hl_client=hl,
        state_store=state_store,
        symbol="ETH",
        cleanup_interval_s=60.0,
    )
    rm = RiskManager(
        config=config.risk,
        inventory=inv,
        market_data=md,
        order_manager=om,
        hl_client=hl,
        state_store=state_store,
        symbol="ETH",
        session_start_capital=config.trading.capital_usdc,
        check_interval_s=60.0,
    )
    metrics = MetricsCollector(
        state_store=state_store,
        inventory=inv,
        market_data=md,
        order_manager=om,
        symbol="ETH",
        capital_usdc=config.trading.capital_usdc,
        interval_s=60.0,
    )

    bot = MMBot(
        config=config,
        state_store=state_store,
        hl_client=hl,
        market_data=md,
        inventory=inv,
        quote_engine=qe,
        order_manager=om,
        risk_manager=rm,
        metrics=metrics,
        notifier=None,
        quote_loop_interval_s=0.1,
    )

    await bot.setup()
    # Force a mid into market_data so quote_loop has something to compute against
    md._current_book = _make_book(1800.0)
    md._last_message_at = asyncio.get_event_loop().time()  # mark "fresh"
    await bot.start_components()

    # Run briefly to let quote_loop execute a few iterations
    await asyncio.sleep(0.4)
    running_after_start = bot._is_running

    # quote_loop should have tried to place orders since mid exists
    placed_or_cancelled = (
        hl.place_order.await_count > 0
        or hl.cancel_all_orders.await_count > 0
    )

    # Trigger graceful shutdown
    bot.request_shutdown()
    await bot.shutdown()
    running_after_shutdown = bot._is_running

    assert running_after_start
    assert not running_after_shutdown
    assert placed_or_cancelled


async def test_mmbot_quote_loop_skips_when_no_mid(
    tmp_path: Path, state_store: StateStore, asset_meta: AssetMeta
) -> None:
    """Ei mid:ä → quote_loop skippaa update_quotes-kutsun."""
    config = _make_config(tmp_path / "no_mid.db")
    hl = _mock_hl_client(asset_meta)

    md = MarketDataFeed(
        symbol="ETH",
        network="testnet",
        connect_factory=_ws_factory(),
        silence_timeout_s=120.0,
    )
    inv = InventoryManager(
        symbol="ETH",
        max_position=Decimal("0.1"),
        hl_client=hl,
        state_store=state_store,
        connect_factory=_ws_factory(),
        silence_timeout_s=120.0,
    )
    qe = QuoteEngine(config.trading, sz_decimals=asset_meta.sz_decimals)
    om = OrderManager(
        hl_client=hl,
        state_store=state_store,
        symbol="ETH",
        cleanup_interval_s=60.0,
    )
    rm = RiskManager(
        config=config.risk,
        inventory=inv,
        market_data=md,
        order_manager=om,
        hl_client=hl,
        state_store=state_store,
        symbol="ETH",
        session_start_capital=config.trading.capital_usdc,
        check_interval_s=60.0,
    )
    metrics = MetricsCollector(
        state_store=state_store,
        inventory=inv,
        market_data=md,
        order_manager=om,
        symbol="ETH",
        capital_usdc=config.trading.capital_usdc,
        interval_s=60.0,
    )

    bot = MMBot(
        config=config,
        state_store=state_store,
        hl_client=hl,
        market_data=md,
        inventory=inv,
        quote_engine=qe,
        order_manager=om,
        risk_manager=rm,
        metrics=metrics,
        notifier=None,
        quote_loop_interval_s=0.05,
    )

    await bot.setup()
    # Do NOT inject a book — current_mid stays None
    await bot.start_components()
    await asyncio.sleep(0.2)

    # place_order ei kutsuttu koska mid puuttuu
    assert hl.place_order.await_count == 0

    bot.request_shutdown()
    await bot.shutdown()


async def test_mmbot_kill_triggers_shutdown(
    tmp_path: Path, state_store: StateStore, asset_meta: AssetMeta
) -> None:
    """Risk kill → shutdown_watcher havaitsee → request_shutdown."""
    config = _make_config(tmp_path / "kill.db")
    hl = _mock_hl_client(asset_meta)

    md = MarketDataFeed(
        symbol="ETH",
        network="testnet",
        connect_factory=_ws_factory(),
        silence_timeout_s=120.0,
    )
    inv = InventoryManager(
        symbol="ETH",
        max_position=Decimal("0.1"),
        hl_client=hl,
        state_store=state_store,
        connect_factory=_ws_factory(),
        silence_timeout_s=120.0,
    )
    qe = QuoteEngine(config.trading, sz_decimals=asset_meta.sz_decimals)
    om = OrderManager(
        hl_client=hl,
        state_store=state_store,
        symbol="ETH",
        cleanup_interval_s=60.0,
    )
    rm = RiskManager(
        config=config.risk,
        inventory=inv,
        market_data=md,
        order_manager=om,
        hl_client=hl,
        state_store=state_store,
        symbol="ETH",
        session_start_capital=config.trading.capital_usdc,
        check_interval_s=60.0,
    )
    metrics = MetricsCollector(
        state_store=state_store,
        inventory=inv,
        market_data=md,
        order_manager=om,
        symbol="ETH",
        capital_usdc=config.trading.capital_usdc,
        interval_s=60.0,
    )

    bot = MMBot(
        config=config,
        state_store=state_store,
        hl_client=hl,
        market_data=md,
        inventory=inv,
        quote_engine=qe,
        order_manager=om,
        risk_manager=rm,
        metrics=metrics,
        notifier=None,
        quote_loop_interval_s=0.05,
    )
    await bot.setup()
    md._current_book = _make_book(1800.0)
    md._last_message_at = asyncio.get_event_loop().time()
    await bot.start_components()

    await asyncio.sleep(0.1)
    # Trigger kill — shutdown_watcher should pick it up within 1s
    await rm.trigger_kill("test forced kill")
    assert rm.is_killed

    # shutdown_event triggered by _on_risk_kill callback or watcher
    await asyncio.wait_for(bot._shutdown_event.wait(), timeout=2.5)
    await bot.shutdown()


async def test_mmbot_provider_callbacks_build_status(
    tmp_path: Path, state_store: StateStore, asset_meta: AssetMeta
) -> None:
    """Telegram-providerit palauttavat täytetyt dataclassit ilman exceptioneita."""
    config = _make_config(tmp_path / "providers.db")
    hl = _mock_hl_client(asset_meta)

    md = MarketDataFeed(
        symbol="ETH",
        network="testnet",
        connect_factory=_ws_factory(),
        silence_timeout_s=120.0,
    )
    inv = InventoryManager(
        symbol="ETH",
        max_position=Decimal("0.1"),
        hl_client=hl,
        state_store=state_store,
        connect_factory=_ws_factory(),
        silence_timeout_s=120.0,
    )
    qe = QuoteEngine(config.trading, sz_decimals=asset_meta.sz_decimals)
    om = OrderManager(
        hl_client=hl,
        state_store=state_store,
        symbol="ETH",
        cleanup_interval_s=60.0,
    )
    rm = RiskManager(
        config=config.risk,
        inventory=inv,
        market_data=md,
        order_manager=om,
        hl_client=hl,
        state_store=state_store,
        symbol="ETH",
        session_start_capital=config.trading.capital_usdc,
        check_interval_s=60.0,
    )
    metrics = MetricsCollector(
        state_store=state_store,
        inventory=inv,
        market_data=md,
        order_manager=om,
        symbol="ETH",
        capital_usdc=config.trading.capital_usdc,
        interval_s=60.0,
    )

    bot = MMBot(
        config=config,
        state_store=state_store,
        hl_client=hl,
        market_data=md,
        inventory=inv,
        quote_engine=qe,
        order_manager=om,
        risk_manager=rm,
        metrics=metrics,
        notifier=None,
    )

    status = bot._build_status_info()
    assert status.position == "0 ETH"
    assert status.is_killed is False
    assert status.is_paused is False

    pnl = bot._build_pnl_info()
    assert "USDC" in pnl.realized_pnl

    inv_info = bot._build_inventory_info()
    assert "ETH" in inv_info.position

    orders = bot._build_order_list()
    assert orders == []


async def test_mmbot_inventory_fill_forwarded_to_order_manager(
    tmp_path: Path, state_store: StateStore, asset_meta: AssetMeta
) -> None:
    """Inventory.apply_fill → on_raw_fill → OrderManager.handle_fill → metrics.on_fill."""
    config = _make_config(tmp_path / "fill_chain.db")
    hl = _mock_hl_client(asset_meta)

    md = MarketDataFeed(
        symbol="ETH",
        network="testnet",
        connect_factory=_ws_factory(),
        silence_timeout_s=120.0,
    )
    inv = InventoryManager(
        symbol="ETH",
        max_position=Decimal("0.1"),
        hl_client=hl,
        state_store=state_store,
        connect_factory=_ws_factory(),
        silence_timeout_s=120.0,
    )
    qe = QuoteEngine(config.trading, sz_decimals=asset_meta.sz_decimals)
    om = OrderManager(
        hl_client=hl,
        state_store=state_store,
        symbol="ETH",
        cleanup_interval_s=60.0,
    )
    rm = RiskManager(
        config=config.risk,
        inventory=inv,
        market_data=md,
        order_manager=om,
        hl_client=hl,
        state_store=state_store,
        symbol="ETH",
        session_start_capital=config.trading.capital_usdc,
        check_interval_s=60.0,
    )
    metrics = MetricsCollector(
        state_store=state_store,
        inventory=inv,
        market_data=md,
        order_manager=om,
        symbol="ETH",
        capital_usdc=config.trading.capital_usdc,
        interval_s=60.0,
    )

    bot = MMBot(
        config=config,
        state_store=state_store,
        hl_client=hl,
        market_data=md,
        inventory=inv,
        quote_engine=qe,
        order_manager=om,
        risk_manager=rm,
        metrics=metrics,
        notifier=None,
    )
    await bot.setup()
    # Force a mid so adverse-selection has a baseline
    md._current_book = _make_book(1800.0)

    # Apply a synthetic fill directly to the inventory
    fill = FillEntry(
        timestamp_ms=1_700_000_000_000,
        symbol="ETH",
        side="bid",
        price=Decimal("1799.5"),
        size=Decimal("0.01"),
        fee=Decimal("0.0001"),
        oid=42,
        is_maker=True,
        tid=1234,
    )
    applied = await inv.apply_fill(fill)
    assert applied is True
    # metrics.on_fill should have incremented bid count via the callback chain
    assert metrics.fill_count_bid == 1
