"""Main event loop / orchestrator.

Käynnistää kaikki komponentit (state_store, hl_client, market_data, inventory,
quote_engine, order_manager, risk_manager, notifier, metrics) ja ajaa
`quote_loop`-rinnakkaistehtävän joka periodisesti laskee tavoite-quotet ja
diffaa ne aktiivisia ordereita vastaan.

Lifecycle:
    1. `setup()` — avaa state_store, fetcheää asset_meta, päivittää
        sz_decimals quote_engineen, kytkee callbacks-verkon.
    2. `start()` — käynnistää kaikki taustatehtävät rekisterin kautta.
    3. `quote_loop()` — pää-quote-cycle (huomioi is_paused / is_killed).
    4. `wait_for_shutdown()` — odottaa SIGINT/SIGTERM tai kill:iä,
        ajaa `shutdown()`:n kontrolloidusti.
    5. `shutdown()` — peruuttaa kaikki orderit, pysäyttää tehtävät,
        sulkee state_store:n.

Task registry:
    Pidetään `_tasks: dict[str, asyncio.Task]`. Shutdown:in yhteydessä
    cancel + wait kaikki, force timeout 5s jälkeen.

Signaalit:
    SIGINT (Ctrl+C) ja SIGTERM kytketään loopin signal handleriin
    (Linux). Windowsilla KeyboardInterrupt-poikkeus napataan asyncio.run-tasolla.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import time
from contextlib import suppress
from decimal import Decimal
from typing import Any

from src.config import BotConfig, load_config
from src.hl_client import AssetMeta, FillEntry, HLClient
from src.inventory import InventoryManager, InventorySnapshot
from src.logger import setup_logging
from src.market_data import MarketDataFeed, OrderBook
from src.metrics import MetricsCollector
from src.notifier import (
    InventoryInfo,
    OrderListItem,
    PnLInfo,
    StatusInfo,
    TelegramNotifier,
)
from src.order_manager import OrderInfo, OrderManager
from src.quote_engine import QuoteEngine
from src.risk import RiskManager, RiskStatus
from src.sim_fill import SimulatedFillEngine
from src.state import EventRecord, StateStore

_logger = logging.getLogger(__name__)

DEFAULT_QUOTE_LOOP_MIN_INTERVAL_S = 0.1
SHUTDOWN_TIMEOUT_S = 5.0


# ---------- Banner ----------


def _banner(config: BotConfig) -> str:
    """Iso banner käynnistykseen — selkeä erottelu testnet/mainnet."""
    network = config.hyperliquid.network.upper()
    flag = "MAINNET" if network == "MAINNET" else "TESTNET"
    bar = "=" * 64
    return (
        f"\n{bar}\n"
        f"  HYPERLIQUID MM BOT v0.1 — {flag}\n"
        f"  Symbol: {config.trading.symbol}  "
        f"Capital: {config.trading.capital_usdc} USDC  "
        f"Dry-run: {config.operations.dry_run}\n"
        f"{bar}\n"
    )


# ---------- Orchestrator ----------


class MMBot:
    """Komponenttien kokoaja + lifecycle-kontrolli.

    Komponentit voidaan injektoida konstruktorissa testausta varten;
    muutoin `from_config()` rakentaa oletukset BotConfigista.
    """

    def __init__(
        self,
        config: BotConfig,
        *,
        state_store: StateStore,
        hl_client: HLClient,
        market_data: MarketDataFeed,
        inventory: InventoryManager,
        quote_engine: QuoteEngine,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        metrics: MetricsCollector,
        notifier: TelegramNotifier | None = None,
        sim_fill: SimulatedFillEngine | None = None,
        quote_loop_interval_s: float | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.hl_client = hl_client
        self.market_data = market_data
        self.inventory = inventory
        self.quote_engine = quote_engine
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.metrics = metrics
        self.notifier = notifier
        self.sim_fill = sim_fill

        if quote_loop_interval_s is None:
            quote_loop_interval_s = max(
                DEFAULT_QUOTE_LOOP_MIN_INTERVAL_S,
                config.trading.quote_refresh_ms / 1000.0,
            )
        self._quote_loop_interval_s = quote_loop_interval_s

        self._start_time = time.time()
        self._shutdown_event = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._asset_meta: AssetMeta | None = None
        self._is_running = False

    # ----- Construction -----

    @classmethod
    async def from_config(
        cls, config: BotConfig, *, private_key: str | None = None
    ) -> MMBot:
        """Rakenna oletus-bot BotConfigista. Lukee HL_PRIVATE_KEY env:istä.

        Dry-run: jos `config.operations.dry_run == True`, HL_PRIVATE_KEY ei
        ole pakollinen — käytetään offline-stub:ja oikeiden API-kutsujen
        sijaan, ja luodaan SimulatedFillEngine täyttämään orderit
        todennäköisin perustein.
        """
        dry_run = config.operations.dry_run
        key = private_key or os.environ.get("HL_PRIVATE_KEY", "")
        if not key and not dry_run:
            raise RuntimeError(
                "HL_PRIVATE_KEY env var is required (set it in .env). "
                "Aja botti dry-run-tilassa jos sinulla ei ole avainta."
            )
        state_store = StateStore(config.storage.db_path)
        await state_store.open()
        await state_store.migrate()

        hl_client = HLClient(config.hyperliquid, key, dry_run=dry_run)
        market_data = MarketDataFeed(
            config.trading.symbol,
            network=config.hyperliquid.network,
        )
        inventory = InventoryManager(
            symbol=config.trading.symbol,
            max_position=config.trading.max_position_size,
            hl_client=hl_client,
            state_store=state_store,
            network=config.hyperliquid.network,
        )
        quote_engine = QuoteEngine(config.trading)
        order_manager = OrderManager(
            hl_client=hl_client,
            state_store=state_store,
            symbol=config.trading.symbol,
            max_order_age_seconds=config.trading.max_order_age_seconds,
        )
        risk_manager = RiskManager(
            config=config.risk,
            inventory=inventory,
            market_data=market_data,
            order_manager=order_manager,
            hl_client=hl_client,
            state_store=state_store,
            symbol=config.trading.symbol,
            session_start_capital=config.trading.capital_usdc,
        )
        metrics = MetricsCollector(
            state_store=state_store,
            inventory=inventory,
            market_data=market_data,
            order_manager=order_manager,
            symbol=config.trading.symbol,
            capital_usdc=config.trading.capital_usdc,
            interval_s=config.storage.metrics_interval_seconds,
        )
        notifier = _build_notifier(config)
        sim_fill = (
            SimulatedFillEngine(
                hl_client=hl_client,
                market_data=market_data,
                symbol=config.trading.symbol,
                on_fill=inventory.apply_fill,
            )
            if dry_run
            else None
        )

        return cls(
            config=config,
            state_store=state_store,
            hl_client=hl_client,
            market_data=market_data,
            inventory=inventory,
            quote_engine=quote_engine,
            order_manager=order_manager,
            risk_manager=risk_manager,
            metrics=metrics,
            notifier=notifier,
            sim_fill=sim_fill,
        )

    # ----- Setup / wiring -----

    async def setup(self) -> None:
        """Fetch meta, päivitä sz_decimals, kytke callbacks."""
        try:
            meta = await self.hl_client.get_asset_meta(self.config.trading.symbol)
            self._asset_meta = meta
            self.quote_engine.update_sz_decimals(meta.sz_decimals)
            _logger.info(
                "asset meta loaded symbol=%s sz_decimals=%d max_leverage=%d",
                meta.symbol, meta.sz_decimals, meta.max_leverage,
            )
        except Exception:
            _logger.exception(
                "failed to load asset meta for %s — using defaults",
                self.config.trading.symbol,
            )

        self._wire_callbacks()
        await self._record_event("info", "main", "bot setup complete")

    def _wire_callbacks(self) -> None:
        """Kytke callbacks-verkko komponenttien välillä.

        Komponenteilla on cyclic-deps (inventory → order_manager → metrics →
        inventory), joten callbacks asetetaan post-construction:issa.
        Käytetään private-attr asignaatiota — kierretään cyclic-importit
        ja vältetään pakollinen public-API laajennus jokaiseen luokkaan.
        """
        self.market_data._on_book_update = self._on_book_update
        self.market_data._on_disconnect = self._on_disconnect
        self.inventory._on_raw_fill = self._on_inventory_raw_fill
        self.inventory._on_change = self._on_inventory_change
        self.order_manager._on_fill = self.metrics.on_fill
        self.order_manager._on_cancelled = self._on_order_cancelled
        # risk_manager → notifier (kill / pause)
        self.risk_manager._on_kill = self._on_risk_kill
        self.risk_manager._on_pause_change = self._on_risk_pause_change
        if self.notifier is not None:
            self._wire_notifier_providers()

    def _wire_notifier_providers(self) -> None:
        if self.notifier is None:
            return
        self.notifier._status_provider = self._build_status_info
        self.notifier._pnl_provider = self._build_pnl_info
        self.notifier._inventory_provider = self._build_inventory_info
        self.notifier._orders_provider = self._build_order_list
        self.notifier._on_pause = self._notifier_pause
        self.notifier._on_resume = self._notifier_resume
        self.notifier._on_kill = self._notifier_kill

    # ----- Lifecycle -----

    async def start_components(self) -> None:
        """Käynnistä kaikki taustatehtävät rekisterin kautta."""
        await self.market_data.start()
        await self.inventory.start()
        await self.order_manager.start()
        await self.risk_manager.start()
        await self.metrics.start()
        if self.sim_fill is not None:
            await self.sim_fill.start()
        if self.notifier is not None:
            try:
                await self.notifier.start()
            except Exception:
                _logger.exception("notifier failed to start — continuing without")
        self._tasks["quote_loop"] = asyncio.create_task(
            self.quote_loop(), name="quote-loop"
        )
        self._tasks["shutdown_watcher"] = asyncio.create_task(
            self._shutdown_watcher(), name="shutdown-watcher"
        )
        self._is_running = True

    async def run(self) -> None:
        """Setup → start → wait for shutdown → cleanup."""
        await self.setup()
        await self.start_components()
        try:
            await self._shutdown_event.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Peruuta orderit, pysäytä tehtävät, sulje yhteydet."""
        if not self._is_running:
            return
        self._is_running = False
        _logger.info("shutdown begin")
        await self._record_event("info", "main", "shutdown initiated")

        # 1) Peruuta kaikki orderit
        try:
            cancelled = await self.order_manager.cancel_all()
            _logger.info("shutdown: cancelled %d orders", cancelled)
        except Exception:
            _logger.exception("shutdown: cancel_all failed")

        # 2) Pysäytä tehtävät rekisteristä
        for task in list(self._tasks.values()):
            if task.done():
                continue
            task.cancel()
        for name, task in list(self._tasks.items()):
            try:
                await asyncio.wait_for(task, timeout=SHUTDOWN_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError):
                _logger.warning("shutdown: task %s did not stop in time", name)
            except Exception:
                _logger.exception("shutdown: task %s raised", name)
        self._tasks.clear()

        # 3) Sulje komponentit
        stop_fns: list[tuple[str, Any]] = [
            ("metrics", self.metrics.stop),
            ("risk", self.risk_manager.stop),
            ("order_manager", self.order_manager.stop),
            ("inventory", self.inventory.stop),
            ("market_data", self.market_data.stop),
        ]
        if self.sim_fill is not None:
            stop_fns.insert(0, ("sim_fill", self.sim_fill.stop))
        for name, stop_fn in stop_fns:
            try:
                await stop_fn()
            except Exception:
                _logger.exception("shutdown: %s.stop failed", name)
        if self.notifier is not None:
            try:
                await self.notifier.stop()
            except Exception:
                _logger.exception("shutdown: notifier.stop failed")
        try:
            await self.state_store.close()
        except Exception:
            _logger.exception("shutdown: state_store.close failed")

        _logger.info("shutdown complete")

    def request_shutdown(self) -> None:
        """Triggeröi graceful shutdown (signal handlerista tai callbackista)."""
        self._shutdown_event.set()

    async def _shutdown_watcher(self) -> None:
        """Tarkista onko risk_manager triggeröinyt killin → shutdown."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                return
            if self.risk_manager.is_killed:
                _logger.warning(
                    "risk killed (%s) — initiating shutdown",
                    self.risk_manager.kill_reason,
                )
                self._shutdown_event.set()
                return

    # ----- Quote loop -----

    async def quote_loop(self) -> None:
        """Pää-quote-cycle. Laskee tavoitteet ja diffaa ne aktiivisia vastaan."""
        symbol = self.config.trading.symbol
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._quote_loop_interval_s,
                )
                return
            except TimeoutError:
                pass

            if self.risk_manager.is_killed:
                # Kill — shutdown_watcher hoitaa lopetuksen, peruuta varmuudeksi.
                with suppress(Exception):
                    await self.order_manager.cancel_all()
                continue

            if self.risk_manager.is_paused:
                with suppress(Exception):
                    await self.order_manager.cancel_all()
                continue

            mid = self.market_data.current_mid
            if mid is None or mid <= 0:
                continue

            try:
                vol = self.market_data.realized_volatility(60)
                skew = self.inventory.inventory_skew
                can_bid = self.inventory.can_quote_bid()
                can_ask = self.inventory.can_quote_ask()
                target = self.quote_engine.compute_quotes(
                    mid=mid,
                    inventory_skew=skew,
                    volatility=vol,
                    can_bid=can_bid,
                    can_ask=can_ask,
                )
                t0 = time.perf_counter()
                await self.order_manager.update_quotes(target)
                latency_ms = (time.perf_counter() - t0) * 1000.0
                self.metrics.record_quote_latency_ms(latency_ms)
            except Exception:
                _logger.exception("quote_loop iteration failed symbol=%s", symbol)
                self.risk_manager.record_api_error("quote_loop iteration")

    # ----- Risk callbacks -----

    async def _on_risk_kill(self, reason: str, _status: RiskStatus) -> None:
        _logger.critical("risk kill triggered: %s", reason)
        if self.notifier is not None:
            with suppress(Exception):
                await self.notifier.send_alert(
                    "critical", f"KILL: {reason}", force=True
                )
        self._shutdown_event.set()

    async def _on_risk_pause_change(
        self, paused: bool, reason: str | None
    ) -> None:
        if self.notifier is None:
            return
        msg = (
            f"PAUSED: {reason}" if paused else f"RESUMED ({reason or 'auto'})"
        )
        level = "warning" if paused else "info"
        with suppress(Exception):
            await self.notifier.send_alert(level, msg)  # type: ignore[arg-type]

    # ----- Market data callbacks -----

    async def _on_book_update(self, _book: OrderBook) -> None:
        # quote_loop pollaa intervallilla — ei trigger:iä per book-update
        return None

    async def _on_disconnect(self) -> None:
        _logger.warning("market data disconnected")
        if self.notifier is not None:
            with suppress(Exception):
                await self.notifier.send_alert(
                    "warning", "Market data WebSocket disconnected"
                )

    # ----- Inventory callbacks -----

    async def _on_inventory_change(self, snap: InventorySnapshot) -> None:
        _logger.debug(
            "inventory change position=%s realized=%s",
            snap.position, snap.realized_pnl,
        )

    async def _on_inventory_raw_fill(self, fill: FillEntry) -> None:
        """Forward raw fill to order_manager (latency + active-state cleanup)."""
        with suppress(Exception):
            await self.order_manager.handle_fill(fill)

    async def _on_order_cancelled(self, info: OrderInfo) -> None:
        self.metrics.on_cancel(info)

    # ----- Notifier providers -----

    def _build_status_info(self) -> StatusInfo:
        bid, ask = 0, 0
        for o in self.order_manager.active_orders.values():
            if o.side == "bid":
                bid += 1
            else:
                ask += 1
        active = bid + ask
        rs = self.risk_manager
        session_pnl = (
            self.inventory.realized_pnl - self.inventory.total_fees
        )
        position_str = (
            f"{self.inventory.current_position} {self.config.trading.symbol}"
        )
        return StatusInfo(
            uptime_seconds=time.time() - self._start_time,
            is_paused=rs.is_paused,
            is_killed=rs.is_killed,
            pause_reason=rs.pause_reason,
            kill_reason=rs.kill_reason,
            session_pnl=f"{session_pnl} USDC",
            position=position_str,
            active_orders=active,
        )

    def _build_pnl_info(self) -> PnLInfo:
        summary = self.metrics.get_summary()
        capital = self.config.trading.capital_usdc
        pct = (
            (summary.net_pnl / capital * Decimal("100"))
            if capital > 0
            else Decimal("0")
        )
        return PnLInfo(
            realized_pnl=f"{summary.realized_pnl} USDC",
            unrealized_pnl=f"{summary.unrealized_pnl} USDC",
            total_fees=f"{summary.total_fees} USDC",
            rebate_earned=f"{summary.rebate_earned} USDC",
            session_pnl_pct=f"{pct:.2f}%",
        )

    def _build_inventory_info(self) -> InventoryInfo:
        pos = self.inventory.current_position
        return InventoryInfo(
            position=f"{pos} {self.config.trading.symbol}",
            avg_entry=f"{self.inventory.avg_entry_price}",
            value_usd=f"{self.inventory.position_value_usd(self.market_data.current_mid)} USDC",
            skew=f"{self.inventory.inventory_skew}",
        )

    def _build_order_list(self) -> list[OrderListItem]:
        items: list[OrderListItem] = []
        now_ms = int(time.time() * 1000)
        for o in self.order_manager.active_orders.values():
            items.append(
                OrderListItem(
                    side=o.side,
                    price=str(o.price),
                    size=str(o.size),
                    age_seconds=o.age_seconds(now_ms),
                )
            )
        items.sort(key=lambda x: (x.side, x.price))
        return items

    # ----- Notifier callbacks (commands) -----

    async def _notifier_pause(self) -> None:
        await self.risk_manager._set_pause(True, "manual via Telegram")

    async def _notifier_resume(self) -> None:
        await self.risk_manager._set_pause(False, None)

    async def _notifier_kill(self, reason: str) -> None:
        await self.risk_manager.trigger_kill(reason)

    # ----- Internal helpers -----

    async def _record_event(
        self, level: str, component: str, message: str
    ) -> None:
        with suppress(Exception):
            await self.state_store.record_event(
                EventRecord(
                    timestamp_ms=int(time.time() * 1000),
                    level=level,  # type: ignore[arg-type]
                    component=component,
                    message=message,
                )
            )


# ---------- Helpers ----------


def _build_notifier(config: BotConfig) -> TelegramNotifier | None:
    """Rakenna TelegramNotifier env-pohjaisesta tokenista, None jos puuttuu."""
    token_env = config.telegram.bot_token_env
    chat_env = config.telegram.chat_id_env
    token = (
        config.telegram.bot_token.get_secret_value()
        if config.telegram.bot_token is not None
        else os.environ.get(token_env, "")
    )
    chat_id = config.telegram.chat_id or os.environ.get(chat_env, "")
    if not token or not chat_id:
        _logger.warning(
            "Telegram bot disabled (missing %s or %s)", token_env, chat_env
        )
        return None
    return TelegramNotifier(
        bot_token=token,
        chat_id=chat_id,
        rate_limit_seconds=float(
            config.telegram.notification_rate_limit_seconds
        ),
    )


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, bot: MMBot
) -> None:
    """Asenna SIGINT/SIGTERM-handlerit (Linux/macOS)."""
    if platform.system() == "Windows":
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, bot.request_shutdown)


# ---------- Entrypoint ----------


async def async_main() -> None:
    """Async-entry: lataa config, käynnistä bot, odota shutdown."""
    config = load_config()
    setup_logging(config.storage.log_path)
    print(_banner(config))

    bot = await MMBot.from_config(config)
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, bot)
    await bot.run()


def main() -> None:
    """Synchronous entrypoint — käytetään `python -m src.main`-kutsussa."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        _logger.info("interrupted by user")


if __name__ == "__main__":
    main()
