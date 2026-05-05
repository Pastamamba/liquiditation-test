"""Tests for src.quote_engine."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.config import TradingConfig
from src.quote_engine import Quote, QuoteEngine, QuoteSet


def _cfg(
    *,
    spread_bps: float = 5.0,
    num_levels: int = 5,
    level_spacing_bps: float = 3.0,
    order_size: str = "0.02",
    skew_factor: float = 0.5,
    max_spread_bps: float = 50.0,
) -> TradingConfig:
    return TradingConfig(
        symbol="ETH",
        capital_usdc=Decimal("500"),
        max_position_size=Decimal("0.1"),
        spread_bps=spread_bps,
        num_levels=num_levels,
        level_spacing_bps=level_spacing_bps,
        order_size=Decimal(order_size),
        skew_factor=skew_factor,
        quote_refresh_ms=2000,
        max_order_age_seconds=30,
        max_spread_bps=max_spread_bps,
    )


# ---------- Symmetric / asymmetric quotes ----------


class TestSymmetric:
    def test_zero_skew_zero_vol_symmetric(self) -> None:
        eng = QuoteEngine(_cfg(), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"),
            inventory_skew=0,
            volatility=0,
            can_bid=True,
            can_ask=True,
        )
        assert qs.skew_adjustment_bps == 0.0
        assert qs.effective_spread_bps == 5.0
        assert len(qs.bids) == 5
        assert len(qs.asks) == 5
        # Best bid + best ask symmetric around mid (within tick rounding).
        best_bid = qs.bids[0].price
        best_ask = qs.asks[0].price
        # mid * (1 - 2.5/10000) = 1850 * 0.99975 = 1849.5375 → round_down → 1849.5
        # mid * (1 + 2.5/10000) = 1850 * 1.00025 = 1850.4625 → round_up → 1850.5
        assert best_bid == Decimal("1849.5")
        assert best_ask == Decimal("1850.5")

    def test_zero_skew_no_volatility_input_uses_baseline(self) -> None:
        # volatility=None → vol_multiplier=1.0 (baseline).
        eng = QuoteEngine(_cfg(), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"),
            inventory_skew=0,
            volatility=None,
            can_bid=True,
            can_ask=True,
        )
        assert qs.effective_spread_bps == 5.0


# ---------- Skew shifts both quotes ----------


class TestSkew:
    def test_positive_skew_shifts_both_down(self) -> None:
        # Long position → skew>0 → both quotes shift DOWN (myydään ennemmin).
        eng = QuoteEngine(_cfg(skew_factor=0.5), sz_decimals=4)
        baseline = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        skewed = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=Decimal("0.5"), volatility=0,
            can_bid=True, can_ask=True,
        )
        assert skewed.bids[0].price < baseline.bids[0].price
        assert skewed.asks[0].price < baseline.asks[0].price
        assert skewed.skew_adjustment_bps > 0

    def test_negative_skew_shifts_both_up(self) -> None:
        eng = QuoteEngine(_cfg(skew_factor=0.5), sz_decimals=4)
        baseline = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        skewed = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=Decimal("-0.5"), volatility=0,
            can_bid=True, can_ask=True,
        )
        assert skewed.bids[0].price > baseline.bids[0].price
        assert skewed.asks[0].price > baseline.asks[0].price
        assert skewed.skew_adjustment_bps < 0

    def test_skew_adjustment_calculation(self) -> None:
        # skew=0.5, factor=0.5, spread=5 → skew_adj = 0.5*0.5*5 = 1.25 bps
        eng = QuoteEngine(_cfg(skew_factor=0.5, spread_bps=5.0), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=Decimal("0.5"), volatility=0,
            can_bid=True, can_ask=True,
        )
        assert qs.skew_adjustment_bps == pytest.approx(1.25)

    def test_max_long_skew_aggressive_ask(self) -> None:
        # skew=1.0, factor=1.0, spread=5
        # → skew_adj = 5; bid_off = -2.5-5 = -7.5; ask_off = 2.5-5 = -2.5
        # Bid 7.5 bps below mid, ask 2.5 bps below mid (aggressive sell).
        eng = QuoteEngine(_cfg(skew_factor=1.0, spread_bps=5.0), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=Decimal("1"), volatility=0,
            can_bid=True, can_ask=True,
        )
        # ask should be BELOW mid (aggressive)
        assert qs.asks[0].price < Decimal("1850")
        # bid should be further below mid than ask
        assert qs.bids[0].price < qs.asks[0].price

    def test_max_short_skew_aggressive_bid(self) -> None:
        eng = QuoteEngine(_cfg(skew_factor=1.0, spread_bps=5.0), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=Decimal("-1"), volatility=0,
            can_bid=True, can_ask=True,
        )
        # bid above mid (aggressive buy)
        assert qs.bids[0].price > Decimal("1850")
        # ask above bid
        assert qs.asks[0].price > qs.bids[0].price


# ---------- Volatility ----------


class TestVolatility:
    def test_high_vol_widens_spread(self) -> None:
        eng = QuoteEngine(_cfg(spread_bps=5.0), sz_decimals=4)
        calm = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0.5,
            can_bid=True, can_ask=True,
        )
        wild = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=3.0,
            can_bid=True, can_ask=True,
        )
        assert wild.effective_spread_bps > calm.effective_spread_bps
        # Quoten ulkoreuna kauempana mid:stä volatiilissa.
        wild_outer = wild.asks[0].price - wild.bids[0].price
        calm_outer = calm.asks[0].price - calm.bids[0].price
        assert wild_outer > calm_outer

    def test_vol_multiplier_clamped_at_max(self) -> None:
        eng = QuoteEngine(_cfg(spread_bps=5.0, max_spread_bps=100), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=10.0,  # 10x baseline
            can_bid=True, can_ask=True,
        )
        # multiplier max=5 → effective = 5 * 5 = 25
        assert qs.effective_spread_bps == 25.0

    def test_vol_multiplier_clamped_at_min(self) -> None:
        eng = QuoteEngine(_cfg(spread_bps=5.0), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0.1,
            can_bid=True, can_ask=True,
        )
        # ratio < 1 → clamp to 1 → effective = 5
        assert qs.effective_spread_bps == 5.0

    def test_excessive_vol_suppresses_quotes(self) -> None:
        # spread=5, max=10 → effective>10 with vol_mult=3 (=15) → suppress
        eng = QuoteEngine(
            _cfg(spread_bps=5.0, max_spread_bps=10.0), sz_decimals=4
        )
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=3.0,
            can_bid=True, can_ask=True,
        )
        assert qs.is_empty
        assert qs.suppressed_reason is not None
        assert "max_spread" in qs.suppressed_reason


# ---------- Levels ----------


class TestLevels:
    def test_num_levels_respected(self) -> None:
        eng = QuoteEngine(_cfg(num_levels=3), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        assert len(qs.bids) == 3
        assert len(qs.asks) == 3

    def test_levels_progressively_further_from_mid(self) -> None:
        eng = QuoteEngine(_cfg(num_levels=5, level_spacing_bps=3.0), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        # bids decreasing
        for i in range(len(qs.bids) - 1):
            assert qs.bids[i].price >= qs.bids[i + 1].price
        # asks increasing
        for i in range(len(qs.asks) - 1):
            assert qs.asks[i].price <= qs.asks[i + 1].price


# ---------- Rounding ----------


class TestRounding:
    def test_eth_prices_rounded_to_1_decimal(self) -> None:
        # ETH sz_decimals=4 → max_dp=2, sig_fig at 1850.x → 1 decimal
        eng = QuoteEngine(_cfg(), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850.55"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        for q in (*qs.bids, *qs.asks):
            # All prices should be rounded to 1 decimal place
            assert q.price == q.price.quantize(Decimal("0.1"))

    def test_size_rounded_down_to_sz_decimals(self) -> None:
        eng = QuoteEngine(_cfg(order_size="0.02345"), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        # 0.02345 → 0.0234 (round down to 4 dp)
        for q in (*qs.bids, *qs.asks):
            assert q.size == Decimal("0.0234")

    def test_btc_sz_decimals(self) -> None:
        # BTC sz_decimals=5 → max_dp=1, sig_fig limit applies
        eng = QuoteEngine(_cfg(spread_bps=2.0, order_size="0.001"), sz_decimals=5)
        qs = eng.compute_quotes(
            mid=Decimal("60000"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        # max_dp = 6-5 = 1, sig_fig_dp = 5-1-4 = 0 → integer prices
        for q in (*qs.bids, *qs.asks):
            assert q.price == q.price.to_integral_value()


# ---------- can_bid / can_ask ----------


class TestCanBidCanAsk:
    def test_can_bid_false_returns_empty_bids(self) -> None:
        eng = QuoteEngine(_cfg(), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=Decimal("1"), volatility=0,
            can_bid=False, can_ask=True,
        )
        assert qs.bids == ()
        assert len(qs.asks) > 0

    def test_can_ask_false_returns_empty_asks(self) -> None:
        eng = QuoteEngine(_cfg(), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=Decimal("-1"), volatility=0,
            can_bid=True, can_ask=False,
        )
        assert len(qs.bids) > 0
        assert qs.asks == ()

    def test_both_false_returns_empty_set(self) -> None:
        eng = QuoteEngine(_cfg(), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0,
            can_bid=False, can_ask=False,
        )
        assert qs.is_empty


# ---------- Edge cases ----------


class TestEdgeCases:
    def test_invalid_mid_returns_empty(self) -> None:
        eng = QuoteEngine(_cfg(), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("0"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        assert qs.is_empty
        assert qs.suppressed_reason is not None

    def test_negative_mid_returns_empty(self) -> None:
        eng = QuoteEngine(_cfg(), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("-1"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        assert qs.is_empty

    def test_size_rounds_to_zero_skipped(self) -> None:
        # order_size 0.00001 with sz_decimals=4 → rounds to 0
        eng = QuoteEngine(_cfg(order_size="0.00001"), sz_decimals=4)
        qs = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        assert qs.is_empty

    def test_invalid_num_levels_raises(self) -> None:
        # Pydantic catches num_levels=0 already, but if passed through:
        with pytest.raises(ValueError):
            QuoteEngine(_cfg(num_levels=1).model_copy(update={"num_levels": 0}))

    def test_invalid_vol_target_raises(self) -> None:
        with pytest.raises(ValueError):
            QuoteEngine(_cfg(), vol_target=0)

    def test_quoteset_dataclass_is_quote(self) -> None:
        # Quote is a dataclass with price + size
        q = Quote(price=Decimal("1850"), size=Decimal("0.02"))
        assert q.price == Decimal("1850")
        assert q.size == Decimal("0.02")

    def test_update_sz_decimals(self) -> None:
        eng = QuoteEngine(_cfg(order_size="0.02345"), sz_decimals=4)
        qs1 = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        # sz_decimals=4 → size 0.02345 → 0.0234
        assert qs1.bids[0].size == Decimal("0.0234")
        eng.update_sz_decimals(3)
        qs2 = eng.compute_quotes(
            mid=Decimal("1850"), inventory_skew=0, volatility=0,
            can_bid=True, can_ask=True,
        )
        # sz_decimals=3 → size 0.02345 → 0.023
        assert qs2.bids[0].size == Decimal("0.023")


# ---------- Integration / sanity ----------


def test_typical_eth_quote() -> None:
    """Sanity: tyypilliset ETH-arvot tuottaa järkevän quote-paketin."""
    eng = QuoteEngine(_cfg(), sz_decimals=4)
    qs = eng.compute_quotes(
        mid=Decimal("1850"), inventory_skew=Decimal("0.2"), volatility=0.8,
        can_bid=True, can_ask=True,
    )
    assert isinstance(qs, QuoteSet)
    assert len(qs.bids) == 5
    assert len(qs.asks) == 5
    # All prices positive
    for q in (*qs.bids, *qs.asks):
        assert q.price > 0
        assert q.size > 0
    # Best bid < mid < best ask
    assert qs.bids[0].price < Decimal("1850")
    assert qs.asks[0].price < Decimal("1860")  # not too far
