"""Quote engine — laskee target bid/ask -kerrokset mid-pricesta.

Algoritmi (Decimal-arithmetiikka kaikessa rahaa-koskevassa):

    base_spread_bps  = config.spread_bps
    vol_multiplier   = clamp(current_vol / vol_target, 1.0, 5.0)
    effective_spread = base_spread_bps * vol_multiplier
    skew_adj_bps     = inventory_skew * config.skew_factor * effective_spread

    bid_offset_bps   = -effective_spread/2 - skew_adj_bps
    ask_offset_bps   = +effective_spread/2 - skew_adj_bps

    bid_base = mid * (1 + bid_offset_bps/10000)
    ask_base = mid * (1 + ask_offset_bps/10000)

    for level in 0..num_levels-1:
        bid_price = bid_base * (1 - level * level_spacing_bps/10000)
        ask_price = ask_base * (1 + level * level_spacing_bps/10000)

Skew-logiikka: positiivinen skew (long) → skew_adj > 0 → MOLEMPIA siirretään
alas → suurempi todennäköisyys myydä, pienempi ostaa → palauttaa nollaan.

Pyöristys:
    - bid → ROUND_DOWN tickiin (säilyy maker-puolella)
    - ask → ROUND_UP tickiin
    - size → ROUND_DOWN sz_decimals-tarkkuuteen

Bypass-säännöt:
    - can_bid=False → tyhjä bids-lista (esim. position max long)
    - can_ask=False → tyhjä asks-lista
    - effective_spread > config.max_spread_bps → kaikki tyhjä (markkina liian
      volatiili, älä quotaa)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.hl_client import round_price, round_size

_logger = logging.getLogger(__name__)

DEFAULT_VOL_TARGET = 1.0  # baseline annualized vol (1.0 = 100%)
DEFAULT_VOL_MULTIPLIER_MIN = Decimal("1")
DEFAULT_VOL_MULTIPLIER_MAX = Decimal("5")

_TEN_K = Decimal("10000")
_HALF = Decimal("0.5")


@dataclass(frozen=True)
class Quote:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class QuoteSet:
    bids: tuple[Quote, ...]
    asks: tuple[Quote, ...]
    mid: Decimal
    effective_spread_bps: float
    skew_adjustment_bps: float
    suppressed_reason: str | None = field(default=None)

    @property
    def is_empty(self) -> bool:
        return not self.bids and not self.asks


class QuoteEngine:
    """Laskee uudet target-quotet TradingConfigista + market state:sta."""

    def __init__(
        self,
        config: Any,  # TradingConfig — avoid circular import
        *,
        sz_decimals: int = 4,
        vol_target: float = DEFAULT_VOL_TARGET,
    ) -> None:
        if config.num_levels <= 0:
            raise ValueError("num_levels must be > 0")
        if vol_target <= 0:
            raise ValueError("vol_target must be > 0")
        self._cfg = config
        self._sz_decimals = sz_decimals
        self._vol_target = Decimal(str(vol_target))

    def update_sz_decimals(self, sz_decimals: int) -> None:
        """Päivitä asset-meta jos se muuttuu (esim. meta-cache refreshattu)."""
        self._sz_decimals = sz_decimals

    def compute_quotes(
        self,
        *,
        mid: Decimal,
        inventory_skew: Decimal | float,
        volatility: float | None,
        can_bid: bool,
        can_ask: bool,
    ) -> QuoteSet:
        """Laske tavoite-quotet annetulle mid:lle ja markkinatilalle.

        Args:
            mid: nykyinen mid-price.
            inventory_skew: position / max_position, alueella [-1, 1].
            volatility: annualisoitu realized vol (0..) tai None jos ei tietoa.
            can_bid: jos False, palautetaan tyhjä bids-lista.
            can_ask: jos False, palautetaan tyhjä asks-lista.
        """
        if mid is None or mid <= 0:
            return self._empty(mid or Decimal("0"), 0.0, 0.0, "invalid mid")

        spread_bps = Decimal(str(self._cfg.spread_bps))
        skew_factor = Decimal(str(self._cfg.skew_factor))
        skew_dec = (
            inventory_skew if isinstance(inventory_skew, Decimal)
            else Decimal(str(inventory_skew))
        )

        # Vol multiplier: clamp(current/target, 1, 5).
        vol_mult = self._compute_vol_multiplier(volatility)
        effective_spread = spread_bps * vol_mult

        # Bypass: liian volatile markkina → ei quoteja kummallekaan puolelle.
        max_spread = Decimal(str(self._cfg.max_spread_bps))
        if effective_spread > max_spread:
            return self._empty(
                mid,
                float(effective_spread),
                0.0,
                f"effective_spread {effective_spread} bps > max_spread {max_spread}",
            )

        skew_adj = skew_dec * skew_factor * effective_spread
        bid_offset_bps = -(effective_spread * _HALF) - skew_adj
        ask_offset_bps = (effective_spread * _HALF) - skew_adj

        bid_base = mid * (Decimal("1") + bid_offset_bps / _TEN_K)
        ask_base = mid * (Decimal("1") + ask_offset_bps / _TEN_K)

        order_size_dec = (
            self._cfg.order_size
            if isinstance(self._cfg.order_size, Decimal)
            else Decimal(str(self._cfg.order_size))
        )
        size_rounded = round_size(order_size_dec, self._sz_decimals)

        level_spacing = Decimal(str(self._cfg.level_spacing_bps))
        bids: list[Quote] = []
        asks: list[Quote] = []

        for level in range(int(self._cfg.num_levels)):
            level_offset = Decimal(level) * level_spacing / _TEN_K

            if can_bid and size_rounded > 0:
                bid_raw = bid_base * (Decimal("1") - level_offset)
                if bid_raw > 0:
                    bid_px = round_price(
                        bid_raw, self._sz_decimals, side="bid"
                    )
                    if bid_px > 0:
                        bids.append(Quote(price=bid_px, size=size_rounded))

            if can_ask and size_rounded > 0:
                ask_raw = ask_base * (Decimal("1") + level_offset)
                if ask_raw > 0:
                    ask_px = round_price(
                        ask_raw, self._sz_decimals, side="ask"
                    )
                    if ask_px > 0:
                        asks.append(Quote(price=ask_px, size=size_rounded))

        return QuoteSet(
            bids=tuple(bids),
            asks=tuple(asks),
            mid=mid,
            effective_spread_bps=float(effective_spread),
            skew_adjustment_bps=float(skew_adj),
            suppressed_reason=None,
        )

    def _compute_vol_multiplier(self, volatility: float | None) -> Decimal:
        if volatility is None or volatility <= 0:
            return DEFAULT_VOL_MULTIPLIER_MIN
        ratio = Decimal(str(volatility)) / self._vol_target
        if ratio < DEFAULT_VOL_MULTIPLIER_MIN:
            return DEFAULT_VOL_MULTIPLIER_MIN
        if ratio > DEFAULT_VOL_MULTIPLIER_MAX:
            return DEFAULT_VOL_MULTIPLIER_MAX
        return ratio

    def _empty(
        self,
        mid: Decimal,
        effective_spread_bps: float,
        skew_adj_bps: float,
        reason: str | None,
    ) -> QuoteSet:
        return QuoteSet(
            bids=(),
            asks=(),
            mid=mid,
            effective_spread_bps=effective_spread_bps,
            skew_adjustment_bps=skew_adj_bps,
            suppressed_reason=reason,
        )
