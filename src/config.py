"""Configuration loading and validation.

Lukee `config.yaml`-tiedoston, soveltaa `.env`- ja prosessiympäristömuuttujien
overridet (env > .env > yaml > defaults), ja validoi Pydantic v2:lla.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_DOTENV_PATH = Path(".env")

_SECTION_NAMES = frozenset(
    {"hyperliquid", "trading", "risk", "telegram", "storage", "operations"}
)

# Tunnetut nimetyt env-muuttujat → (section, field). Salaisuudet ja master-osoite.
_ENV_ALIASES: dict[str, tuple[str, str]] = {
    "HL_API_WALLET_ADDRESS": ("hyperliquid", "api_wallet_address"),
    "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
    "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
}

_logger = logging.getLogger(__name__)


class HyperliquidConfig(BaseModel):
    network: Literal["testnet", "mainnet"] = "testnet"
    api_wallet_address: str = ""

    @field_validator("api_wallet_address")
    @classmethod
    def _check_address(cls, v: str) -> str:
        if v and not v.startswith("0x"):
            raise ValueError("api_wallet_address must start with 0x")
        return v


class TradingConfig(BaseModel):
    symbol: str = "ETH"
    capital_usdc: Decimal = Field(default=Decimal("500"), gt=0)
    max_position_size: Decimal = Field(default=Decimal("0.1"), gt=0)
    spread_bps: float = Field(default=5.0, gt=0, lt=100)
    num_levels: int = Field(default=5, gt=0, le=20)
    level_spacing_bps: float = Field(default=3.0, gt=0)
    order_size: Decimal = Field(default=Decimal("0.02"), gt=0)
    skew_factor: float = Field(default=0.5, ge=0, le=1)
    quote_refresh_ms: int = Field(default=2000, gt=0)
    max_order_age_seconds: int = Field(default=30, gt=0)
    max_spread_bps: float = Field(default=50.0, gt=0)


class RiskConfig(BaseModel):
    max_loss_pct: float = Field(default=10.0, gt=0, le=100)
    daily_max_loss_pct: float = Field(default=20.0, gt=0, le=100)
    max_vol_pct_1min: float = Field(default=2.0, gt=0)
    inventory_hard_stop_multiplier: float = Field(default=1.2, gt=1)
    max_api_errors_per_minute: int = Field(default=5, gt=0)
    funding_rate_threshold_8h: float = Field(default=0.01, ge=0)


class TelegramConfig(BaseModel):
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    notification_rate_limit_seconds: int = Field(default=10, gt=0)
    bot_token: SecretStr | None = None
    chat_id: str | None = None


class StorageConfig(BaseModel):
    db_path: Path = Path("./data/mm_bot.db")
    log_path: Path = Path("./logs/mm_bot.log")
    metrics_interval_seconds: int = Field(default=10, gt=0)


class OperationsConfig(BaseModel):
    reconnect_initial_backoff_ms: int = Field(default=1000, gt=0)
    reconnect_max_backoff_ms: int = Field(default=60000, gt=0)
    websocket_heartbeat_seconds: int = Field(default=5, gt=0)
    inventory_reconcile_seconds: int = Field(default=30, gt=0)
    dry_run: bool = False


class BotConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_file=None)

    hyperliquid: HyperliquidConfig = Field(default_factory=HyperliquidConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    operations: OperationsConfig = Field(default_factory=OperationsConfig)


def _read_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env-parser: KEY=VALUE per line, # comments + blank lines ignored."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value:
            env[key] = value
    return env


def _apply_env_overrides(raw: dict[str, Any], env: dict[str, str]) -> None:
    """Mutate `raw` in place applying env-pohjaiset overridet."""
    for env_key, (section, field) in _ENV_ALIASES.items():
        value = env.get(env_key)
        if value:
            raw.setdefault(section, {})[field] = value

    # Geneerinen SECTION__FIELD-pattern (esim. TRADING__SPREAD_BPS=10).
    for key, value in env.items():
        if "__" not in key or not value:
            continue
        section, _, field = key.partition("__")
        section = section.lower()
        field = field.lower()
        if section in _SECTION_NAMES:
            raw.setdefault(section, {})[field] = value


def load_config(
    yaml_path: Path | str = DEFAULT_CONFIG_PATH,
    *,
    env: dict[str, str] | None = None,
    dotenv_path: Path | str | None = None,
) -> BotConfig:
    """Lue YAML, sovella .env + os.environ overridet, validoi.

    Prioriteetti (korkein ensin): os.environ > .env > YAML > defaults.

    Args:
        yaml_path: polku config-tiedostoon. Jos puuttuu, käytetään defaultteja.
        env: ympäristömuuttujadiktaari (default: os.environ).
        dotenv_path: polku .env-tiedostoon (default: ./.env).
    """
    yaml_path = Path(yaml_path)
    raw: dict[str, Any] = {}
    if yaml_path.exists():
        with yaml_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{yaml_path} must be a YAML mapping at top level")
        raw = data

    dotenv_file = Path(dotenv_path) if dotenv_path is not None else DEFAULT_DOTENV_PATH
    if dotenv_file.exists():
        _apply_env_overrides(raw, _read_dotenv(dotenv_file))

    process_env = dict(os.environ) if env is None else env
    _apply_env_overrides(raw, process_env)

    config = BotConfig.model_validate(raw)

    if config.hyperliquid.network == "mainnet":
        _logger.warning(
            "Hyperliquid network is set to MAINNET — real funds at risk. "
            "Verify capital, position limits, and dry_run settings before running."
        )

    return config


@lru_cache(maxsize=1)
def get_config() -> BotConfig:
    """Singleton BotConfig oletuspoluilta. Käytä tätä tuotantokoodissa."""
    return load_config()


def reset_config_cache() -> None:
    """Test-helper: tyhjennä `get_config()`-cache."""
    get_config.cache_clear()
