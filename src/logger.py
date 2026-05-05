"""Structured logging setup using structlog.

JSON-rivi per loki tiedostoon `logs/mm_bot.log`, ihmis-luettava muoto konsoliin.
Sensitiiviset kentät (private key, bot token, chat id) redactataan automaattisesti.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import structlog

REDACTED = "***REDACTED***"

# Substring-match: jos avaimen lower-case sisältää jonkin näistä, se redactataan.
_SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "private_key",
    "privatekey",
    "bot_token",
    "bottoken",
    "chat_id",
    "chatid",
    "api_key",
    "apikey",
    "secret",
    "password",
)


def _redact_sensitive(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Korvaa sensitiivisten avainten arvot REDACTED-merkkijonolla."""
    for key in list(event_dict.keys()):
        lower = key.lower()
        if any(fragment in lower for fragment in _SENSITIVE_KEY_FRAGMENTS):
            event_dict[key] = REDACTED
    return event_dict


def setup_logging(
    log_path: Path | str = "./logs/mm_bot.log",
    *,
    level: str = "INFO",
    json_console: bool = False,
) -> None:
    """Konfiguroi structlog: JSON tiedostoon, värillinen konsoliin.

    Args:
        log_path: tiedostoloki-polku (vanhemmaiskansio luodaan tarvittaessa).
        level: log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        json_console: jos True, myös konsoli on JSON (esim. systemd-ympäristöön).
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level_int = getattr(logging, level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(
        fmt="iso", utc=True, key="timestamp"
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        _redact_sensitive,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    json_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    console_processor: Any = (
        structlog.processors.JSONRenderer()
        if json_console
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=console_processor,
        foreign_pre_chain=shared_processors,
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(json_formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(level_int)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Hae structlog-logger valinnaisella nimellä (yleensä `__name__`)."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
