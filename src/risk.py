"""Risk manager — kill switchit, volatility halt, emergency close.

Tarkistuksia ajetaan joka sekunti `_check_loop`:n alla. Kill triggeröi
`emergency_close`:n joka peruuttaa kaikki orderit ja sulkee position
market-orderilla (taker — ainoa kerta kun otamme likviditeettiä).

Kill switchit:
    a) Connection silence > timeout → kill
    b) Session PnL % < -max_loss_pct → kill
    c) Daily PnL % < -daily_max_loss_pct → kill
    d) |position| > max_position * inventory_hard_stop_multiplier → kill
    e) Realized vol > max_vol_pct_1min → PAUSE (ei kill, hysteresis 0.7x)
    f) API error rate > max_api_errors_per_minute → kill
    g) Funding rate vs position → warning (ei kill)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.hl_client import HLClient, HLClientError
from src.inventory import InventoryManager
from src.market_data import MarketDataFeed
from src.order_manager import OrderManager
from src.state import EventRecord, StateStore

DEFAULT_CONNECTION_TIMEOUT_S = 10.0
DEFAULT_CHECK_INTERVAL_S = 1.0
DEFAULT_VOL_HYSTERESIS_FACTOR = 0.7
DEFAULT_EMERGENCY_SLIPPAGE = Decimal("0.01")  # 100 bps aggressive close
DEFAULT_API_ERROR_WINDOW_S = 60.0

_logger = logging.getLogger(__name__)


# ---------- Status snapshot ----------


@dataclass(frozen=True)
class RiskStatus:
    is_killed: bool
    is_paused: bool
    kill_reason: str | None
    pause_reason: str | None
    session_pnl: Decimal
    session_pnl_pct: Decimal
    daily_pnl: Decimal
    daily_pnl_pct: Decimal
    api_errors_last_60s: int
    seconds_since_last_message: float | None
    realized_volatility_60s: float | None
    inventory_position: Decimal
    timestamp_ms: int


# ---------- Callbacks ----------

_KillCallback = Callable[[str, RiskStatus], Awaitable[None] | None]
_PauseChangeCallback = Callable[[bool, str | None], Awaitable[None] | None]


# ---------- RiskManager ----------


class RiskManager:
    """Aktiiviset kill switchit + volatility halt + emergency close.

    Args:
        config: RiskConfig (max_loss_pct, daily_max_loss_pct, ...).
        inventory: InventoryManager — position + realized PnL.
        market_data: MarketDataFeed — mid + vol + connection-status.
        order_manager: OrderManager — cancel_all emergencyssä.
        hl_client: HLClient — emergency close market-orderille.
        state_store: StateStore — kill/warning-eventit.
        symbol: kohde-symboli.
        session_start_capital: aloituspääoma (USDC).
        connection_timeout_s: kuinka pitkä WS-hiljaisuus → kill.
        check_interval_s: kuinka usein check_all ajetaan.
        vol_hysteresis_factor: kerroin paluulle vol-pause:sta (0.7 → 70%
            threshold:sta riittää resume:lle).
        emergency_slippage: aggressive price -multiplier emergency close:ssa.
        on_kill: callback (reason, status) kill-tapahtumassa.
        on_pause_change: callback (is_paused, reason) kun pause-tila muuttuu.
    """

    def __init__(
        self,
        config: Any,  # RiskConfig — avoid circular import
        inventory: InventoryManager,
        market_data: MarketDataFeed,
        order_manager: OrderManager,
        hl_client: HLClient,
        state_store: StateStore,
        symbol: str,
        session_start_capital: Decimal,
        *,
        connection_timeout_s: float = DEFAULT_CONNECTION_TIMEOUT_S,
        check_interval_s: float = DEFAULT_CHECK_INTERVAL_S,
        vol_hysteresis_factor: float = DEFAULT_VOL_HYSTERESIS_FACTOR,
        emergency_slippage: Decimal = DEFAULT_EMERGENCY_SLIPPAGE,
        on_kill: _KillCallback | None = None,
        on_pause_change: _PauseChangeCallback | None = None,
    ) -> None:
        if session_start_capital <= 0:
            raise ValueError("session_start_capital must be > 0")
        if not 0 < vol_hysteresis_factor < 1:
            raise ValueError("vol_hysteresis_factor must be in (0, 1)")
        self._config = config
        self._inventory = inventory
        self._market_data = market_data
        self._order_manager = order_manager
        self._hl = hl_client
        self._store = state_store
        self._symbol = symbol
        self._session_start_capital = session_start_capital
        self._connection_timeout_s = connection_timeout_s
        self._check_interval_s = check_interval_s
        self._vol_hysteresis = Decimal(str(vol_hysteresis_factor))
        self._emergency_slippage = emergency_slippage
        self._on_kill = on_kill
        self._on_pause_change = on_pause_change

        self._is_killed = False
        self._is_paused = False
        self._kill_reason: str | None = None
        self._pause_reason: str | None = None
        self._session_start_time = time.time()
        self._daily_start_pnl = Decimal("0")
        self._daily_start_day = datetime.now(UTC).date()
        self._api_error_timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._check_task: asyncio.Task[None] | None = None

    # ----- Properties -----

    @property
    def is_killed(self) -> bool:
        return self._is_killed

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    @property
    def kill_reason(self) -> str | None:
        return self._kill_reason

    @property
    def pause_reason(self) -> str | None:
        return self._pause_reason

    @property
    def api_errors_last_60s(self) -> int:
        self._trim_api_errors()
        return len(self._api_error_timestamps)

    # ----- API error tracking -----

    def record_api_error(self, error: BaseException | str | None = None) -> None:
        """Lisää API-virhe sliding-window:iin. Ulkoinen kirjaaja (esim. main.py)
        kutsuu tämän kun hl_client raiseaa."""
        self._api_error_timestamps.append(time.monotonic())
        self._trim_api_errors()
        if error is not None:
            _logger.debug("recorded API error: %s", error)

    def _trim_api_errors(self) -> None:
        cutoff = time.monotonic() - DEFAULT_API_ERROR_WINDOW_S
        while self._api_error_timestamps and self._api_error_timestamps[0] < cutoff:
            self._api_error_timestamps.popleft()

    # ----- PnL -----

    def _compute_session_pnl(self) -> Decimal:
        realized = self._inventory.realized_pnl
        fees = self._inventory.total_fees
        pos = self._inventory.current_position
        avg_entry = self._inventory.avg_entry_price
        mid = self._market_data.current_mid
        unrealized = Decimal("0")
        if mid is not None and pos != 0 and avg_entry > 0:
            unrealized = pos * (mid - avg_entry)
        return realized - fees + unrealized

    def _check_daily_rollover(self, current_pnl: Decimal) -> None:
        now_day = datetime.now(UTC).date()
        if now_day != self._daily_start_day:
            self._daily_start_day = now_day
            self._daily_start_pnl = current_pnl

    # ----- Status snapshot -----

    def _snapshot_status(self) -> RiskStatus:
        session_pnl = self._compute_session_pnl()
        self._check_daily_rollover(session_pnl)
        daily_pnl = session_pnl - self._daily_start_pnl
        session_pct = (
            session_pnl / self._session_start_capital * Decimal("100")
            if self._session_start_capital > 0
            else Decimal("0")
        )
        daily_pct = (
            daily_pnl / self._session_start_capital * Decimal("100")
            if self._session_start_capital > 0
            else Decimal("0")
        )
        return RiskStatus(
            is_killed=self._is_killed,
            is_paused=self._is_paused,
            kill_reason=self._kill_reason,
            pause_reason=self._pause_reason,
            session_pnl=session_pnl,
            session_pnl_pct=session_pct,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pct,
            api_errors_last_60s=self.api_errors_last_60s,
            seconds_since_last_message=(
                self._market_data.seconds_since_last_message()
            ),
            realized_volatility_60s=self._market_data.realized_volatility(60),
            inventory_position=self._inventory.current_position,
            timestamp_ms=int(time.time() * 1000),
        )

    # ----- Main check -----

    async def check_all(self) -> RiskStatus:
        """Aja kaikki kill-switchit. Palauttaa RiskStatus-snapshotin."""
        if self._is_killed:
            return self._snapshot_status()

        # a) Connection check
        sec_since_msg = self._market_data.seconds_since_last_message()
        if (
            sec_since_msg is not None
            and sec_since_msg > self._connection_timeout_s
        ):
            await self.trigger_kill(
                f"WS silent {sec_since_msg:.1f}s > {self._connection_timeout_s}s"
            )
            return self._snapshot_status()

        # b) Session PnL
        session_pnl = self._compute_session_pnl()
        self._check_daily_rollover(session_pnl)
        session_pct = session_pnl / self._session_start_capital * Decimal("100")
        if session_pct < Decimal(str(-self._config.max_loss_pct)):
            await self.trigger_kill(
                f"session loss {session_pct:.2f}% < -{self._config.max_loss_pct}%"
            )
            return self._snapshot_status()

        # c) Daily PnL
        daily_pnl = session_pnl - self._daily_start_pnl
        daily_pct = daily_pnl / self._session_start_capital * Decimal("100")
        if daily_pct < Decimal(str(-self._config.daily_max_loss_pct)):
            await self.trigger_kill(
                f"daily loss {daily_pct:.2f}% < -{self._config.daily_max_loss_pct}%"
            )
            return self._snapshot_status()

        # d) Inventory hard stop
        pos_abs = abs(self._inventory.current_position)
        hard_stop = self._inventory.max_position * Decimal(
            str(self._config.inventory_hard_stop_multiplier)
        )
        if pos_abs > hard_stop:
            await self.trigger_kill(
                f"inventory hard stop: |{self._inventory.current_position}| > {hard_stop}"
            )
            return self._snapshot_status()

        # e) Volatility halt (with hysteresis)
        await self._check_volatility_halt()

        # f) API error rate
        self._trim_api_errors()
        if len(self._api_error_timestamps) > self._config.max_api_errors_per_minute:
            await self.trigger_kill(
                f"API error rate {len(self._api_error_timestamps)} > "
                f"{self._config.max_api_errors_per_minute}/min"
            )
            return self._snapshot_status()

        # g) Funding check on jätetty erilliseen tehtävään (STEP 12 metrics)

        return self._snapshot_status()

    async def _check_volatility_halt(self) -> None:
        vol = self._market_data.realized_volatility(60)
        if vol is None:
            return
        threshold = float(self._config.max_vol_pct_1min)
        resume_threshold = threshold * float(self._vol_hysteresis)
        if not self._is_paused and vol > threshold:
            await self._set_pause(True, f"vol halt: {vol:.3f} > {threshold}")
        elif self._is_paused and vol < resume_threshold:
            await self._set_pause(False, None)

    async def _set_pause(self, paused: bool, reason: str | None) -> None:
        if self._is_paused == paused:
            return
        self._is_paused = paused
        self._pause_reason = reason
        await self._record_event(
            "warning" if paused else "info",
            "risk-pause",
            f"is_paused={paused} reason={reason or 'auto-resume'}",
        )
        await self._invoke_pause_callback(paused, reason)

    # ----- Kill / emergency close -----

    async def trigger_kill(self, reason: str) -> None:
        """Triggeröi kill: emergency_close + state-event + callback."""
        async with self._lock:
            if self._is_killed:
                return
            self._is_killed = True
            self._kill_reason = reason
        _logger.critical("RISK KILL: %s", reason)
        await self._record_event("kill", "risk", reason)
        try:
            await self.emergency_close()
        except Exception:
            _logger.exception("emergency_close failed during kill")
        status = self._snapshot_status()
        await self._invoke_kill_callback(reason, status)

    async def emergency_close(self) -> None:
        """Cancel all orders + close position market-orderilla.

        Tämä on AINOA paikka jossa otamme taker-likviditeettiä. Käytetään
        post_only=False ja aggressiivinen hinta jotta orderi täyttyy heti.
        """
        try:
            await self._order_manager.cancel_all()
        except Exception:
            _logger.exception("cancel_all failed in emergency_close")

        pos = self._inventory.current_position
        if pos == 0:
            return

        mid = self._market_data.current_mid
        if mid is None:
            _logger.error(
                "emergency_close: no mid price available, cannot close position %s",
                pos,
            )
            return

        # Long → myydään (ask side) hieman alle midin → varma fill takerina.
        # Short → ostetaan (bid side) hieman yli midin.
        if pos > 0:
            side = "ask"
            close_price = mid * (Decimal("1") - self._emergency_slippage)
        else:
            side = "bid"
            close_price = mid * (Decimal("1") + self._emergency_slippage)

        size = abs(pos)
        try:
            result = await self._hl.place_order(
                self._symbol,
                side,  # type: ignore[arg-type]
                close_price,
                size,
                post_only=False,
                reduce_only=True,
            )
            _logger.critical(
                "emergency close placed side=%s size=%s price=%s status=%s",
                side, size, close_price, result.status,
            )
        except HLClientError:
            _logger.exception("emergency close place_order failed")

    # ----- Lifecycle -----

    async def start(self) -> None:
        if self._check_task is not None:
            return
        self._stop_event.clear()
        self._check_task = asyncio.create_task(
            self._check_loop(), name="risk-check"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._check_task is not None:
            self._check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._check_task
            self._check_task = None

    async def _check_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._check_interval_s,
                )
                return
            except TimeoutError:
                pass
            try:
                await self.check_all()
            except Exception:
                _logger.exception("check_all iteration failed")

    # ----- Callbacks / persistence -----

    async def _record_event(
        self, level: str, component: str, message: str
    ) -> None:
        try:
            await self._store.record_event(
                EventRecord(
                    timestamp_ms=int(time.time() * 1000),
                    level=level,  # type: ignore[arg-type]
                    component=component,
                    message=message,
                )
            )
        except Exception:
            _logger.exception("failed to persist risk event")

    async def _invoke_kill_callback(
        self, reason: str, status: RiskStatus
    ) -> None:
        if self._on_kill is None:
            return
        try:
            result = self._on_kill(reason, status)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            _logger.exception("on_kill callback error")

    async def _invoke_pause_callback(
        self, paused: bool, reason: str | None
    ) -> None:
        if self._on_pause_change is None:
            return
        try:
            result = self._on_pause_change(paused, reason)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            _logger.exception("on_pause_change callback error")
