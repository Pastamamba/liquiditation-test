"""Tests for src.hl_client."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.config import HyperliquidConfig
from src.hl_client import (
    HLAuthError,
    HLClient,
    HLClientError,
    HLNetworkError,
    HLOrderRejectedError,
    HLRateLimitError,
    OrderResult,
    TokenBucket,
    _categorize,
    _extract_retry_after,
    _make_cloid,
    _parse_order_response,
    round_price,
    round_size,
)

# ---------- round_price ----------


class TestRoundPrice:
    def test_integer_preserved(self) -> None:
        assert round_price(Decimal("1850"), sz_decimals=4) == Decimal("1850")

    def test_eth_simple_5sigfig(self) -> None:
        # ETH: szDecimals=4, max_dp=2, sig_fig_dp=1 → 1850.55 → 1850.5/1850.6
        assert round_price(Decimal("1850.55"), sz_decimals=4, side="bid") == Decimal("1850.5")
        assert round_price(Decimal("1850.55"), sz_decimals=4, side="ask") == Decimal("1850.6")

    def test_eth_already_valid(self) -> None:
        assert round_price(Decimal("1850.5"), sz_decimals=4, side="bid") == Decimal("1850.5")

    def test_btc_5_digit_price(self) -> None:
        # 9999.5 (4 digits left) → sig_fig_dp = 5-1-3 = 1, max_dp = 6-5 = 1, dp=1
        assert round_price(Decimal("9999.55"), sz_decimals=5, side="bid") == Decimal("9999.5")

    def test_btc_5_sigfig_at_10k(self) -> None:
        # adjusted=4 → sig_fig_dp = 5-1-4 = 0 → integer
        assert round_price(Decimal("10000.7"), sz_decimals=5, side="bid") == Decimal("10000")
        assert round_price(Decimal("10000.7"), sz_decimals=5, side="ask") == Decimal("10001")

    def test_subdollar_price(self) -> None:
        # 0.001234, sz_decimals=0, max_dp=6, adjusted=-3 → sig_fig_dp = 5-1-(-3) = 7, dp = min(7,6) = 6
        assert round_price(
            Decimal("0.0012345"), sz_decimals=0, max_decimals=6, side="bid"
        ) == Decimal("0.001234")

    def test_bid_rounds_down(self) -> None:
        # ROUND_DOWN: 1850.55 -> 1850.5 (down) for bid.
        assert round_price(Decimal("1850.59"), sz_decimals=4, side="bid") == Decimal("1850.5")

    def test_ask_rounds_up(self) -> None:
        assert round_price(Decimal("1850.51"), sz_decimals=4, side="ask") == Decimal("1850.6")

    def test_negative_price_raises(self) -> None:
        with pytest.raises(ValueError):
            round_price(Decimal("-1"), sz_decimals=4)

    def test_zero_price_raises(self) -> None:
        with pytest.raises(ValueError):
            round_price(Decimal("0"), sz_decimals=4)


class TestRoundSize:
    def test_eth_size(self) -> None:
        # ETH szDecimals=4 → 4 decimals
        assert round_size(Decimal("0.0234567"), sz_decimals=4) == Decimal("0.0234")

    def test_btc_size(self) -> None:
        assert round_size(Decimal("0.000123456"), sz_decimals=5) == Decimal("0.00012")

    def test_already_at_precision(self) -> None:
        assert round_size(Decimal("0.0234"), sz_decimals=4) == Decimal("0.0234")

    def test_zero_decimals(self) -> None:
        assert round_size(Decimal("12.7"), sz_decimals=0) == Decimal("12")

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            round_size(Decimal("-0.01"), sz_decimals=4)

    def test_round_down_never_up(self) -> None:
        # Aina kohti nollaa, jotta ei ylitetä haluttua kokoa
        assert round_size(Decimal("0.02349999"), sz_decimals=4) == Decimal("0.0234")


# ---------- TokenBucket ----------


class TestTokenBucket:
    async def test_under_capacity_no_wait(self) -> None:
        b = TokenBucket(rate_per_second=100.0, capacity=10)
        for _ in range(10):
            await b.acquire()
        # 10 acquires kapasiteetilla 10 → ei refilliä tarvita

    async def test_throttles_when_empty(self) -> None:
        import time as time_mod
        b = TokenBucket(rate_per_second=50.0, capacity=2)
        await b.acquire()
        await b.acquire()
        # Buffer tyhjä — seuraava odottaa noin 1/50 = 20ms
        t0 = time_mod.perf_counter()
        await b.acquire()
        elapsed = time_mod.perf_counter() - t0
        assert elapsed >= 0.015  # vähintään ~20ms (toleranssi schedulerin sumean ajastuksen takia)

    async def test_invalid_rate(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate_per_second=0)
        with pytest.raises(ValueError):
            TokenBucket(rate_per_second=-1)


# ---------- _categorize ----------


class TestCategorize:
    @pytest.mark.parametrize(
        "exc,expected",
        [
            (ConnectionError("conn refused"), "network"),
            (TimeoutError("timed out"), "network"),
            (OSError("network unreachable"), "network"),
            (Exception("HTTP 429 Too Many Requests"), "rate_limit"),
            (Exception("rate limit exceeded"), "rate_limit"),
            (Exception("Throttle-Mode active"), "rate_limit"),
            (Exception("connection reset by peer"), "network"),
            (Exception("Insufficient balance"), "validation"),
            (Exception("Post only order would have crossed the book"), "validation"),
            (Exception("Price must be divisible by tick size"), "validation"),
            (Exception("invalid order size"), "validation"),
            (Exception("Order not found"), "validation"),
            (Exception("Invalid signature"), "auth"),
            (Exception("Agent does not exist for this address"), "auth"),
            (Exception("unauthorized"), "auth"),
            (Exception("something weird happened"), "unknown"),
        ],
    )
    def test_categorize(self, exc: Exception, expected: str) -> None:
        assert _categorize(exc) == expected


def test_extract_retry_after() -> None:
    assert _extract_retry_after(Exception("HTTP 429: Retry-After: 5")) == 5.0
    assert _extract_retry_after(Exception("retry-after 2.5")) == 2.5
    assert _extract_retry_after(Exception("rate limit")) is None


# ---------- _parse_order_response ----------


class TestParseOrderResponse:
    def test_resting(self) -> None:
        resp = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"resting": {"oid": 12345}}]},
            },
        }
        r = _parse_order_response(resp, cloid="0xabc")
        assert r == OrderResult(
            status="resting",
            oid=12345,
            cloid="0xabc",
            avg_price=None,
            filled_size=None,
        )
        assert r.success

    def test_filled(self) -> None:
        resp = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {"filled": {"totalSz": "0.1", "avgPx": "1850.5", "oid": 99}}
                    ]
                },
            },
        }
        r = _parse_order_response(resp, cloid=None)
        assert r.status == "filled"
        assert r.oid == 99
        assert r.avg_price == Decimal("1850.5")
        assert r.filled_size == Decimal("0.1")
        assert r.success

    def test_error_status(self) -> None:
        resp = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"error": "Post only order would have crossed"}]},
            },
        }
        r = _parse_order_response(resp, cloid=None)
        assert r.status == "rejected"
        assert r.error is not None
        assert "Post only" in r.error
        assert not r.success

    def test_top_level_not_ok(self) -> None:
        r = _parse_order_response({"status": "err"}, cloid=None)
        assert r.status == "rejected"


# ---------- HLClient (mocked SDK) ----------


_FAKE_META: dict[str, Any] = {
    "universe": [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 50, "isDelisted": False},
        {"name": "ETH", "szDecimals": 4, "maxLeverage": 25, "isDelisted": False},
    ]
}


def _resting_response(oid: int = 1) -> dict[str, Any]:
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"resting": {"oid": oid}}]},
        },
    }


def _success_cancel_response() -> dict[str, Any]:
    return {
        "status": "ok",
        "response": {
            "type": "cancel",
            "data": {"statuses": ["success"]},
        },
    }


@pytest.fixture
def info_mock() -> MagicMock:
    m = MagicMock()
    m.meta.return_value = _FAKE_META
    return m


@pytest.fixture
def exchange_mock() -> MagicMock:
    return MagicMock()


@pytest.fixture
def hl_config() -> HyperliquidConfig:
    return HyperliquidConfig(
        network="testnet",
        api_wallet_address="0x" + "1" * 40,
    )


@pytest.fixture
def client(
    hl_config: HyperliquidConfig,
    info_mock: MagicMock,
    exchange_mock: MagicMock,
) -> HLClient:
    return HLClient(
        hl_config,
        private_key="",
        info=info_mock,
        exchange=exchange_mock,
        requests_per_second=10_000.0,  # tests should not be throttled
        retry_delays_s=(0.001, 0.001, 0.001),
        meta_cache_ttl_seconds=300.0,
    )


# ----- meta cache -----


async def test_meta_cache_first_call_fetches(
    client: HLClient, info_mock: MagicMock
) -> None:
    meta = await client.get_asset_meta("ETH")
    assert meta.symbol == "ETH"
    assert meta.asset_id == 1
    assert meta.sz_decimals == 4
    assert meta.max_leverage == 25
    assert info_mock.meta.call_count == 1


async def test_meta_cache_reused_within_ttl(
    client: HLClient, info_mock: MagicMock
) -> None:
    await client.get_asset_meta("ETH")
    await client.get_asset_meta("BTC")
    assert info_mock.meta.call_count == 1


async def test_meta_unknown_symbol_raises(client: HLClient) -> None:
    with pytest.raises(HLClientError):
        await client.get_asset_meta("UNKNOWN")


# ----- place_order -----


async def test_place_order_post_only_passes_alo_tif(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.return_value = _resting_response(oid=42)
    result = await client.place_order(
        "ETH", "bid", Decimal("1850.55"), Decimal("0.02"),
        post_only=True, client_order_id=None,
    )
    assert result.status == "resting"
    assert result.oid == 42
    # Verify SDK call args
    kwargs = exchange_mock.order.call_args.kwargs
    assert kwargs["name"] == "ETH"
    assert kwargs["is_buy"] is True
    assert kwargs["order_type"] == {"limit": {"tif": "Alo"}}
    assert kwargs["reduce_only"] is False
    assert kwargs["limit_px"] == 1850.5  # rounded down for bid (5 sig figs)
    assert kwargs["sz"] == 0.02


async def test_place_order_non_post_only_uses_gtc(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.return_value = _resting_response()
    await client.place_order(
        "ETH", "ask", Decimal("1900"), Decimal("0.01"), post_only=False,
    )
    kwargs = exchange_mock.order.call_args.kwargs
    assert kwargs["order_type"] == {"limit": {"tif": "Gtc"}}
    assert kwargs["is_buy"] is False


async def test_place_order_rounds_price_per_symbol(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.return_value = _resting_response()
    # ETH: szDecimals=4, max_dp=2, sig_fig_dp=1 (around 1850) → 1 decimal
    await client.place_order("ETH", "bid", Decimal("1850.999"), Decimal("0.05"))
    px = exchange_mock.order.call_args.kwargs["limit_px"]
    assert px == 1850.9  # round_down


async def test_place_order_rounds_size_down(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.return_value = _resting_response()
    await client.place_order("ETH", "bid", Decimal("1800"), Decimal("0.0234567"))
    sz = exchange_mock.order.call_args.kwargs["sz"]
    assert sz == 0.0234


async def test_place_order_size_rounds_to_zero_rejected(client: HLClient) -> None:
    # ETH szDecimals=4 → koko 0.00001 pyöristyy alas → 0
    with pytest.raises(HLOrderRejectedError):
        await client.place_order("ETH", "bid", Decimal("1800"), Decimal("0.00001"))


async def test_place_order_filled_response(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.return_value = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [
                    {"filled": {"totalSz": "0.02", "avgPx": "1850.5", "oid": 100}}
                ]
            },
        },
    }
    r = await client.place_order("ETH", "bid", Decimal("1850.5"), Decimal("0.02"))
    assert r.status == "filled"
    assert r.oid == 100
    assert r.avg_price == Decimal("1850.5")
    assert r.filled_size == Decimal("0.02")


async def test_place_order_rejected_in_response_returns_rejected_result(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.return_value = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [{"error": "Post only order would have crossed the book"}]
            },
        },
    }
    r = await client.place_order("ETH", "bid", Decimal("1850.5"), Decimal("0.02"))
    assert r.status == "rejected"
    assert r.error is not None
    assert "Post only" in r.error


async def test_place_order_with_cloid(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    cloid_str = "0x" + "ab" * 16  # 128-bit hex
    exchange_mock.order.return_value = _resting_response(oid=7)
    result = await client.place_order(
        "ETH", "bid", Decimal("1800"), Decimal("0.02"),
        client_order_id=cloid_str,
    )
    assert result.cloid == cloid_str
    assert exchange_mock.order.call_args.kwargs["cloid"] is not None


# ----- retry semantics -----


async def test_retry_on_network_error_succeeds(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.side_effect = [
        ConnectionError("conn refused"),
        ConnectionError("conn refused"),
        _resting_response(oid=99),
    ]
    r = await client.place_order("ETH", "bid", Decimal("1850"), Decimal("0.02"))
    assert r.oid == 99
    assert exchange_mock.order.call_count == 3


async def test_retry_exhausted_raises_network_error(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.side_effect = [
        ConnectionError("conn refused"),
        ConnectionError("conn refused"),
        ConnectionError("conn refused"),
        ConnectionError("conn refused"),
    ]
    with pytest.raises(HLNetworkError):
        await client.place_order("ETH", "bid", Decimal("1850"), Decimal("0.02"))
    assert exchange_mock.order.call_count == 4  # initial + 3 retries


async def test_retry_on_429(client: HLClient, exchange_mock: MagicMock) -> None:
    exchange_mock.order.side_effect = [
        Exception("HTTP 429 Too Many Requests"),
        _resting_response(oid=5),
    ]
    r = await client.place_order("ETH", "bid", Decimal("1850"), Decimal("0.02"))
    assert r.oid == 5
    assert exchange_mock.order.call_count == 2


async def test_retry_after_header_respected(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    import time as time_mod
    exchange_mock.order.side_effect = [
        Exception("HTTP 429: Retry-After: 0.05"),
        _resting_response(oid=7),
    ]
    t0 = time_mod.perf_counter()
    await client.place_order("ETH", "bid", Decimal("1850"), Decimal("0.02"))
    elapsed = time_mod.perf_counter() - t0
    assert elapsed >= 0.04  # ~50ms


async def test_no_retry_on_validation_error(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.side_effect = Exception("Insufficient balance for order")
    with pytest.raises(HLOrderRejectedError):
        await client.place_order("ETH", "bid", Decimal("1850"), Decimal("0.02"))
    assert exchange_mock.order.call_count == 1  # ei retryä


async def test_no_retry_on_auth_error(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.side_effect = Exception("Invalid signature")
    with pytest.raises(HLAuthError):
        await client.place_order("ETH", "bid", Decimal("1850"), Decimal("0.02"))
    assert exchange_mock.order.call_count == 1


async def test_rate_limit_exhausted_raises(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.order.side_effect = [Exception("HTTP 429")] * 4
    with pytest.raises(HLRateLimitError):
        await client.place_order("ETH", "bid", Decimal("1850"), Decimal("0.02"))


# ----- cancel / modify / queries -----


async def test_cancel_order_success(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.cancel.return_value = _success_cancel_response()
    ok = await client.cancel_order("ETH", oid=42)
    assert ok is True
    exchange_mock.cancel.assert_called_once_with("ETH", 42)


async def test_cancel_order_not_found_returns_false(
    client: HLClient, exchange_mock: MagicMock
) -> None:
    exchange_mock.cancel.side_effect = Exception("Order not found")
    ok = await client.cancel_order("ETH", oid=42)
    assert ok is False


async def test_cancel_all_orders(
    client: HLClient, info_mock: MagicMock, exchange_mock: MagicMock
) -> None:
    info_mock.open_orders.return_value = [
        {"coin": "ETH", "oid": 1, "side": "B", "limitPx": "1850.5", "sz": "0.02", "timestamp": 1},
        {"coin": "ETH", "oid": 2, "side": "A", "limitPx": "1851.0", "sz": "0.02", "timestamp": 2},
    ]
    exchange_mock.bulk_cancel.return_value = {
        "status": "ok",
        "response": {"type": "cancel", "data": {"statuses": ["success", "success"]}},
    }
    n = await client.cancel_all_orders("ETH")
    assert n == 2
    exchange_mock.bulk_cancel.assert_called_once()
    cancels = exchange_mock.bulk_cancel.call_args.args[0]
    assert {c["oid"] for c in cancels} == {1, 2}


async def test_cancel_all_orders_no_opens(
    client: HLClient, info_mock: MagicMock, exchange_mock: MagicMock
) -> None:
    info_mock.open_orders.return_value = []
    n = await client.cancel_all_orders("ETH")
    assert n == 0
    exchange_mock.bulk_cancel.assert_not_called()


async def test_modify_order(client: HLClient, exchange_mock: MagicMock) -> None:
    exchange_mock.modify_order.return_value = _resting_response(oid=42)
    r = await client.modify_order(
        "ETH", oid=42, side="bid",
        new_price=Decimal("1851.55"),
        new_size=Decimal("0.0234567"),
    )
    assert r.status == "resting"
    kwargs = exchange_mock.modify_order.call_args.kwargs
    assert kwargs["oid"] == 42
    assert kwargs["name"] == "ETH"
    assert kwargs["is_buy"] is True
    assert kwargs["order_type"] == {"limit": {"tif": "Alo"}}
    assert kwargs["limit_px"] == 1851.5  # bid → round_down
    assert kwargs["sz"] == 0.0234


async def test_get_open_orders(
    client: HLClient, info_mock: MagicMock
) -> None:
    info_mock.open_orders.return_value = [
        {"coin": "ETH", "oid": 1, "side": "B", "limitPx": "1850.5", "sz": "0.02", "timestamp": 1000},
        {"coin": "BTC", "oid": 2, "side": "A", "limitPx": "60000", "sz": "0.001", "timestamp": 2000},
    ]
    eth_only = await client.get_open_orders("ETH")
    assert len(eth_only) == 1
    assert eth_only[0].oid == 1
    assert eth_only[0].side == "bid"
    assert eth_only[0].price == Decimal("1850.5")

    all_orders = await client.get_open_orders()
    assert len(all_orders) == 2


async def test_get_position(
    client: HLClient, info_mock: MagicMock
) -> None:
    info_mock.user_state.return_value = {
        "assetPositions": [
            {
                "type": "oneWay",
                "position": {
                    "coin": "ETH",
                    "szi": "0.05",
                    "entryPx": "1820.5",
                    "unrealizedPnl": "1.50",
                },
            }
        ]
    }
    pos = await client.get_position("ETH")
    assert pos.symbol == "ETH"
    assert pos.size == Decimal("0.05")
    assert pos.entry_price == Decimal("1820.5")
    assert pos.unrealized_pnl == Decimal("1.50")


async def test_get_position_when_flat(
    client: HLClient, info_mock: MagicMock
) -> None:
    info_mock.user_state.return_value = {"assetPositions": []}
    pos = await client.get_position("ETH")
    assert pos.size == Decimal("0")
    assert pos.entry_price == Decimal("0")


async def test_get_user_fills(
    client: HLClient, info_mock: MagicMock
) -> None:
    info_mock.user_fills_by_time.return_value = [
        {
            "time": 1700_000_000_000,
            "coin": "ETH",
            "side": "B",
            "px": "1850.5",
            "sz": "0.02",
            "fee": "0.0028",
            "oid": 42,
            "crossed": False,
        }
    ]
    fills = await client.get_user_fills(start_time_ms=1700_000_000_000)
    assert len(fills) == 1
    f = fills[0]
    assert f.symbol == "ETH"
    assert f.side == "bid"
    assert f.price == Decimal("1850.5")
    assert f.is_maker is True


async def test_get_funding_rate(
    client: HLClient, info_mock: MagicMock
) -> None:
    info_mock.meta_and_asset_ctxs.return_value = [
        _FAKE_META,
        [
            {"funding": "0.00001234", "openInterest": "100"},  # BTC (asset_id=0)
            {"funding": "-0.00005678", "openInterest": "50"},  # ETH (asset_id=1)
        ],
    ]
    eth_funding = await client.get_funding_rate("ETH")
    assert eth_funding == Decimal("-0.00005678")


# ----- Cloid helper -----


def test_make_cloid_none() -> None:
    assert _make_cloid(None) is None


def test_make_cloid_valid_returns_object() -> None:
    cloid = _make_cloid("0x" + "ab" * 16)
    # Should be an SDK Cloid instance — verify it's not None at minimum.
    assert cloid is not None


# ----- private_key validation -----


def test_no_private_key_when_no_injection_raises() -> None:
    with pytest.raises(HLAuthError):
        HLClient(
            HyperliquidConfig(
                network="testnet",
                api_wallet_address="0x" + "1" * 40,
            ),
            private_key="",
        )
