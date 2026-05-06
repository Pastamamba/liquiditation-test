"""Inventory manager — pitää kirjaa nykyisestä positiosta, lasketaan FIFO-PnL.

Subscribe `userFills` WebSocketin yli. Atominen position-päivitys per fill
(`asyncio.Lock`), reconcile-loop API:a vastaan 30 s välein joka kuro umpeen
mahdollisesti missatut WS-viestit.

Idempotenssi: jokainen fill identifioidaan `tid`-kentällä; uudelleenlähetetyt
viestit (esim. reconnect-snapshot) ohitetaan.

Realized PnL lasketaan FIFO-matchingilla: kun position on long ja saadaan
sell-fill, vanhin long-lot suljetaan ensin. PnL session-pohjainen — botin
restart resetoi laskurin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

import websockets

from src.hl_client import FillEntry, HLClient
from src.state import FillRecord, OrderSide, StateStore

WS_URL_MAINNET = "wss://api.hyperliquid.xyz/ws"
WS_URL_TESTNET = "wss://api.hyperliquid-testnet.xyz/ws"

DEFAULT_RECONCILE_INTERVAL_S = 30.0
DEFAULT_RECONCILE_TOLERANCE = Decimal("1e-9")
DEFAULT_PING_INTERVAL_S = 15.0
DEFAULT_SILENCE_TIMEOUT_S = 30.0  # userFills-kanava on hiljaisempi kuin l2Book
DEFAULT_INITIAL_BACKOFF_S = 1.0
DEFAULT_MAX_BACKOFF_S = 60.0

_logger = logging.getLogger(__name__)

LotSide = Literal["long", "short"]


@dataclass
class _Lot:
    """Avoin position-osa. Kaikki deque:ssa olevat lot:it ovat samaa side:a."""

    side: LotSide
    price: Decimal
    size: Decimal  # aina positiivinen


@dataclass(frozen=True)
class InventorySnapshot:
    """Snapshot inventoryn tilasta callback-payloadia varten."""

    symbol: str
    position: Decimal  # signed: + long, - short
    avg_entry_price: Decimal  # 0 jos flat
    realized_pnl: Decimal
    total_fees: Decimal
    timestamp_ms: int = field(default=0)


# ---------- Parsers ----------


def parse_user_fill(d: Any, *, symbol_filter: str | None = None) -> FillEntry | None:
    """Parse single Hyperliquid userFills-channel-fill → FillEntry.

    Palauttaa None jos data ei ole validi tai jos symbol_filter ei matchaa.
    """
    if not isinstance(d, dict):
        return None
    coin = d.get("coin")
    if symbol_filter is not None and coin != symbol_filter:
        return None
    try:
        side: OrderSide = "bid" if d.get("side", "B") == "B" else "ask"
        closed_pnl_raw = d.get("closedPnl")
        return FillEntry(
            timestamp_ms=int(d["time"]),
            symbol=str(coin),
            side=side,
            price=Decimal(str(d["px"])),
            size=Decimal(str(d["sz"])),
            fee=Decimal(str(d.get("fee", "0"))),
            oid=int(d.get("oid", 0)),
            is_maker=not bool(d.get("crossed", False)),
            tid=int(d.get("tid", 0)),
            closed_pnl=(
                Decimal(str(closed_pnl_raw))
                if closed_pnl_raw is not None
                else None
            ),
        )
    except (KeyError, ValueError):
        return None


# ---------- InventoryManager ----------

ConnectFactory = Callable[[str], Awaitable[Any]]
_ChangeCallback = Callable[["InventorySnapshot"], Awaitable[None] | None]
_RawFillCallback = Callable[[FillEntry], Awaitable[None] | None]


class InventoryManager:
    """Position-kirjanpito userFills-WS:n ja API-reconcile:n yhdistelmänä."""

    def __init__(
        self,
        symbol: str,
        max_position: Decimal,
        hl_client: HLClient,
        state_store: StateStore,
        *,
        user_address: str | None = None,
        network: Literal["testnet", "mainnet"] = "testnet",
        on_inventory_change: _ChangeCallback | None = None,
        on_raw_fill: _RawFillCallback | None = None,
        connect_factory: ConnectFactory | None = None,
        reconcile_interval_s: float = DEFAULT_RECONCILE_INTERVAL_S,
        reconcile_tolerance: Decimal = DEFAULT_RECONCILE_TOLERANCE,
        ping_interval_s: float = DEFAULT_PING_INTERVAL_S,
        silence_timeout_s: float = DEFAULT_SILENCE_TIMEOUT_S,
        initial_backoff_s: float = DEFAULT_INITIAL_BACKOFF_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
    ) -> None:
        if max_position <= 0:
            raise ValueError("max_position must be > 0")
        self.symbol = symbol
        self.max_position = max_position
        self._hl = hl_client
        self._store = state_store
        # Master/api-osoite jonka puolesta agent edustaa — userFills-subscription
        # vaatii tämän.
        self._user_address = user_address or hl_client._config.api_wallet_address
        self._network = network
        self._url = (
            WS_URL_MAINNET if network == "mainnet" else WS_URL_TESTNET
        )
        self._on_change = on_inventory_change
        self._on_raw_fill = on_raw_fill
        self._connect_factory = connect_factory
        self._reconcile_interval = reconcile_interval_s
        self._reconcile_tol = reconcile_tolerance
        self._ping_interval = ping_interval_s
        self._silence_timeout = silence_timeout_s
        self._initial_backoff = initial_backoff_s
        self._max_backoff = max_backoff_s

        # State (kaikki manipulointi _lock-suojattua)
        self._lock = asyncio.Lock()
        self._lots: deque[_Lot] = deque()
        self._realized_pnl = Decimal("0")
        self._total_fees = Decimal("0")
        self._seen_tids: set[int] = set()
        # Cached scalar properties — päivitetään kun lots muuttuu.
        self._cached_position = Decimal("0")
        self._cached_avg_entry = Decimal("0")

        # Lifecycle
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._ws_task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._snapshot_received = False

    # ----- Public properties -----

    @property
    def current_position(self) -> Decimal:
        return self._cached_position

    @property
    def avg_entry_price(self) -> Decimal:
        return self._cached_avg_entry

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized_pnl

    @property
    def total_fees(self) -> Decimal:
        return self._total_fees

    @property
    def is_long(self) -> bool:
        return self._cached_position > 0

    @property
    def is_short(self) -> bool:
        return self._cached_position < 0

    @property
    def is_flat(self) -> bool:
        return self._cached_position == 0

    @property
    def inventory_skew(self) -> Decimal:
        """Position normalisoituna max_position:iin, clamp [-1, 1]."""
        if self.max_position == 0:
            return Decimal("0")
        skew = self._cached_position / self.max_position
        if skew > 1:
            return Decimal("1")
        if skew < -1:
            return Decimal("-1")
        return skew

    def position_value_usd(self, mark_price: Decimal | None = None) -> Decimal:
        """Position |arvo| USD:nä mark_price:lla (tai avg_entry:llä jos None)."""
        px = mark_price if mark_price is not None else self._cached_avg_entry
        return abs(self._cached_position) * px

    def can_quote_bid(self) -> bool:
        """False jos olemme jo max long → emme saa lisätä longia."""
        return self._cached_position < self.max_position

    def can_quote_ask(self) -> bool:
        """False jos olemme jo max short → emme saa lisätä shortia."""
        return self._cached_position > -self.max_position

    def get_skew_adjustment_factor(self) -> Decimal:
        """Alias QuoteEnginelle — palauttaa inventory_skew."""
        return self.inventory_skew

    def snapshot(self, *, timestamp_ms: int = 0) -> InventorySnapshot:
        return InventorySnapshot(
            symbol=self.symbol,
            position=self._cached_position,
            avg_entry_price=self._cached_avg_entry,
            realized_pnl=self._realized_pnl,
            total_fees=self._total_fees,
            timestamp_ms=timestamp_ms or int(time.time() * 1000),
        )

    # ----- Fill application (FIFO PnL) -----

    async def apply_fill(self, fill: FillEntry) -> bool:
        """Käsittele fill: dedup tid:llä, päivitä lots/PnL atomically, persist.

        Palauttaa True jos uusi fill, False jos jo nähty (idempotent).
        """
        async with self._lock:
            if fill.tid != 0 and fill.tid in self._seen_tids:
                return False
            if fill.tid != 0:
                self._seen_tids.add(fill.tid)
            self._update_lots_and_pnl(fill)
            self._refresh_cached_position()
            await self._record_fill(fill)
        await self._invoke_raw_fill_callback(fill)
        await self._invoke_change_callback()
        return True

    def _update_lots_and_pnl(self, fill: FillEntry) -> None:
        """FIFO-matching: avaa uusi lot tai sulje vanhin vastainen lot."""
        # bid = ostaa = long-suunta; ask = myy = short-suunta
        fill_lot_side: LotSide = "long" if fill.side == "bid" else "short"
        remaining = fill.size

        while remaining > 0:
            if not self._lots or self._lots[0].side == fill_lot_side:
                # Sama suunta tai tyhjä → uusi lot
                self._lots.append(
                    _Lot(side=fill_lot_side, price=fill.price, size=remaining)
                )
                remaining = Decimal("0")
                break
            # Vastainen suunta → sulje vanhin lot
            lot = self._lots[0]
            matched = min(lot.size, remaining)
            if lot.side == "long":
                # Long suljetaan myymällä: P&L = (sell - buy) * size
                self._realized_pnl += (fill.price - lot.price) * matched
            else:
                # Short suljetaan ostamalla: P&L = (orig_sell - buy_now) * size
                self._realized_pnl += (lot.price - fill.price) * matched
            lot.size -= matched
            remaining -= matched
            if lot.size == 0:
                self._lots.popleft()

        self._total_fees += fill.fee

    def _refresh_cached_position(self) -> None:
        """Laske current_position ja avg_entry_price lots:ista."""
        if not self._lots:
            self._cached_position = Decimal("0")
            self._cached_avg_entry = Decimal("0")
            return
        side = self._lots[0].side
        total_size = sum((lot.size for lot in self._lots), Decimal("0"))
        notional = sum(
            (lot.size * lot.price for lot in self._lots), Decimal("0")
        )
        signed = total_size if side == "long" else -total_size
        self._cached_position = signed
        self._cached_avg_entry = (
            notional / total_size if total_size > 0 else Decimal("0")
        )

    async def _record_fill(self, fill: FillEntry) -> None:
        """Tallenna fill state_store:en. Käyttää tid:tä id:nä jos saatavilla."""
        fill_id = (
            str(fill.tid)
            if fill.tid != 0
            else f"{fill.oid}-{fill.timestamp_ms}-{fill.side}-{fill.price}-{fill.size}"
        )
        record = FillRecord(
            id=fill_id,
            timestamp_ms=fill.timestamp_ms,
            symbol=fill.symbol,
            side=fill.side,
            price=fill.price,
            size=fill.size,
            fee=fill.fee,
            order_id=str(fill.oid),
            is_maker=fill.is_maker,
        )
        await self._store.record_fill(record)

    async def _invoke_change_callback(self) -> None:
        if self._on_change is None:
            return
        snap = self.snapshot()
        try:
            result = self._on_change(snap)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            _logger.exception("on_inventory_change callback error")

    async def _invoke_raw_fill_callback(self, fill: FillEntry) -> None:
        if self._on_raw_fill is None:
            return
        try:
            result = self._on_raw_fill(fill)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            _logger.exception("on_raw_fill callback error")

    # ----- Reconcile -----

    async def reconcile(self) -> bool:
        """Vertaa muistia API:n positioon. Korjaa muisti jos eroaa.

        Palauttaa True jos memory == API (tolerance:n sisällä), False jos
        korjaus tehtiin tai virhe.
        """
        try:
            api_pos = await self._hl.get_position(self.symbol)
        except Exception:
            _logger.exception("reconcile: get_position failed")
            return False

        async with self._lock:
            mem_pos = self._cached_position
            diff = api_pos.size - mem_pos
            if abs(diff) <= self._reconcile_tol:
                return True
            _logger.warning(
                "reconcile MISMATCH symbol=%s memory=%s api=%s diff=%s",
                self.symbol, mem_pos, api_pos.size, diff,
            )
            # Rakenna lots uudelleen yhdellä lot:illa API:n entry_price:lla.
            self._lots.clear()
            if api_pos.size != 0:
                lot_side: LotSide = "long" if api_pos.size > 0 else "short"
                self._lots.append(
                    _Lot(
                        side=lot_side,
                        price=(
                            api_pos.entry_price
                            if api_pos.entry_price > 0
                            else Decimal("0")
                        ),
                        size=abs(api_pos.size),
                    )
                )
            self._refresh_cached_position()
        await self._invoke_change_callback()
        return False

    # ----- Lifecycle -----

    async def start(self) -> None:
        if self._ws_task is not None:
            return
        # Initial reconcile asettaa baseline-positionin API:sta.
        await self.reconcile()
        self._stop_event.clear()
        self._snapshot_received = False
        self._ws_task = asyncio.create_task(
            self._ws_loop(), name="inventory-ws"
        )
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="inventory-reconcile"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        for task_attr in ("_ws_task", "_reconcile_task"):
            task = getattr(self, task_attr, None)
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            setattr(self, task_attr, None)

    @property
    def is_connected(self) -> bool:
        return self._connected_event.is_set()

    # ----- Reconcile loop -----

    async def _reconcile_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._reconcile_interval,
                )
                return  # stop signaled
            except TimeoutError:
                pass
            try:
                await self.reconcile()
            except Exception:
                _logger.exception("reconcile loop iteration failed")

    # ----- WebSocket loop -----

    async def _ws_loop(self) -> None:
        backoff = self._initial_backoff
        while not self._stop_event.is_set():
            ws: Any = None
            try:
                ws = await self._open_ws()
                self._connected_event.set()
                backoff = self._initial_backoff
                _logger.info("inventory WS connected to %s", self._url)
                await self._subscribe(ws)
                await self._run_session(ws)
            except asyncio.CancelledError:
                if ws is not None and hasattr(ws, "close"):
                    with contextlib.suppress(Exception):
                        await ws.close()
                raise
            except Exception as exc:
                _logger.warning("inventory WS session error: %s", exc)
            finally:
                self._connected_event.clear()
                if ws is not None and hasattr(ws, "close"):
                    with contextlib.suppress(Exception):
                        await ws.close()

            if self._stop_event.is_set():
                break

            sleep_s = min(backoff, self._max_backoff)
            _logger.info("inventory WS reconnecting in %.1fs", sleep_s)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=sleep_s
                )
                break
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self._max_backoff)

    async def _open_ws(self) -> Any:
        if self._connect_factory is not None:
            return await self._connect_factory(self._url)
        return await websockets.connect(
            self._url, ping_interval=None, ping_timeout=None, close_timeout=2
        )

    async def _subscribe(self, ws: Any) -> None:
        await ws.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {
                        "type": "userFills",
                        "user": self._user_address,
                    },
                }
            )
        )

    async def _run_session(self, ws: Any) -> None:
        last_msg = time.monotonic()
        ping_task = asyncio.create_task(self._ping_loop(ws), name="inventory-ping")
        try:
            while not self._stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=self._silence_timeout
                    )
                except TimeoutError as exc:
                    elapsed = time.monotonic() - last_msg
                    raise ConnectionError(
                        f"inventory WS silent for {elapsed:.1f}s"
                    ) from exc
                last_msg = time.monotonic()
                await self._handle_message(msg)
        finally:
            ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ping_task

    async def _ping_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                await ws.send(json.dumps({"method": "ping"}))
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            text = (
                raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            )
            msg = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            _logger.debug("inventory WS bad message: %r", raw)
            return
        if not isinstance(msg, dict):
            return
        if msg.get("channel") != "userFills":
            return
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        is_snapshot = bool(data.get("isSnapshot", False))
        fills_raw = data.get("fills")
        if not isinstance(fills_raw, list):
            return

        if is_snapshot and not self._snapshot_received:
            # Ensimmäinen snapshot — merkitään kaikki seen, ei applyä.
            # Position rakennetaan API-reconcile:sta, ei fill-historiasta.
            await self._mark_fills_seen(fills_raw)
            self._snapshot_received = True
            return

        for raw_fill in fills_raw:
            fill = parse_user_fill(raw_fill, symbol_filter=self.symbol)
            if fill is None:
                continue
            await self.apply_fill(fill)

    async def _mark_fills_seen(self, fills_raw: list[Any]) -> None:
        async with self._lock:
            for raw_fill in fills_raw:
                if not isinstance(raw_fill, dict):
                    continue
                tid = raw_fill.get("tid")
                if isinstance(tid, int) and tid != 0:
                    self._seen_tids.add(tid)
