"""Tests for src.notifier."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.notifier import (
    KILL_CONFIRM_TOKEN,
    InventoryInfo,
    OrderListItem,
    PnLInfo,
    StatusInfo,
    TelegramNotifier,
    _RateBucket,
    escape_markdown_v2,
)

# ---------- Fake telegram primitives ----------


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[tuple[str, dict[str, Any]]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append((text, kwargs))


class FakeChat:
    def __init__(self, chat_id: int | str) -> None:
        self.id = chat_id


class FakeUpdate:
    def __init__(self, chat_id: int | str, text: str = "") -> None:
        self.effective_chat = FakeChat(chat_id)
        self.message = FakeMessage(text=text)


def _make_app() -> MagicMock:
    app = MagicMock()
    app.bot = MagicMock()
    app.bot.send_message = AsyncMock()
    return app


def _make_notifier(
    *,
    chat_id: str = "12345",
    rate_limit_seconds: float = 0.0,  # default no rate limit for tests
    app: MagicMock | None = None,
    **kwargs: Any,
) -> TelegramNotifier:
    return TelegramNotifier(
        bot_token="abc:def",
        chat_id=chat_id,
        rate_limit_seconds=rate_limit_seconds,
        application=app or _make_app(),
        **kwargs,
    )


# ---------- escape_markdown_v2 ----------


class TestEscapeMarkdownV2:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("hello", "hello"),
            ("foo.bar", r"foo\.bar"),
            ("a*b", r"a\*b"),
            ("(parens)", r"\(parens\)"),
            ("under_score", r"under\_score"),
            ("[link]", r"\[link\]"),
            ("a+b=c", r"a\+b\=c"),
            ("hash#tag", r"hash\#tag"),
            ("dash-line", r"dash\-line"),
            ("100.55%", r"100\.55%"),  # % not special
        ],
    )
    def test_escape(self, raw: str, expected: str) -> None:
        assert escape_markdown_v2(raw) == expected


# ---------- Rate bucket ----------


class TestRateBucket:
    def test_first_allows(self) -> None:
        b = _RateBucket(period_s=10.0)
        assert b.allow() is True

    def test_second_blocked_within_window(self) -> None:
        b = _RateBucket(period_s=10.0)
        b.allow()
        assert b.allow() is False

    def test_zero_period_always_allows(self) -> None:
        b = _RateBucket(period_s=0.0)
        for _ in range(10):
            assert b.allow() is True

    def test_after_period_allows(self) -> None:
        b = _RateBucket(period_s=0.05)
        b.allow()
        time.sleep(0.06)
        assert b.allow() is True


# ---------- Auth ----------


async def test_check_auth_matches() -> None:
    n = _make_notifier(chat_id="12345")
    assert n.check_auth("12345") is True
    assert n.check_auth(12345) is True


async def test_check_auth_rejects_mismatch() -> None:
    n = _make_notifier(chat_id="12345")
    assert n.check_auth("99999") is False
    assert n.check_auth(None) is False


async def test_command_from_unauthorized_chat_ignored() -> None:
    app = _make_app()
    n = _make_notifier(chat_id="12345", app=app, status_provider=lambda: _status())
    update = FakeUpdate(chat_id="99999")
    await n._cmd_status(update, None)
    # No reply sent (unauthorized)
    assert len(update.message.replies) == 0


# ---------- send_alert / rate limit ----------


async def test_send_alert_info_sends() -> None:
    app = _make_app()
    n = _make_notifier(app=app, rate_limit_seconds=10.0)
    sent = await n.send_alert("info", "hello world")
    assert sent is True
    app.bot.send_message.assert_awaited_once()
    text = app.bot.send_message.await_args.kwargs["text"]
    assert "INFO" in text
    assert "hello world" in text


async def test_send_alert_rate_limited() -> None:
    app = _make_app()
    n = _make_notifier(app=app, rate_limit_seconds=10.0)
    assert await n.send_alert("info", "first") is True
    assert await n.send_alert("info", "second") is False  # rate-limited
    assert app.bot.send_message.await_count == 1


async def test_critical_bypasses_rate_limit() -> None:
    app = _make_app()
    n = _make_notifier(app=app, rate_limit_seconds=10.0)
    assert await n.send_alert("critical", "first") is True
    assert await n.send_alert("critical", "second") is True
    assert await n.send_alert("critical", "third") is True
    assert app.bot.send_message.await_count == 3


async def test_force_bypasses_rate_limit() -> None:
    app = _make_app()
    n = _make_notifier(app=app, rate_limit_seconds=10.0)
    await n.send_alert("info", "first")
    sent = await n.send_alert("info", "forced", force=True)
    assert sent is True


async def test_per_category_rate_limit() -> None:
    """info ja warning kuluttavat erilliset bucketit."""
    app = _make_app()
    n = _make_notifier(app=app, rate_limit_seconds=10.0)
    assert await n.send_alert("info", "x") is True
    assert await n.send_alert("warning", "x") is True
    # Second info blocked, but error not yet used
    assert await n.send_alert("info", "y") is False
    assert await n.send_alert("error", "z") is True


async def test_unknown_level_raises() -> None:
    n = _make_notifier()
    with pytest.raises(ValueError):
        await n.send_alert("oops", "msg")  # type: ignore[arg-type]


async def test_send_uses_markdown_v2() -> None:
    app = _make_app()
    n = _make_notifier(app=app)
    await n.send_alert("info", "hello.world")
    kwargs = app.bot.send_message.await_args.kwargs
    # parse_mode argument present
    pm = kwargs.get("parse_mode")
    assert pm is not None
    # Special chars escaped in body
    assert r"hello\.world" in kwargs["text"]


async def test_alert_includes_level_and_emoji() -> None:
    app = _make_app()
    n = _make_notifier(app=app)
    await n.send_alert("critical", "boom")
    text = app.bot.send_message.await_args.kwargs["text"]
    # critical has emoji + text
    assert "CRITICAL" in text


async def test_token_never_logged_in_repr() -> None:
    n = _make_notifier()
    # Repr should not leak the token
    assert "abc:def" not in repr(n)


# ---------- /status command ----------


def _status(**overrides: Any) -> StatusInfo:
    defaults: dict[str, Any] = {
        "uptime_seconds": 1234.0,
        "is_paused": False,
        "is_killed": False,
        "pause_reason": None,
        "kill_reason": None,
        "session_pnl": "+1.50 USDC",
        "position": "0.05 ETH @ 1850.00",
        "active_orders": 8,
    }
    defaults.update(overrides)
    return StatusInfo(**defaults)


async def test_status_command_replies_with_data() -> None:
    n = _make_notifier(status_provider=lambda: _status())
    update = FakeUpdate(chat_id="12345")
    await n._cmd_status(update, None)
    assert len(update.message.replies) == 1
    text, _ = update.message.replies[0]
    assert "Status" in text
    assert "1234s" in text
    # Position rendered (with escaped @ — but @ isn't in MD v2 special list, so plain)
    assert "0.05" in text or r"0\.05" in text
    assert "ETH" in text
    assert "False" in text  # not paused, not killed


async def test_status_with_pause_reason() -> None:
    n = _make_notifier(
        status_provider=lambda: _status(is_paused=True, pause_reason="vol halt"),
    )
    update = FakeUpdate(chat_id="12345")
    await n._cmd_status(update, None)
    text, _ = update.message.replies[0]
    assert "True" in text
    assert "vol halt" in text


async def test_status_provider_missing_returns_message() -> None:
    n = _make_notifier()  # no provider
    update = FakeUpdate(chat_id="12345")
    await n._cmd_status(update, None)
    text, _ = update.message.replies[0]
    assert "not configured" in text


async def test_status_provider_error_handled() -> None:
    def boom() -> StatusInfo:
        raise RuntimeError("db down")

    n = _make_notifier(status_provider=boom)
    update = FakeUpdate(chat_id="12345")
    await n._cmd_status(update, None)
    text, _ = update.message.replies[0]
    assert "error" in text.lower()


# ---------- /pnl, /inventory, /orders ----------


async def test_pnl_command() -> None:
    pnl = PnLInfo(
        realized_pnl="+2.10",
        unrealized_pnl="-0.40",
        total_fees="0.30",
        rebate_earned="0.00",
        session_pnl_pct="+1.40%",
    )
    n = _make_notifier(pnl_provider=lambda: pnl)
    update = FakeUpdate(chat_id="12345")
    await n._cmd_pnl(update, None)
    text, _ = update.message.replies[0]
    assert "PnL" in text
    assert "2.10" in text or r"2\.10" in text


async def test_inventory_command() -> None:
    inv = InventoryInfo(
        position="0.05 ETH",
        avg_entry="1850.5",
        value_usd="92.50",
        skew="0.50",
    )
    n = _make_notifier(inventory_provider=lambda: inv)
    update = FakeUpdate(chat_id="12345")
    await n._cmd_inventory(update, None)
    text, _ = update.message.replies[0]
    assert "Inventory" in text
    assert "ETH" in text


async def test_orders_command_lists_active() -> None:
    orders = [
        OrderListItem(side="bid", price="1849.5", size="0.02", age_seconds=5),
        OrderListItem(side="ask", price="1850.5", size="0.02", age_seconds=10),
    ]
    n = _make_notifier(orders_provider=lambda: orders)
    update = FakeUpdate(chat_id="12345")
    await n._cmd_orders(update, None)
    text, _ = update.message.replies[0]
    assert "bid" in text
    assert "ask" in text
    assert "1849" in text or r"1849\.5" in text


async def test_orders_command_empty() -> None:
    n = _make_notifier(orders_provider=lambda: [])
    update = FakeUpdate(chat_id="12345")
    await n._cmd_orders(update, None)
    text, _ = update.message.replies[0]
    assert "No active" in text


async def test_orders_command_truncates_long_list() -> None:
    orders = [
        OrderListItem(side="bid", price="1849", size="0.01", age_seconds=1)
        for _ in range(30)
    ]
    n = _make_notifier(orders_provider=lambda: orders)
    update = FakeUpdate(chat_id="12345")
    await n._cmd_orders(update, None)
    text, _ = update.message.replies[0]
    assert "more" in text


# ---------- /pause, /resume ----------


async def test_pause_command_invokes_callback() -> None:
    fired: list[None] = []

    async def on_pause() -> None:
        fired.append(None)

    n = _make_notifier(on_pause=on_pause)
    update = FakeUpdate(chat_id="12345")
    await n._cmd_pause(update, None)
    assert len(fired) == 1
    text, _ = update.message.replies[0]
    assert "Paused" in text


async def test_resume_command_invokes_callback() -> None:
    fired: list[None] = []

    async def on_resume() -> None:
        fired.append(None)

    n = _make_notifier(on_resume=on_resume)
    update = FakeUpdate(chat_id="12345")
    await n._cmd_resume(update, None)
    assert len(fired) == 1
    text, _ = update.message.replies[0]
    assert "Resumed" in text


async def test_pause_command_unauthorized_does_not_invoke() -> None:
    fired: list[None] = []

    async def on_pause() -> None:
        fired.append(None)

    n = _make_notifier(chat_id="12345", on_pause=on_pause)
    update = FakeUpdate(chat_id="99999")
    await n._cmd_pause(update, None)
    assert len(fired) == 0


# ---------- /kill confirmation flow ----------


async def test_kill_requires_confirmation() -> None:
    fired: list[str] = []

    async def on_kill(reason: str) -> None:
        fired.append(reason)

    n = _make_notifier(on_kill=on_kill)
    # First /kill — prompts for confirmation
    update1 = FakeUpdate(chat_id="12345")
    await n._cmd_kill(update1, None)
    text1, _ = update1.message.replies[0]
    assert "KILL" in text1
    assert KILL_CONFIRM_TOKEN in text1
    assert len(fired) == 0

    # Confirmation message
    update2 = FakeUpdate(chat_id="12345", text=KILL_CONFIRM_TOKEN)
    await n._handle_text(update2, None)
    assert len(fired) == 1
    assert fired[0] == "manual via Telegram"


async def test_kill_confirm_without_pending_does_nothing() -> None:
    fired: list[str] = []

    async def on_kill(reason: str) -> None:
        fired.append(reason)

    n = _make_notifier(on_kill=on_kill)
    update = FakeUpdate(chat_id="12345", text=KILL_CONFIRM_TOKEN)
    await n._handle_text(update, None)
    assert len(fired) == 0
    text, _ = update.message.replies[0]
    assert "No pending" in text


async def test_kill_confirm_expired() -> None:
    fired: list[str] = []

    async def on_kill(reason: str) -> None:
        fired.append(reason)

    n = _make_notifier(on_kill=on_kill, kill_confirm_timeout_s=0.05)
    update = FakeUpdate(chat_id="12345")
    await n._cmd_kill(update, None)
    await asyncio.sleep(0.1)
    update2 = FakeUpdate(chat_id="12345", text=KILL_CONFIRM_TOKEN)
    await n._handle_text(update2, None)
    assert len(fired) == 0  # expired
    text, _ = update2.message.replies[0]
    assert "expired" in text.lower()


async def test_kill_random_text_ignored() -> None:
    fired: list[str] = []

    async def on_kill(reason: str) -> None:
        fired.append(reason)

    n = _make_notifier(on_kill=on_kill)
    await n._cmd_kill(FakeUpdate(chat_id="12345"), None)
    update = FakeUpdate(chat_id="12345", text="not the right text")
    await n._handle_text(update, None)
    assert len(fired) == 0
    # No reply because the text doesn't match KILL_CONFIRM_TOKEN
    assert len(update.message.replies) == 0


async def test_kill_confirm_unauthorized_ignored() -> None:
    fired: list[str] = []

    async def on_kill(reason: str) -> None:
        fired.append(reason)

    n = _make_notifier(chat_id="12345", on_kill=on_kill)
    await n._cmd_kill(FakeUpdate(chat_id="12345"), None)
    # Confirmation from different chat
    update = FakeUpdate(chat_id="99999", text=KILL_CONFIRM_TOKEN)
    await n._handle_text(update, None)
    assert len(fired) == 0


async def test_secrets_pydantic_input_handled() -> None:
    """SecretStr input for token should also work."""
    from pydantic import SecretStr

    n = TelegramNotifier(
        bot_token=SecretStr("abc:def"),
        chat_id="123",
        application=_make_app(),
    )
    assert n.check_auth("123") is True
