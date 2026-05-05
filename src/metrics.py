"""Metrics collector — PnL, fills, latency-percentiilit, adverse selection.

Background-task joka mittaa joka N sekunti ja kirjoittaa pnl_snapshots-tauluun.
Ulkoiset tahot (OrderManager) kutsuvat:
    - `on_fill(fill, latency_ms)` per fill
    - `on_cancel()` per cancel
    - `record_quote_latency_ms(ms)` per `update_quotes`-kutsu

Adverse selection -laskenta:
    Jokainen fill talletetaan pending-listaan + sen aikainen mid. Kun
    `adverse_window_s` on kulunut, lasketaan `(post_mid - fill_mid)` bid:lle
    negaationa ja ask:lle suoraan; positiivinen tulos = adverse (toinen
    osapuoli "valitsi" sun fillin oikealla puolella). Tulokset bps:nä,
    historiaa pidetään deque:ssa.

Päivän rollover (UTC 00:00):
    Tallennetaan summary event log:iin, nollataan session-counterit, mutta
    EI nollata position:ia tai realized PnL:ää.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np

from src.hl_client import FillEntry
from src.inventory import InventoryManager
from src.market_data import MarketDataFeed
from src.order_manager import OrderInfo, OrderManager
from src.state import EventRecord, PnLSnapshot, StateStore

DEFAULT_INTERVAL_S = 10.0
DEFAULT_ADVERSE_WINDOW_S = 10.0
DEFAULT_LATENCY_HISTORY = 1000
DEFAULT_ADVERSE_HISTORY = 200

_logger = logging.getLogger(__name__)


# ---------- Public summary ----------


@dataclass(frozen=True)
class MetricsSummary:
    """Snapshot Telegram /status:ia + PnL-loggausta varten."""

    timestamp_ms: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    net_pnl: Decimal
    spread_pnl: Decimal
    total_fees: Decimal
    rebate_earned: Decimal
    capital: Decimal
    inventory_position: Decimal
    inventory_value_usd: Decimal
    avg_entry_price: Decimal
    active_bid_count: int
    active_ask_count: int
    fill_count_bid_session: int
    fill_count_ask_session: int
    cancel_count_session: int
    quote_latency_p50_ms: float
    quote_latency_p95_ms: float
    quote_latency_p99_ms: float
    websocket_lag_seconds: float | None
    realized_volatility_60s: float | None
    adverse_selection_bps: float
    sample_count_adverse: int = field(default=0)


# ---------- Pure helpers (testattavat) ----------


def compute_adverse_bps(
    fill_side: str, fill_mid: Decimal, post_mid: Decimal
) -> float:
    """Adverse selection bp:nä yhdelle fillille.

    Bid (osto) → adverse jos hinta laskee fillin jälkeen.
    Ask (myynti) → adverse jos hinta nousee.
    Positiivinen = adverse, negatiivinen = vino sun puoleen.
    """
    if fill_mid <= 0:
        return 0.0
    # bid: bought, mid dropped → adverse positive.
    # ask: sold, mid rose → adverse positive.
    delta = (
        float(fill_mid - post_mid)
        if fill_side == "bid"
        else float(post_mid - fill_mid)
    )
    return delta / float(fill_mid) * 10000.0


def percentile(samples: list[float] | tuple[float, ...], p: float) -> float:
    """numpy-pohjainen percentile-helper. Tyhjä → 0.0."""
    if not samples:
        return 0.0
    return float(np.percentile(np.asarray(samples, dtype=np.float64), p))


# ---------- Pending adverse-fill record ----------


@dataclass
class _PendingAdverse:
    fill_side: str
    fill_mid: Decimal
    recorded_at_mono: float


# ---------- MetricsCollector ----------


class MetricsCollector:
    """Aggregoi metrikat ja tallentaa pnl_snapshot:t periodisesti."""

    def __init__(
        self,
        state_store: StateStore,
        inventory: InventoryManager,
        market_data: MarketDataFeed,
        order_manager: OrderManager,
        *,
        symbol: str,
        capital_usdc: Decimal,
        interval_s: float = DEFAULT_INTERVAL_S,
        adverse_window_s: float = DEFAULT_ADVERSE_WINDOW_S,
        latency_history_size: int = DEFAULT_LATENCY_HISTORY,
        adverse_history_size: int = DEFAULT_ADVERSE_HISTORY,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self._store = state_store
        self._inv = inventory
        self._md = market_data
        self._om = order_manager
        self._symbol = symbol
        self._capital = capital_usdc
        self._interval_s = interval_s
        self._adverse_window_s = adverse_window_s

        self._latency_samples: deque[float] = deque(maxlen=latency_history_size)
        self._adverse_history: deque[float] = deque(maxlen=adverse_history_size)
        self._pending_adverse: deque[_PendingAdverse] = deque()

        self._fill_count_bid = 0
        self._fill_count_ask = 0
        self._cancel_count = 0
        self._rebate_earned = Decimal("0")  # placeholder — pieni pilotti ei rebatea

        self._daily_start_day: date = datetime.now(UTC).date()

        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # ----- Recording API (kutsutaan ulkopuolelta) -----

    def record_quote_latency_ms(self, ms: float) -> None:
        if ms < 0:
            return
        self._latency_samples.append(float(ms))

    def on_fill(
        self,
        fill: FillEntry,
        order: OrderInfo | None = None,
        latency_ms: float = 0.0,
    ) -> None:
        """OrderManager.on_fill-callbackin signature. order voi olla None."""
        del order, latency_ms  # ei tarvita tässä metric-laskennassa
        if fill.side == "bid":
            self._fill_count_bid += 1
        else:
            self._fill_count_ask += 1
        # Talleta mid fill-hetkeltä adverse selection -mittaukseen
        mid = self._md.current_mid
        if mid is not None and mid > 0:
            self._pending_adverse.append(
                _PendingAdverse(
                    fill_side=fill.side,
                    fill_mid=mid,
                    recorded_at_mono=time.monotonic(),
                )
            )

    def on_cancel(self, _info: OrderInfo | None = None) -> None:
        self._cancel_count += 1

    def add_rebate(self, amount: Decimal) -> None:
        self._rebate_earned += amount

    # ----- Adverse selection processing -----

    def process_pending_adverse(
        self,
        *,
        now_mono: float | None = None,
        current_mid: Decimal | None = None,
    ) -> int:
        """Drainaa pending-fillit joiden ikä >= window. Palauttaa kpl."""
        if now_mono is None:
            now_mono = time.monotonic()
        if current_mid is None:
            current_mid = self._md.current_mid
        cutoff = now_mono - self._adverse_window_s
        drained = 0
        while self._pending_adverse and self._pending_adverse[0].recorded_at_mono <= cutoff:
            entry = self._pending_adverse.popleft()
            if current_mid is None or current_mid <= 0:
                continue
            bps = compute_adverse_bps(entry.fill_side, entry.fill_mid, current_mid)
            self._adverse_history.append(bps)
            drained += 1
        return drained

    # ----- Public properties (testaukseen / introspection) -----

    @property
    def latency_samples(self) -> tuple[float, ...]:
        return tuple(self._latency_samples)

    @property
    def adverse_history(self) -> tuple[float, ...]:
        return tuple(self._adverse_history)

    @property
    def fill_count_bid(self) -> int:
        return self._fill_count_bid

    @property
    def fill_count_ask(self) -> int:
        return self._fill_count_ask

    @property
    def cancel_count(self) -> int:
        return self._cancel_count

    # ----- Snapshot building -----

    def _count_active_orders(self) -> tuple[int, int]:
        bid = ask = 0
        for o in self._om.active_orders.values():
            if o.side == "bid":
                bid += 1
            else:
                ask += 1
        return bid, ask

    def get_summary(self) -> MetricsSummary:
        mid = self._md.current_mid
        realized = self._inv.realized_pnl
        fees = self._inv.total_fees
        pos = self._inv.current_position
        avg_entry = self._inv.avg_entry_price
        unrealized = Decimal("0")
        if mid is not None and pos != 0 and avg_entry > 0:
            unrealized = pos * (mid - avg_entry)
        net = realized - fees + self._rebate_earned + unrealized
        # spread_pnl = realized PnL FIFO-matchingista (gross, ennen feeitä)
        spread = realized
        bid_count, ask_count = self._count_active_orders()
        samples = list(self._latency_samples)
        adverse_avg = (
            float(np.mean(self._adverse_history))
            if self._adverse_history
            else 0.0
        )
        return MetricsSummary(
            timestamp_ms=int(time.time() * 1000),
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            net_pnl=net,
            spread_pnl=spread,
            total_fees=fees,
            rebate_earned=self._rebate_earned,
            capital=self._capital,
            inventory_position=pos,
            inventory_value_usd=self._inv.position_value_usd(mid),
            avg_entry_price=avg_entry,
            active_bid_count=bid_count,
            active_ask_count=ask_count,
            fill_count_bid_session=self._fill_count_bid,
            fill_count_ask_session=self._fill_count_ask,
            cancel_count_session=self._cancel_count,
            quote_latency_p50_ms=percentile(samples, 50),
            quote_latency_p95_ms=percentile(samples, 95),
            quote_latency_p99_ms=percentile(samples, 99),
            websocket_lag_seconds=self._md.seconds_since_last_message(),
            realized_volatility_60s=self._md.realized_volatility(60),
            adverse_selection_bps=adverse_avg,
            sample_count_adverse=len(self._adverse_history),
        )

    # ----- Persistence -----

    async def snapshot_now(self) -> MetricsSummary:
        """Drainaa adverse + tallenna PnL-snapshot state_store:en + palauta summary."""
        self.process_pending_adverse()
        summary = self.get_summary()
        try:
            await self._store.record_pnl_snapshot(
                PnLSnapshot(
                    timestamp_ms=summary.timestamp_ms,
                    realized_pnl=summary.realized_pnl,
                    unrealized_pnl=summary.unrealized_pnl,
                    inventory=summary.inventory_position,
                    capital=summary.capital,
                    spread_pnl=summary.spread_pnl,
                    rebate_earned=summary.rebate_earned,
                )
            )
        except Exception:
            _logger.exception("failed to persist pnl_snapshot")
        return summary

    # ----- Daily rollover -----

    async def _check_daily_rollover(self) -> None:
        now_day = datetime.now(UTC).date()
        if now_day == self._daily_start_day:
            return
        prev_day = self._daily_start_day
        summary = self.get_summary()
        try:
            await self._store.record_event(
                EventRecord(
                    timestamp_ms=int(time.time() * 1000),
                    level="info",
                    component="metrics",
                    message=f"daily_summary {prev_day.isoformat()}",
                    data={
                        "date": prev_day.isoformat(),
                        "realized_pnl": str(summary.realized_pnl),
                        "unrealized_pnl": str(summary.unrealized_pnl),
                        "net_pnl": str(summary.net_pnl),
                        "spread_pnl": str(summary.spread_pnl),
                        "total_fees": str(summary.total_fees),
                        "fill_count_bid": summary.fill_count_bid_session,
                        "fill_count_ask": summary.fill_count_ask_session,
                        "cancel_count": summary.cancel_count_session,
                        "adverse_selection_bps": summary.adverse_selection_bps,
                    },
                )
            )
        except Exception:
            _logger.exception("failed to persist daily summary")
        # Reset session-counterit, säilytä position + realized
        self._fill_count_bid = 0
        self._fill_count_ask = 0
        self._cancel_count = 0
        self._adverse_history.clear()
        self._latency_samples.clear()
        self._daily_start_day = now_day

    # ----- Lifecycle -----

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="metrics")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def run(self) -> None:
        """Pää-loop — snapshot + rollover joka interval_s."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_s
                )
                return
            except TimeoutError:
                pass
            try:
                await self._check_daily_rollover()
                await self.snapshot_now()
            except Exception:
                _logger.exception("metrics iteration failed")
