"""Telegram-notifier — hälytykset + komento-rajapinta.

Lähettää hälytyksiä severity-tasoittain (info / warning / error / critical).
Critical ohittaa rate limitin; muut on rajoitettu yhdellä viestillä per
kategoria per `rate_limit_seconds`.

Komennot (vain `chat_id`-matchaava käyttäjä):
    /status     — uptime, position, session PnL, paused/killed-tila
    /pnl        — realized + unrealized + fees breakdown
    /inventory  — current size, value, avg entry
    /orders     — aktiivisten ordereiden lista
    /pause      — pyydä quote-pause (ei kill, peruuttaa quotet)
    /resume     — pause:n peruutus
    /kill       — kaksi-vaiheinen vahvistus → emergency_close

Auth: chat_id-tarkistus jokaisessa viestissä — botin tokenia EI ikinä logiteta.
Formatointi: MarkdownV2 (special-merkit escape:tään), aikaleima UTC + Helsinki.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import SecretStr

AlertLevel = Literal["info", "warning", "error", "critical"]

_LEVEL_EMOJI: dict[AlertLevel, str] = {
    "info": "ℹ️",  # noqa: RUF001 - emoji
    "warning": "⚠️",  # warning sign emoji
    "error": "\U0001f6a8",  # rotating police light
    "critical": "\U0001f480",  # skull
}

# Telegram MarkdownV2 special chars that must be escaped:
# https://core.telegram.org/bots/api#markdownv2-style
_MD_V2_ESCAPE = r"_*[]()~`>#+-=|{}.!\\"

DEFAULT_RATE_LIMIT_S = 10.0
DEFAULT_KILL_CONFIRM_TIMEOUT_S = 30.0
KILL_CONFIRM_TOKEN = "KILL CONFIRM"

_HELSINKI = ZoneInfo("Europe/Helsinki")

_logger = logging.getLogger(__name__)


# ---------- Data providers (callbacks) ----------


@dataclass(frozen=True)
class StatusInfo:
    uptime_seconds: float
    is_paused: bool
    is_killed: bool
    pause_reason: str | None
    kill_reason: str | None
    session_pnl: str  # already-formatted
    position: str  # "0.05 ETH"
    active_orders: int


@dataclass(frozen=True)
class PnLInfo:
    realized_pnl: str
    unrealized_pnl: str
    total_fees: str
    rebate_earned: str
    session_pnl_pct: str


@dataclass(frozen=True)
class InventoryInfo:
    position: str
    avg_entry: str
    value_usd: str
    skew: str


@dataclass(frozen=True)
class OrderListItem:
    side: str
    price: str
    size: str
    age_seconds: float


StatusProvider = Callable[[], StatusInfo]
PnLProvider = Callable[[], PnLInfo]
InventoryProvider = Callable[[], InventoryInfo]
OrdersProvider = Callable[[], list[OrderListItem]]
KillCallback = Callable[[str], Awaitable[None] | None]
PauseCallback = Callable[[], Awaitable[None] | None]


# ---------- Helpers ----------


def escape_markdown_v2(text: str) -> str:
    """Escape MarkdownV2 special chars. Käytä dynaamisille arvoille."""
    out: list[str] = []
    for c in text:
        if c in _MD_V2_ESCAPE:
            out.append("\\")
        out.append(c)
    return "".join(out)


def _now_timestamp_line() -> str:
    now_utc = datetime.now(UTC)
    now_local = now_utc.astimezone(_HELSINKI)
    utc_str = escape_markdown_v2(now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"))
    local_str = escape_markdown_v2(now_local.strftime("%H:%M Helsinki"))
    return f"_{utc_str} \\({local_str}\\)_"


# ---------- Rate limit (token-bucket lite) ----------


class _RateBucket:
    """Yksi viesti per `period_s`. Threading-yksinkertainen — ei lockeja."""

    def __init__(self, period_s: float) -> None:
        self.period = period_s
        self._last_emit: float = -1e9  # ennen alkua

    def allow(self) -> bool:
        now = time.monotonic()
        if now - self._last_emit >= self.period:
            self._last_emit = now
            return True
        return False


# ---------- Notifier ----------


class TelegramNotifier:
    """Telegram-bot hälytysten lähettämiseen + komentojen vastaanottoon.

    Tokenia EI logiteta missään tilanteessa. Vain `chat_id`-matchaavat
    viestit käsitellään (auth).

    Args:
        bot_token: bot-token env-muuttujasta (SecretStr tai str).
        chat_id: sallittu chat (master / yksittäinen käyttäjä).
        rate_limit_seconds: viesti-cap per kategoria (default 10s).
        kill_confirm_timeout_s: aikaikkuna /kill-vahvistukselle (default 30s).
        status_provider, pnl_provider, inventory_provider, orders_provider:
            sync-callbackit jotka palauttavat dataa komennoille.
        on_pause, on_resume: async/sync callbackit /pause ja /resume:lle.
        on_kill: async/sync callback /kill:lle (saa reason-stringin).
        application: optional injection (testit). Jos None, telegram
            Application luodaan `start()`:ssa.
    """

    def __init__(
        self,
        bot_token: SecretStr | str,
        chat_id: str | int,
        *,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_S,
        kill_confirm_timeout_s: float = DEFAULT_KILL_CONFIRM_TIMEOUT_S,
        status_provider: StatusProvider | None = None,
        pnl_provider: PnLProvider | None = None,
        inventory_provider: InventoryProvider | None = None,
        orders_provider: OrdersProvider | None = None,
        on_pause: PauseCallback | None = None,
        on_resume: PauseCallback | None = None,
        on_kill: KillCallback | None = None,
        application: Any | None = None,
    ) -> None:
        self._bot_token = (
            bot_token.get_secret_value()
            if isinstance(bot_token, SecretStr)
            else str(bot_token)
        )
        self._chat_id = str(chat_id)
        self._buckets: dict[AlertLevel, _RateBucket] = {
            "info": _RateBucket(rate_limit_seconds),
            "warning": _RateBucket(rate_limit_seconds),
            "error": _RateBucket(rate_limit_seconds),
            # critical bypassaa rate limitin → ei bucketia
        }
        self._kill_confirm_timeout = kill_confirm_timeout_s
        self._kill_pending = False
        self._kill_pending_expires_at: float = 0.0

        self._status_provider = status_provider
        self._pnl_provider = pnl_provider
        self._inventory_provider = inventory_provider
        self._orders_provider = orders_provider
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_kill = on_kill

        self._app: Any = application
        self._started = False
        self._lock = asyncio.Lock()

    # ----- Auth -----

    def check_auth(self, chat_id: str | int | None) -> bool:
        if chat_id is None:
            return False
        return str(chat_id) == self._chat_id

    # ----- Lifecycle -----

    async def start(self) -> None:
        if self._started:
            return
        if self._app is None:
            from telegram.ext import Application, CommandHandler, MessageHandler, filters

            app = Application.builder().token(self._bot_token).build()
            app.add_handler(CommandHandler("status", self._cmd_status))
            app.add_handler(CommandHandler("pnl", self._cmd_pnl))
            app.add_handler(CommandHandler("inventory", self._cmd_inventory))
            app.add_handler(CommandHandler("orders", self._cmd_orders))
            app.add_handler(CommandHandler("pause", self._cmd_pause))
            app.add_handler(CommandHandler("resume", self._cmd_resume))
            app.add_handler(CommandHandler("kill", self._cmd_kill))
            # Plain-text handler for "KILL CONFIRM"
            app.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text)
            )
            self._app = app

        await self._app.initialize()
        await self._app.start()
        if getattr(self._app, "updater", None) is not None:
            await self._app.updater.start_polling()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        if self._app is None:
            self._started = False
            return
        if (
            getattr(self._app, "updater", None) is not None
            and self._app.updater.running
        ):
            with contextlib.suppress(Exception):
                await self._app.updater.stop()
        with contextlib.suppress(Exception):
            await self._app.stop()
        with contextlib.suppress(Exception):
            await self._app.shutdown()
        self._started = False

    # ----- send_alert -----

    async def send_alert(
        self, level: AlertLevel, message: str, *, force: bool = False
    ) -> bool:
        """Lähetä hälytys. Palauttaa True jos lähti, False jos rate-limited.

        Critical aina läpi. force=True ohittaa rate-limit:in muille tasoille.
        """
        if level not in ("info", "warning", "error", "critical"):
            raise ValueError(f"unknown level: {level}")
        if level != "critical" and not force:
            bucket = self._buckets[level]
            if not bucket.allow():
                return False
        text = self._format_alert(level, message)
        await self._send_text(text)
        return True

    def _format_alert(self, level: AlertLevel, message: str) -> str:
        emoji = _LEVEL_EMOJI[level]
        # Otsikko = emoji + tason nimi (boldattu)
        head = f"{emoji} *{level.upper()}*"
        body = escape_markdown_v2(message)
        return f"{head}\n{body}\n{_now_timestamp_line()}"

    async def _send_text(self, text: str) -> None:
        if self._app is None:
            _logger.warning("notifier not initialized; dropping outgoing message")
            return
        from telegram.constants import ParseMode

        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            _logger.exception("telegram send_message failed")

    # ----- Command handlers -----

    async def _reply(self, update: Any, text: str) -> None:
        from telegram.constants import ParseMode

        if update is None or update.message is None:
            return
        try:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            _logger.exception("telegram reply_text failed")

    async def _cmd_status(self, update: Any, _context: Any) -> None:
        if not self.check_auth(_chat_id_of(update)):
            return
        text = self._build_status_response()
        await self._reply(update, text)

    async def _cmd_pnl(self, update: Any, _context: Any) -> None:
        if not self.check_auth(_chat_id_of(update)):
            return
        text = self._build_pnl_response()
        await self._reply(update, text)

    async def _cmd_inventory(self, update: Any, _context: Any) -> None:
        if not self.check_auth(_chat_id_of(update)):
            return
        text = self._build_inventory_response()
        await self._reply(update, text)

    async def _cmd_orders(self, update: Any, _context: Any) -> None:
        if not self.check_auth(_chat_id_of(update)):
            return
        text = self._build_orders_response()
        await self._reply(update, text)

    async def _cmd_pause(self, update: Any, _context: Any) -> None:
        if not self.check_auth(_chat_id_of(update)):
            return
        await self._invoke_pause()
        await self._reply(update, escape_markdown_v2("Paused — quotes cancelled."))

    async def _cmd_resume(self, update: Any, _context: Any) -> None:
        if not self.check_auth(_chat_id_of(update)):
            return
        await self._invoke_resume()
        await self._reply(update, escape_markdown_v2("Resumed — quoting enabled."))

    async def _cmd_kill(self, update: Any, _context: Any) -> None:
        if not self.check_auth(_chat_id_of(update)):
            return
        async with self._lock:
            self._kill_pending = True
            self._kill_pending_expires_at = (
                time.monotonic() + self._kill_confirm_timeout
            )
        prompt = (
            f"⚠️ *KILL REQUESTED*\n"
            f"Reply within {int(self._kill_confirm_timeout)}s with the exact text\n"
            f"`{KILL_CONFIRM_TOKEN}`\n"
            f"to confirm emergency close\\."
        )
        await self._reply(update, prompt)

    async def _handle_text(self, update: Any, _context: Any) -> None:
        if not self.check_auth(_chat_id_of(update)):
            return
        text = ""
        if update.message is not None and update.message.text is not None:
            text = update.message.text.strip()
        if text != KILL_CONFIRM_TOKEN:
            return
        async with self._lock:
            pending = self._kill_pending
            expired = time.monotonic() > self._kill_pending_expires_at
            self._kill_pending = False
        if not pending:
            await self._reply(
                update, escape_markdown_v2("No pending kill request.")
            )
            return
        if expired:
            await self._reply(
                update, escape_markdown_v2("Kill confirmation expired.")
            )
            return
        await self._reply(
            update,
            escape_markdown_v2("Confirmed. Triggering emergency close..."),
        )
        await self._invoke_kill("manual via Telegram")

    # ----- Response builders (pure, testable) -----

    def _build_status_response(self) -> str:
        if self._status_provider is None:
            return escape_markdown_v2("status provider not configured")
        try:
            s = self._status_provider()
        except Exception:
            _logger.exception("status_provider error")
            return escape_markdown_v2("error fetching status")
        lines = [
            "*Status*",
            f"Uptime: {escape_markdown_v2(f'{s.uptime_seconds:.0f}s')}",
            f"Position: {escape_markdown_v2(s.position)}",
            f"Session PnL: {escape_markdown_v2(s.session_pnl)}",
            f"Active orders: {escape_markdown_v2(str(s.active_orders))}",
            f"Paused: {escape_markdown_v2(str(s.is_paused))}",
            f"Killed: {escape_markdown_v2(str(s.is_killed))}",
        ]
        if s.pause_reason:
            lines.append(
                f"Pause reason: {escape_markdown_v2(s.pause_reason)}"
            )
        if s.kill_reason:
            lines.append(
                f"Kill reason: {escape_markdown_v2(s.kill_reason)}"
            )
        return "\n".join(lines)

    def _build_pnl_response(self) -> str:
        if self._pnl_provider is None:
            return escape_markdown_v2("pnl provider not configured")
        try:
            p = self._pnl_provider()
        except Exception:
            _logger.exception("pnl_provider error")
            return escape_markdown_v2("error fetching pnl")
        return "\n".join([
            "*PnL*",
            f"Realized: {escape_markdown_v2(p.realized_pnl)}",
            f"Unrealized: {escape_markdown_v2(p.unrealized_pnl)}",
            f"Fees: {escape_markdown_v2(p.total_fees)}",
            f"Rebate: {escape_markdown_v2(p.rebate_earned)}",
            f"Session: {escape_markdown_v2(p.session_pnl_pct)}",
        ])

    def _build_inventory_response(self) -> str:
        if self._inventory_provider is None:
            return escape_markdown_v2("inventory provider not configured")
        try:
            i = self._inventory_provider()
        except Exception:
            _logger.exception("inventory_provider error")
            return escape_markdown_v2("error fetching inventory")
        return "\n".join([
            "*Inventory*",
            f"Position: {escape_markdown_v2(i.position)}",
            f"Avg entry: {escape_markdown_v2(i.avg_entry)}",
            f"Value USD: {escape_markdown_v2(i.value_usd)}",
            f"Skew: {escape_markdown_v2(i.skew)}",
        ])

    def _build_orders_response(self) -> str:
        if self._orders_provider is None:
            return escape_markdown_v2("orders provider not configured")
        try:
            orders = self._orders_provider()
        except Exception:
            _logger.exception("orders_provider error")
            return escape_markdown_v2("error fetching orders")
        if not orders:
            return escape_markdown_v2("No active orders.")
        lines = [f"*Active orders \\({len(orders)}\\)*"]
        for o in orders[:20]:  # max 20 to avoid spam
            lines.append(
                f"{escape_markdown_v2(o.side)} "
                f"{escape_markdown_v2(o.price)} "
                f"x {escape_markdown_v2(o.size)} "
                f"\\({escape_markdown_v2(f'{o.age_seconds:.0f}s')}\\)"
            )
        if len(orders) > 20:
            lines.append(escape_markdown_v2(f"... and {len(orders) - 20} more"))
        return "\n".join(lines)

    # ----- Callbacks -----

    async def _invoke_pause(self) -> None:
        if self._on_pause is None:
            return
        try:
            r = self._on_pause()
            if asyncio.iscoroutine(r):
                await r
        except Exception:
            _logger.exception("on_pause callback error")

    async def _invoke_resume(self) -> None:
        if self._on_resume is None:
            return
        try:
            r = self._on_resume()
            if asyncio.iscoroutine(r):
                await r
        except Exception:
            _logger.exception("on_resume callback error")

    async def _invoke_kill(self, reason: str) -> None:
        if self._on_kill is None:
            return
        try:
            r = self._on_kill(reason)
            if asyncio.iscoroutine(r):
                await r
        except Exception:
            _logger.exception("on_kill callback error")


def _chat_id_of(update: Any) -> str | int | None:
    """Pura chat_id Update-objektista — None jos puuttuu."""
    if update is None:
        return None
    chat = getattr(update, "effective_chat", None)
    if chat is None:
        return None
    return getattr(chat, "id", None)
