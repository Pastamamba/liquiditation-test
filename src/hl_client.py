"""Async wrapper Hyperliquid Python SDK:n päälle.

Keskittää API-kutsut, lisää retry-logiikan, token-bucket rate-limittauksen,
Decimal-pohjaisen tick/lot-size-pyöristyksen ja meta-cachen. SDK:n synkroniset
metodit ajetaan `asyncio.to_thread`:llä jotta event-loop ei blokkaannu.

Virhepolitiikka:
  - Network (ConnectionError, TimeoutError) → retry 3x backoffilla 0.1s/0.5s/2.0s
  - Rate limit (HTTP 429 / "rate limit"-substring) → retry, kunnioita Retry-After:ia
  - Validation (insufficient, invalid, post only crossed, ...) → raise heti
  - Auth (signature, unauthorized, agent does not exist) → log critical, raise heti
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal
from typing import Any, Literal, Protocol

OrderSide = Literal["bid", "ask"]
OrderStatus = Literal["resting", "filled", "rejected"]

PERP_MAX_DECIMALS = 6
DEFAULT_SIG_FIGS = 5
DEFAULT_RETRY_DELAYS_S: tuple[float, ...] = (0.1, 0.5, 2.0)

_logger = logging.getLogger(__name__)


# ---------- Exceptions ----------


class HLClientError(Exception):
    """Base error for HLClient operations."""


class HLNetworkError(HLClientError):
    """Network-tason virhe (connection refused, timeout)."""


class HLRateLimitError(HLClientError):
    """HTTP 429 / rate-limit ylitetty."""

    def __init__(self, msg: str, retry_after_s: float | None = None) -> None:
        super().__init__(msg)
        self.retry_after_s = retry_after_s


class HLAuthError(HLClientError):
    """Signature- tai authentikaatio-virhe. Kriittinen — botti pitäisi sammuttaa."""


class HLOrderRejectedError(HLClientError):
    """Order rejectattu (insufficient balance, invalid order, post-only crossed, ...)."""


# ---------- Result types ----------


@dataclass(frozen=True)
class OrderResult:
    status: OrderStatus
    oid: int | None
    cloid: str | None
    avg_price: Decimal | None
    filled_size: Decimal | None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status in ("resting", "filled")


@dataclass(frozen=True)
class Position:
    symbol: str
    size: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class OpenOrder:
    oid: int
    cloid: str | None
    symbol: str
    side: OrderSide
    price: Decimal
    size: Decimal
    timestamp_ms: int


@dataclass(frozen=True)
class FillEntry:
    timestamp_ms: int
    symbol: str
    side: OrderSide
    price: Decimal
    size: Decimal
    fee: Decimal
    oid: int
    is_maker: bool


@dataclass(frozen=True)
class AssetMeta:
    symbol: str
    asset_id: int
    sz_decimals: int
    max_leverage: int
    is_delisted: bool


# ---------- Rounding helpers ----------


def round_price(
    price: Decimal,
    sz_decimals: int,
    *,
    side: OrderSide | None = None,
    max_decimals: int = PERP_MAX_DECIMALS,
    sig_figs: int = DEFAULT_SIG_FIGS,
) -> Decimal:
    """Pyöristä hinta Hyperliquid-perp-sääntöjen mukaan.

    Säännöt:
      - Integer-hinnat aina sallittuja (esim. 1234)
      - Muuten enintään `sig_figs` merkitsevää JA enintään
        `max_decimals - sz_decimals` desimaalia.

    Pyöristyssuunta:
      - side="bid": alaspäin (bid pysyy maker-puolella)
      - side="ask": ylöspäin
      - None: half-even (yleinen pyöristys)
    """
    if price <= 0:
        raise ValueError(f"price must be positive: {price}")

    abs_price = abs(price)
    # Integer-hinta on aina valid → palauta sellaisenaan.
    if price == price.to_integral_value():
        return price.quantize(Decimal("1"))

    # `Decimal.adjusted()` antaa eksponentin merkitsevimmälle numerolle:
    #   Decimal("1850.55").adjusted() == 3
    #   Decimal("0.001234").adjusted() == -3
    # → desimaalien max sig.fig-säännöllä = sig_figs - 1 - adjusted.
    sig_fig_dp = max(0, sig_figs - 1 - abs_price.adjusted())
    max_dp = max(0, max_decimals - sz_decimals)
    dp = min(sig_fig_dp, max_dp)
    quant = Decimal("1") if dp == 0 else Decimal(10) ** -dp

    rounding = (
        ROUND_DOWN if side == "bid"
        else ROUND_UP if side == "ask"
        else ROUND_HALF_EVEN
    )
    return price.quantize(quant, rounding=rounding)


def round_size(size: Decimal, sz_decimals: int) -> Decimal:
    """Pyöristä koko alaspäin szDecimals-tarkkuuteen.

    Aina down (kohti nollaa) jotta orderi ei vahingossa ylitä haluttua kokoa.
    """
    if size < 0:
        raise ValueError(f"size must be non-negative: {size}")
    quant = Decimal("1") if sz_decimals == 0 else Decimal(10) ** -sz_decimals
    return size.quantize(quant, rounding=ROUND_DOWN)


# ---------- Token bucket ----------


class TokenBucket:
    """Yksinkertainen async token bucket — throttle requestit ennen API-kutsua."""

    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        self.rate = rate_per_second
        self.capacity = capacity if capacity is not None else max(rate_per_second, 1.0)
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait_s = deficit / self.rate
            await asyncio.sleep(wait_s)


# ---------- Error categorization ----------

_NETWORK_EXC_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
)

_VALIDATION_PATTERNS: tuple[str, ...] = (
    "insufficient",
    "invalid",
    "post only",
    "would have crossed",
    "tick size",
    "lot size",
    "reduce only",
    "min order",
    "must be divisible",
    "rejected",
    "order not found",
)

_AUTH_PATTERNS: tuple[str, ...] = (
    "unauthorized",
    "forbidden",
    "signature",
    "agent does not exist",
    "must approve",
    "access denied",
)

_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "429",
    "rate limit",
    "too many requests",
    "throttle",
)

_NETWORK_PATTERNS: tuple[str, ...] = (
    "connection",
    "timeout",
    "timed out",
    "network",
    "unreachable",
    "reset by peer",
)

ErrorKind = Literal["network", "rate_limit", "auth", "validation", "unknown"]


def _categorize(exc: BaseException) -> ErrorKind:
    if isinstance(exc, _NETWORK_EXC_TYPES):
        return "network"
    msg = str(exc).lower()
    if any(p in msg for p in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    if any(p in msg for p in _NETWORK_PATTERNS):
        return "network"
    if any(p in msg for p in _AUTH_PATTERNS):
        return "auth"
    if any(p in msg for p in _VALIDATION_PATTERNS):
        return "validation"
    return "unknown"


_RETRY_AFTER_RE = re.compile(r"retry[- ]?after[:\s]+(\d+(?:\.\d+)?)", re.IGNORECASE)


def _extract_retry_after(exc: BaseException) -> float | None:
    m = _RETRY_AFTER_RE.search(str(exc))
    return float(m.group(1)) if m else None


# ---------- SDK protocols (testin mockattavuutta varten) ----------


class _InfoProto(Protocol):
    def meta(self) -> Any: ...
    def open_orders(self, address: str) -> Any: ...
    def user_state(self, address: str) -> Any: ...
    def user_fills_by_time(self, address: str, start_time: int) -> Any: ...
    def meta_and_asset_ctxs(self) -> Any: ...


class _ExchangeProto(Protocol):
    def order(
        self,
        name: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: Any,
        reduce_only: bool = False,
        cloid: Any = None,
    ) -> Any: ...

    def cancel(self, name: str, oid: int) -> Any: ...

    def bulk_cancel(self, cancel_requests: list[Any]) -> Any: ...

    def modify_order(
        self,
        oid: int,
        name: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: Any,
        reduce_only: bool = False,
        cloid: Any = None,
    ) -> Any: ...


def _make_cloid(s: str | None) -> Any:
    """Wrap client_order_id Cloid-objektiksi jos SDK on saatavilla, muutoin string."""
    if s is None:
        return None
    try:
        from hyperliquid.utils.signing import Cloid
    except ImportError:
        return s
    return Cloid.from_str(s)


# ---------- HLClient ----------


class HLClient:
    """Async-wrapper Hyperliquid SDK:n päälle.

    Args:
        config: HyperliquidConfig (network + master/api_wallet_address).
        private_key: agent-walletin privaattiavain (0x-prefixed hex).
        requests_per_second: token-bucket rate (default 10/s, well below per-IP cap).
        meta_cache_ttl_seconds: kuinka usein refreshaa meta-cache.
        retry_delays_s: retry-backoff (network + rate_limit virheille).
        info, exchange: optional injection (testaus).
    """

    def __init__(
        self,
        config: Any,  # HyperliquidConfig — avoid circular import
        private_key: str,
        *,
        requests_per_second: float = 10.0,
        meta_cache_ttl_seconds: float = 300.0,
        retry_delays_s: tuple[float, ...] = DEFAULT_RETRY_DELAYS_S,
        info: _InfoProto | None = None,
        exchange: _ExchangeProto | None = None,
    ) -> None:
        if (info is None or exchange is None) and not private_key:
            raise HLAuthError("private_key is required when info/exchange not injected")
        self._config = config
        self._info: _InfoProto
        self._exchange: _ExchangeProto
        if info is not None and exchange is not None:
            self._info = info
            self._exchange = exchange
        else:
            self._info, self._exchange = self._build_sdk(config, private_key)
        self._bucket = TokenBucket(requests_per_second)
        self._meta_ttl = meta_cache_ttl_seconds
        self._meta_cache: dict[str, AssetMeta] = {}
        self._meta_fetched_at: float = 0.0
        self._meta_lock = asyncio.Lock()
        self._retry_delays_s = retry_delays_s

    @staticmethod
    def _build_sdk(
        config: Any, private_key: str
    ) -> tuple[_InfoProto, _ExchangeProto]:
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        base_url = (
            constants.MAINNET_API_URL
            if config.network == "mainnet"
            else constants.TESTNET_API_URL
        )
        account = Account.from_key(private_key)
        info = Info(base_url=base_url, skip_ws=True)
        exchange = Exchange(
            account,
            base_url=base_url,
            account_address=config.api_wallet_address or None,
        )
        return info, exchange

    # ----- Meta cache -----

    async def get_asset_meta(self, symbol: str) -> AssetMeta:
        """Hae asset-metadata. Cache 5min, refresh tarvittaessa."""
        async with self._meta_lock:
            now = time.monotonic()
            stale = (now - self._meta_fetched_at) > self._meta_ttl
            if not self._meta_cache or stale:
                meta = await asyncio.to_thread(self._info.meta)
                universe = meta.get("universe", []) if isinstance(meta, dict) else []
                self._meta_cache = {
                    a["name"]: AssetMeta(
                        symbol=a["name"],
                        asset_id=idx,
                        sz_decimals=int(a["szDecimals"]),
                        max_leverage=int(a.get("maxLeverage", 1)),
                        is_delisted=bool(a.get("isDelisted", False)),
                    )
                    for idx, a in enumerate(universe)
                }
                self._meta_fetched_at = now
            if symbol not in self._meta_cache:
                raise HLClientError(f"Unknown symbol {symbol!r}")
            return self._meta_cache[symbol]

    # ----- Retry / token-bucket core -----

    async def _call_with_retry(
        self,
        fn: Callable[[], Any],
        *,
        operation: str,
        max_retries: int = 3,
    ) -> Any:
        last_exc: BaseException | None = None
        for attempt in range(max_retries + 1):
            await self._bucket.acquire()
            try:
                _logger.debug("hl call op=%s attempt=%d", operation, attempt)
                return await asyncio.to_thread(fn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                kind = _categorize(exc)
                if kind == "auth":
                    _logger.critical(
                        "HL auth error op=%s err=%s", operation, exc
                    )
                    raise HLAuthError(str(exc)) from exc
                if kind == "validation":
                    _logger.info(
                        "HL validation error op=%s err=%s", operation, exc
                    )
                    raise HLOrderRejectedError(str(exc)) from exc
                if kind in ("network", "rate_limit"):
                    if attempt >= max_retries:
                        if kind == "rate_limit":
                            raise HLRateLimitError(
                                str(exc), _extract_retry_after(exc)
                            ) from exc
                        raise HLNetworkError(str(exc)) from exc
                    base = self._retry_delays_s[
                        min(attempt, len(self._retry_delays_s) - 1)
                    ]
                    delay = base
                    if kind == "rate_limit":
                        ra = _extract_retry_after(exc)
                        if ra is not None:
                            delay = max(delay, ra)
                    _logger.warning(
                        "HL %s, retrying op=%s attempt=%d delay=%.3fs err=%s",
                        kind, operation, attempt + 1, delay, exc,
                    )
                    last_exc = exc
                    await asyncio.sleep(delay)
                    continue
                # unknown — älä retryä
                _logger.error("HL unknown error op=%s err=%s", operation, exc)
                raise HLClientError(str(exc)) from exc
        raise HLClientError(f"{operation} retries exhausted: {last_exc}")

    # ----- Public API -----

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
        *,
        post_only: bool = True,
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> OrderResult:
        meta = await self.get_asset_meta(symbol)
        if meta.is_delisted:
            raise HLClientError(f"{symbol} is delisted")
        rounded_price = round_price(price, meta.sz_decimals, side=side)
        rounded_size = round_size(size, meta.sz_decimals)
        if rounded_size <= 0:
            raise HLOrderRejectedError(f"size rounds to zero: {size}")

        tif = "Alo" if post_only else "Gtc"
        order_type = {"limit": {"tif": tif}}
        is_buy = side == "bid"
        cloid_obj = _make_cloid(client_order_id)

        _logger.debug(
            "place_order: symbol=%s side=%s price=%s size=%s tif=%s cloid=%s",
            symbol, side, rounded_price, rounded_size, tif, client_order_id,
        )
        response = await self._call_with_retry(
            lambda: self._exchange.order(
                name=symbol,
                is_buy=is_buy,
                sz=float(rounded_size),
                limit_px=float(rounded_price),
                order_type=order_type,
                reduce_only=reduce_only,
                cloid=cloid_obj,
            ),
            operation="place_order",
        )
        return _parse_order_response(response, cloid=client_order_id)

    async def cancel_order(self, symbol: str, oid: int) -> bool:
        await self.get_asset_meta(symbol)  # validoi symbol
        _logger.debug("cancel_order: symbol=%s oid=%s", symbol, oid)
        try:
            response = await self._call_with_retry(
                lambda: self._exchange.cancel(symbol, oid),
                operation="cancel_order",
            )
        except HLOrderRejectedError as exc:
            # "order not found" — orderi jo täyttynyt tai cancellattu, ei kriittistä
            _logger.debug("cancel rejected (likely already done): %s", exc)
            return False
        statuses = _statuses_from(response)
        return bool(statuses) and statuses[0] == "success"

    async def cancel_all_orders(self, symbol: str) -> int:
        opens = await self.get_open_orders(symbol)
        if not opens:
            return 0
        cancels = [{"coin": o.symbol, "oid": o.oid} for o in opens]
        _logger.debug(
            "cancel_all_orders: symbol=%s count=%d", symbol, len(cancels)
        )
        response = await self._call_with_retry(
            lambda: self._exchange.bulk_cancel(cancels),
            operation="cancel_all_orders",
        )
        statuses = _statuses_from(response)
        return sum(1 for s in statuses if s == "success")

    async def modify_order(
        self,
        symbol: str,
        oid: int,
        side: OrderSide,
        new_price: Decimal,
        new_size: Decimal,
        *,
        post_only: bool = True,
        reduce_only: bool = False,
    ) -> OrderResult:
        meta = await self.get_asset_meta(symbol)
        rounded_price = round_price(new_price, meta.sz_decimals, side=side)
        rounded_size = round_size(new_size, meta.sz_decimals)
        if rounded_size <= 0:
            raise HLOrderRejectedError(f"size rounds to zero: {new_size}")

        tif = "Alo" if post_only else "Gtc"
        order_type = {"limit": {"tif": tif}}
        is_buy = side == "bid"
        _logger.debug(
            "modify_order: symbol=%s oid=%s side=%s price=%s size=%s tif=%s",
            symbol, oid, side, rounded_price, rounded_size, tif,
        )
        response = await self._call_with_retry(
            lambda: self._exchange.modify_order(
                oid=oid,
                name=symbol,
                is_buy=is_buy,
                sz=float(rounded_size),
                limit_px=float(rounded_price),
                order_type=order_type,
                reduce_only=reduce_only,
            ),
            operation="modify_order",
        )
        return _parse_order_response(response, cloid=None)

    async def get_open_orders(self, symbol: str | None = None) -> list[OpenOrder]:
        addr = self._config.api_wallet_address
        raw = await self._call_with_retry(
            lambda: self._info.open_orders(addr),
            operation="get_open_orders",
        )
        out: list[OpenOrder] = []
        for o in raw or []:
            sym = o.get("coin")
            if symbol is not None and sym != symbol:
                continue
            side: OrderSide = "bid" if o.get("side", "B") == "B" else "ask"
            out.append(
                OpenOrder(
                    oid=int(o["oid"]),
                    cloid=o.get("cloid"),
                    symbol=sym,
                    side=side,
                    price=Decimal(str(o["limitPx"])),
                    size=Decimal(str(o["sz"])),
                    timestamp_ms=int(o.get("timestamp", 0)),
                )
            )
        return out

    async def get_position(self, symbol: str) -> Position:
        addr = self._config.api_wallet_address
        state = await self._call_with_retry(
            lambda: self._info.user_state(addr),
            operation="get_position",
        )
        for entry in (state or {}).get("assetPositions") or []:
            pos = entry.get("position", {})
            if pos.get("coin") != symbol:
                continue
            szi = Decimal(str(pos.get("szi", "0")))
            entry_px_raw = pos.get("entryPx")
            entry_price = (
                Decimal(str(entry_px_raw))
                if entry_px_raw is not None
                else Decimal("0")
            )
            upnl = Decimal(str(pos.get("unrealizedPnl", "0")))
            return Position(
                symbol=symbol,
                size=szi,
                entry_price=entry_price,
                unrealized_pnl=upnl,
            )
        return Position(
            symbol=symbol,
            size=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
        )

    async def get_user_fills(self, start_time_ms: int) -> list[FillEntry]:
        addr = self._config.api_wallet_address
        raw = await self._call_with_retry(
            lambda: self._info.user_fills_by_time(addr, start_time_ms),
            operation="get_user_fills",
        )
        out: list[FillEntry] = []
        for f in raw or []:
            side: OrderSide = "bid" if f.get("side", "B") == "B" else "ask"
            out.append(
                FillEntry(
                    timestamp_ms=int(f["time"]),
                    symbol=f["coin"],
                    side=side,
                    price=Decimal(str(f["px"])),
                    size=Decimal(str(f["sz"])),
                    fee=Decimal(str(f.get("fee", "0"))),
                    oid=int(f.get("oid", 0)),
                    is_maker=not bool(f.get("crossed", False)),
                )
            )
        return out

    async def get_funding_rate(self, symbol: str) -> Decimal:
        meta = await self.get_asset_meta(symbol)
        ctx = await self._call_with_retry(
            lambda: self._info.meta_and_asset_ctxs(),
            operation="get_funding_rate",
        )
        if not isinstance(ctx, list) or len(ctx) < 2:
            raise HLClientError("unexpected metaAndAssetCtxs response shape")
        asset_ctxs = ctx[1]
        if not isinstance(asset_ctxs, list) or meta.asset_id >= len(asset_ctxs):
            raise HLClientError(f"asset_id {meta.asset_id} out of range")
        funding = asset_ctxs[meta.asset_id].get("funding", "0")
        return Decimal(str(funding))


# ---------- Response parsing ----------


def _statuses_from(resp: Any) -> list[Any]:
    """Pura `response.data.statuses` Hyperliquid /exchange-vastauksesta."""
    if not isinstance(resp, dict):
        return []
    inner = resp.get("response", {})
    if not isinstance(inner, dict):
        return []
    data = inner.get("data", {})
    if not isinstance(data, dict):
        return []
    statuses = data.get("statuses")
    return statuses if isinstance(statuses, list) else []


def _parse_order_response(resp: Any, *, cloid: str | None) -> OrderResult:
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return OrderResult(
            status="rejected",
            oid=None,
            cloid=cloid,
            avg_price=None,
            filled_size=None,
            error=str(resp),
        )
    statuses = _statuses_from(resp)
    if not statuses:
        return OrderResult(
            status="rejected",
            oid=None,
            cloid=cloid,
            avg_price=None,
            filled_size=None,
            error="empty statuses",
        )
    s = statuses[0]
    if not isinstance(s, dict):
        return OrderResult(
            status="rejected",
            oid=None,
            cloid=cloid,
            avg_price=None,
            filled_size=None,
            error=str(s),
        )
    if "resting" in s:
        return OrderResult(
            status="resting",
            oid=int(s["resting"]["oid"]),
            cloid=cloid,
            avg_price=None,
            filled_size=None,
        )
    if "filled" in s:
        f = s["filled"]
        return OrderResult(
            status="filled",
            oid=int(f["oid"]),
            cloid=cloid,
            avg_price=Decimal(str(f["avgPx"])),
            filled_size=Decimal(str(f["totalSz"])),
        )
    if "error" in s:
        return OrderResult(
            status="rejected",
            oid=None,
            cloid=cloid,
            avg_price=None,
            filled_size=None,
            error=str(s["error"]),
        )
    return OrderResult(
        status="rejected",
        oid=None,
        cloid=cloid,
        avg_price=None,
        filled_size=None,
        error=f"unknown status: {s}",
    )
