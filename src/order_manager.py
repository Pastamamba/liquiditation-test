"""Order manager — diff/place/cancel-logiikka aktiivisille quoteille.

Pitää kirjaa botin omista active_orders-listasta. `update_quotes(QuoteSet)`
vertaa target-quoteja muistissa oleviin ordereihin:

    - Jos active-orderi matchaa target:n hintaan price_tolerance:n sisällä → pidä
    - Jos active ei ole target-listalla → cancel
    - Jos target ei ole active-listalla → place uusi

client_order_id (cloid): 128-bit hex, generoidaan
`"0x" + timestamp_ms (12 hex) + nonce (4 hex) + secrets.token_hex(8) (16 hex)` =
32 hex-merkkiä = 128 bit:iä. Uniikki per-process myös samalla millisekunnilla
nonce-laskurin ansiosta. Hyperliquid käyttää cloidia duplikaattien estoon.

Race-conditionit (place ja cancel ovat erillisiä pyyntöjä):
    - Cancel filled order → "Order not found" → log debug, jatka
    - Place ja heti cancel → "order not yet acknowledged" — hl_client.cancel
      palauttaa False, OrderManager poistaa _active:sta seuraavalla cyclellä
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from src.hl_client import (
    FillEntry,
    HLClient,
    HLClientError,
    HLOrderRejectedError,
    OrderResult,
    OrderSide,
)
from src.quote_engine import Quote, QuoteSet
from src.state import OrderRecord, OrderStatus, StateStore

DEFAULT_PRICE_TOLERANCE = Decimal("0.0001")
DEFAULT_MAX_ORDER_AGE_S = 30
DEFAULT_CLEANUP_INTERVAL_S = 10.0

_logger = logging.getLogger(__name__)


# ---------- Types ----------


@dataclass(frozen=True)
class OrderInfo:
    """Active orderi — botin oma kirjanpito."""

    oid: int
    cloid: str
    symbol: str
    side: OrderSide
    price: Decimal
    size: Decimal
    placed_at_ms: int

    def age_seconds(self, now_ms: int | None = None) -> float:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        return max(0.0, (now_ms - self.placed_at_ms) / 1000.0)


@dataclass(frozen=True)
class UpdateResult:
    """Yhteenveto `update_quotes`-kutsusta."""

    placed: int
    cancelled: int
    kept: int
    failed_placements: int = 0
    failed_cancels: int = 0
    suppressed: bool = False
    suppressed_reason: str | None = None

    @property
    def total_actions(self) -> int:
        return self.placed + self.cancelled


@dataclass
class _SideDiff:
    to_keep: list[OrderInfo] = field(default_factory=list)
    to_cancel: list[OrderInfo] = field(default_factory=list)
    to_place: list[Quote] = field(default_factory=list)

    @property
    def kept_count(self) -> int:
        return len(self.to_keep)


# ---------- Callback types ----------

_PlacedCallback = Callable[[OrderInfo], Awaitable[None] | None]
_CancelledCallback = Callable[[OrderInfo], Awaitable[None] | None]
_FillCallback = Callable[[FillEntry, OrderInfo | None, float], Awaitable[None] | None]


# ---------- OrderManager ----------


class OrderManager:
    """Aktiivisten ordereiden kirjanpito + diff/place/cancel-orchestrointi."""

    def __init__(
        self,
        hl_client: HLClient,
        state_store: StateStore,
        symbol: str,
        *,
        price_tolerance: Decimal = DEFAULT_PRICE_TOLERANCE,
        max_order_age_seconds: int = DEFAULT_MAX_ORDER_AGE_S,
        cleanup_interval_s: float = DEFAULT_CLEANUP_INTERVAL_S,
        on_order_placed: _PlacedCallback | None = None,
        on_order_cancelled: _CancelledCallback | None = None,
        on_fill: _FillCallback | None = None,
    ) -> None:
        if price_tolerance < 0:
            raise ValueError("price_tolerance must be >= 0")
        self._hl = hl_client
        self._store = state_store
        self.symbol = symbol
        self._price_tolerance = price_tolerance
        self._max_order_age_seconds = max_order_age_seconds
        self._cleanup_interval_s = cleanup_interval_s
        self._on_placed = on_order_placed
        self._on_cancelled = on_order_cancelled
        self._on_fill = on_fill

        self._active: dict[int, OrderInfo] = {}
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._cloid_counter = 0

    # ----- Properties -----

    @property
    def active_orders(self) -> dict[int, OrderInfo]:
        """Snapshot aktiivisista ordereista (kopio, turvallinen iteroida)."""
        return dict(self._active)

    @property
    def active_count(self) -> int:
        return len(self._active)

    # ----- cloid generation -----

    def _generate_cloid(self) -> str:
        """Generoi uniikki 128-bit hex cloid: ts(12) + nonce(4) + rand(16)."""
        ts_ms = int(time.time() * 1000)
        self._cloid_counter = (self._cloid_counter + 1) & 0xFFFF
        rand = secrets.token_hex(8)
        return f"0x{ts_ms:012x}{self._cloid_counter:04x}{rand}"

    # ----- Lifecycle -----

    async def start(self) -> None:
        """Käynnistä stale-order cleanup-loop ja synkronoi muisti API:n kanssa."""
        if self._cleanup_task is not None:
            return
        await self.reconcile()
        self._stop_event.clear()
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="om-cleanup"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._cleanup_task
            self._cleanup_task = None

    async def reconcile(self) -> int:
        """Hae open orders API:lta, synkronoi _active.

        Korjaa missatut placement/cancel-tapahtumat (esim. botin restart).
        Palauttaa API:n raportoiman open-orderien lukumäärän.
        """
        try:
            api_orders = await self._hl.get_open_orders(self.symbol)
        except HLClientError:
            _logger.exception("reconcile: get_open_orders failed")
            return -1
        async with self._lock:
            api_oids = {o.oid for o in api_orders}
            # Poista paikat jotka eivät ole enää API:ssa
            for oid in list(self._active.keys()):
                if oid not in api_oids:
                    self._active.pop(oid, None)
            # Lisää API:ssa olevat joita ei muistissa
            for o in api_orders:
                if o.oid not in self._active:
                    self._active[o.oid] = OrderInfo(
                        oid=o.oid,
                        cloid=o.cloid or "",
                        symbol=self.symbol,
                        side=o.side,
                        price=o.price,
                        size=o.size,
                        placed_at_ms=o.timestamp_ms or int(time.time() * 1000),
                    )
        return len(api_orders)

    # ----- Update quotes (core logic) -----

    async def update_quotes(self, target_quotes: QuoteSet) -> UpdateResult:
        """Diff target_quotes vs _active → cancel + place tarvittaessa."""
        if target_quotes.suppressed_reason is not None:
            # QuoteEngine itse pyysi suppressointia (esim. korkea volatility).
            # Peruuta kaikki, mutta älä laita uusia.
            cancelled = await self._cancel_all_active()
            return UpdateResult(
                placed=0,
                cancelled=cancelled,
                kept=0,
                suppressed=True,
                suppressed_reason=target_quotes.suppressed_reason,
            )

        async with self._lock:
            active_bids = [o for o in self._active.values() if o.side == "bid"]
            active_asks = [o for o in self._active.values() if o.side == "ask"]
            bid_diff = self._diff_side(active_bids, target_quotes.bids)
            ask_diff = self._diff_side(active_asks, target_quotes.asks)

            cancel_targets = bid_diff.to_cancel + ask_diff.to_cancel
            place_targets: list[tuple[Quote, OrderSide]] = (
                [(q, "bid") for q in bid_diff.to_place]
                + [(q, "ask") for q in ask_diff.to_place]
            )

            cancelled = 0
            failed_cancels = 0
            for order in cancel_targets:
                ok, was_not_found = await self._cancel_single(order)
                self._active.pop(order.oid, None)
                if ok or was_not_found:
                    cancelled += 1
                else:
                    failed_cancels += 1

            placed = 0
            failed_placements = 0
            for quote, side in place_targets:
                info = await self._place_single(quote, side)
                if info is not None:
                    self._active[info.oid] = info
                    placed += 1
                else:
                    failed_placements += 1

            kept = bid_diff.kept_count + ask_diff.kept_count

        return UpdateResult(
            placed=placed,
            cancelled=cancelled,
            kept=kept,
            failed_placements=failed_placements,
            failed_cancels=failed_cancels,
        )

    def _diff_side(
        self, active: list[OrderInfo], targets: tuple[Quote, ...]
    ) -> _SideDiff:
        """Match target ↔ active hinnan tolerance:n perusteella."""
        diff = _SideDiff()
        used: set[int] = set()
        for target in targets:
            match: OrderInfo | None = None
            for ord_info in active:
                if ord_info.oid in used:
                    continue
                if abs(ord_info.price - target.price) <= self._price_tolerance:
                    match = ord_info
                    break
            if match is not None:
                diff.to_keep.append(match)
                used.add(match.oid)
            else:
                diff.to_place.append(target)
        for ord_info in active:
            if ord_info.oid not in used:
                diff.to_cancel.append(ord_info)
        return diff

    async def _cancel_single(self, order: OrderInfo) -> tuple[bool, bool]:
        """Peruuta yksi orderi.

        Palauttaa (success, was_not_found). was_not_found=True kun API:n
        mukaan orderia ei ole — käsitellään onnistuneena (todennäköisesti
        täyttynyt tai jo cancellattu).
        """
        try:
            ok = await self._hl.cancel_order(self.symbol, order.oid)
        except HLOrderRejectedError as exc:
            _logger.debug(
                "cancel rejected (already gone) oid=%s: %s", order.oid, exc
            )
            await self._record_status(order, status="cancelled")
            await self._invoke_cancelled_cb(order)
            return False, True
        except HLClientError as exc:
            _logger.warning("cancel failed oid=%s: %s", order.oid, exc)
            return False, False
        if ok:
            await self._record_status(order, status="cancelled")
            await self._invoke_cancelled_cb(order)
        return ok, False

    async def _place_single(
        self, quote: Quote, side: OrderSide
    ) -> OrderInfo | None:
        """Place yksi orderi cloid:lla. None jos failasi (validation/network)."""
        cloid = self._generate_cloid()
        try:
            result: OrderResult = await self._hl.place_order(
                self.symbol,
                side,
                quote.price,
                quote.size,
                post_only=True,
                client_order_id=cloid,
            )
        except (HLOrderRejectedError, HLClientError) as exc:
            _logger.warning(
                "place failed side=%s price=%s size=%s: %s",
                side, quote.price, quote.size, exc,
            )
            return None

        if not result.success or result.oid is None:
            _logger.info(
                "place rejected side=%s price=%s err=%s",
                side, quote.price, result.error,
            )
            return None

        info = OrderInfo(
            oid=result.oid,
            cloid=cloid,
            symbol=self.symbol,
            side=side,
            price=quote.price,
            size=quote.size,
            placed_at_ms=int(time.time() * 1000),
        )
        # Filled-immediately on place: ei jää active:in.
        if result.status == "filled":
            await self._record_status(info, status="filled")
            return None
        await self._record_status(info, status="placed")
        await self._invoke_placed_cb(info)
        return info

    # ----- cancel_all -----

    async def cancel_all(self) -> int:
        """Peruuta kaikki orderit. Hakee API:lta jotta varma tulos.

        Kutsutaan emergencyssä — esim. RiskManagerista kun kill triggeröityy.
        """
        async with self._lock:
            return await self._cancel_all_active()

    async def _cancel_all_active(self) -> int:
        """Internal — pitää _lock olla jo otettu."""
        try:
            n = await self._hl.cancel_all_orders(self.symbol)
        except HLClientError as exc:
            _logger.error("cancel_all_orders API failed: %s", exc)
            n = 0
        # Tyhjennä _active joka tapauksessa — jos joku jäi auki, reconcile
        # tarttuu siihen seuraavalla syklillä.
        cancelled_orders = list(self._active.values())
        self._active.clear()
        for order in cancelled_orders:
            await self._record_status(order, status="cancelled")
            await self._invoke_cancelled_cb(order)
        return max(n, len(cancelled_orders))

    # ----- Fill handling -----

    async def handle_fill(self, fill: FillEntry) -> OrderInfo | None:
        """InventoryManager:lta saatu fill — poista vastaava order active:sta.

        Palauttaa OrderInfo:n jos löytyi (kutsuja voi laskea latenssi:n).
        Logittaa fill latenssin (placement → fill).
        """
        if fill.symbol != self.symbol:
            return None
        async with self._lock:
            info = self._active.pop(fill.oid, None)
        if info is None:
            _logger.debug(
                "fill for unknown oid=%s — likely cancelled-then-filled race",
                fill.oid,
            )
        else:
            latency_ms = max(0, fill.timestamp_ms - info.placed_at_ms)
            _logger.info(
                "fill oid=%s side=%s price=%s size=%s latency_ms=%d",
                fill.oid, fill.side, fill.price, fill.size, latency_ms,
            )
            await self._record_status(info, status="filled")
        await self._invoke_fill_cb(fill, info)
        return info

    # ----- Stale cleanup -----

    async def _cleanup_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._cleanup_interval_s,
                )
                return
            except TimeoutError:
                pass
            try:
                await self.cancel_stale_orders()
            except Exception:
                _logger.exception("stale cleanup loop error")

    async def cancel_stale_orders(self) -> int:
        """Cancel orderit joiden ikä > max_order_age_seconds."""
        now_ms = int(time.time() * 1000)
        threshold_ms = self._max_order_age_seconds * 1000
        async with self._lock:
            stale = [
                o
                for o in self._active.values()
                if (now_ms - o.placed_at_ms) > threshold_ms
            ]
            for order in stale:
                ok, was_not_found = await self._cancel_single(order)
                self._active.pop(order.oid, None)
                _ = ok or was_not_found
        if stale:
            _logger.info("cancelled %d stale orders", len(stale))
        return len(stale)

    # ----- State persistence -----

    async def _record_status(
        self, info: OrderInfo, *, status: OrderStatus
    ) -> None:
        cancel_ts = (
            int(time.time() * 1000)
            if status in ("cancelled", "rejected")
            else None
        )
        record = OrderRecord(
            id=str(info.oid),
            client_order_id=info.cloid or None,
            timestamp_ms=info.placed_at_ms,
            symbol=info.symbol,
            side=info.side,
            price=info.price,
            size=info.size,
            status=status,
            cancel_timestamp_ms=cancel_ts,
        )
        await self._store.record_order(record)

    async def _invoke_placed_cb(self, info: OrderInfo) -> None:
        if self._on_placed is None:
            return
        try:
            result = self._on_placed(info)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            _logger.exception("on_order_placed callback error")

    async def _invoke_cancelled_cb(self, info: OrderInfo) -> None:
        if self._on_cancelled is None:
            return
        try:
            result = self._on_cancelled(info)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            _logger.exception("on_order_cancelled callback error")

    async def _invoke_fill_cb(
        self, fill: FillEntry, info: OrderInfo | None
    ) -> None:
        if self._on_fill is None:
            return
        latency_ms: float = 0.0
        if info is not None:
            latency_ms = max(0.0, float(fill.timestamp_ms - info.placed_at_ms))
        try:
            result = self._on_fill(fill, info, latency_ms)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            _logger.exception("on_fill callback error")


# Helper: convenience factory for cloid in tests / standalone use
def generate_cloid() -> str:
    """Yleinen cloid-generaattori (testaukseen / ad-hoc-käyttöön).

    Käyttää uuden OrderManagerin counter:ia jokaisella kutsulla — ei säilytä
    tilaa kutsujen välillä. Riittää koska sisältää 64-bittisen randomin.
    """
    ts_ms = int(time.time() * 1000)
    rand = secrets.token_hex(8)
    return f"0x{ts_ms:012x}0000{rand}"
