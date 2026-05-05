"""Tests for src.config."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from src.config import BotConfig, load_config

VALID_YAML = dedent(
    """
    hyperliquid:
      network: testnet
      api_wallet_address: "0x1234567890abcdef1234567890abcdef12345678"
    trading:
      symbol: ETH
      capital_usdc: 500
      max_position_size: 0.1
      spread_bps: 5
      num_levels: 5
      level_spacing_bps: 3
      order_size: 0.02
      skew_factor: 0.5
      quote_refresh_ms: 2000
      max_order_age_seconds: 30
      max_spread_bps: 50
    risk:
      max_loss_pct: 10
      daily_max_loss_pct: 20
      max_vol_pct_1min: 2.0
      inventory_hard_stop_multiplier: 1.2
      max_api_errors_per_minute: 5
      funding_rate_threshold_8h: 0.01
    telegram:
      bot_token_env: TELEGRAM_BOT_TOKEN
      chat_id_env: TELEGRAM_CHAT_ID
      notification_rate_limit_seconds: 10
    storage:
      db_path: ./data/mm_bot.db
      log_path: ./logs/mm_bot.log
      metrics_interval_seconds: 10
    operations:
      reconnect_initial_backoff_ms: 1000
      reconnect_max_backoff_ms: 60000
      websocket_heartbeat_seconds: 5
      inventory_reconcile_seconds: 30
      dry_run: false
    """
).strip()


@pytest.fixture
def yaml_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(VALID_YAML, encoding="utf-8")
    return p


@pytest.fixture
def empty_dotenv(tmp_path: Path) -> Path:
    p = tmp_path / "empty.env"
    p.write_text("", encoding="utf-8")
    return p


def _write_yaml(tmp_path: Path, replace_from: str, replace_to: str) -> Path:
    yaml_text = VALID_YAML.replace(replace_from, replace_to)
    p = tmp_path / "config.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    return p


def test_loads_valid_yaml(yaml_file: Path, empty_dotenv: Path) -> None:
    cfg = load_config(yaml_file, env={}, dotenv_path=empty_dotenv)
    assert isinstance(cfg, BotConfig)
    assert cfg.hyperliquid.network == "testnet"
    assert cfg.hyperliquid.api_wallet_address.startswith("0x")
    assert cfg.trading.symbol == "ETH"
    assert cfg.trading.capital_usdc == Decimal("500")
    assert cfg.trading.max_position_size == Decimal("0.1")
    assert cfg.trading.spread_bps == 5
    assert cfg.trading.num_levels == 5
    assert cfg.trading.skew_factor == 0.5
    assert cfg.risk.max_loss_pct == 10
    assert cfg.operations.dry_run is False
    assert cfg.storage.metrics_interval_seconds == 10


def test_defaults_when_yaml_missing(tmp_path: Path) -> None:
    cfg = load_config(
        tmp_path / "missing.yaml", env={}, dotenv_path=tmp_path / "missing.env"
    )
    assert cfg.trading.symbol == "ETH"
    assert cfg.trading.spread_bps == 5.0
    assert cfg.hyperliquid.network == "testnet"


def test_negative_capital_rejected(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "capital_usdc: 500", "capital_usdc: -1")
    with pytest.raises(ValidationError):
        load_config(p, env={}, dotenv_path=tmp_path / "missing.env")


def test_zero_capital_rejected(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "capital_usdc: 500", "capital_usdc: 0")
    with pytest.raises(ValidationError):
        load_config(p, env={}, dotenv_path=tmp_path / "missing.env")


def test_spread_bps_too_large_rejected(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "spread_bps: 5", "spread_bps: 150")
    with pytest.raises(ValidationError):
        load_config(p, env={}, dotenv_path=tmp_path / "missing.env")


def test_spread_bps_zero_rejected(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "spread_bps: 5", "spread_bps: 0")
    with pytest.raises(ValidationError):
        load_config(p, env={}, dotenv_path=tmp_path / "missing.env")


def test_skew_factor_above_one_rejected(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "skew_factor: 0.5", "skew_factor: 1.5")
    with pytest.raises(ValidationError):
        load_config(p, env={}, dotenv_path=tmp_path / "missing.env")


def test_skew_factor_negative_rejected(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "skew_factor: 0.5", "skew_factor: -0.1")
    with pytest.raises(ValidationError):
        load_config(p, env={}, dotenv_path=tmp_path / "missing.env")


def test_num_levels_too_large_rejected(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "num_levels: 5", "num_levels: 21")
    with pytest.raises(ValidationError):
        load_config(p, env={}, dotenv_path=tmp_path / "missing.env")


def test_invalid_address_rejected(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        '"0x1234567890abcdef1234567890abcdef12345678"',
        '"not-an-address"',
    )
    with pytest.raises(ValidationError):
        load_config(p, env={}, dotenv_path=tmp_path / "missing.env")


def test_invalid_network_rejected(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "network: testnet", "network: betanet")
    with pytest.raises(ValidationError):
        load_config(p, env={}, dotenv_path=tmp_path / "missing.env")


def test_env_overrides_yaml_named_alias(yaml_file: Path, tmp_path: Path) -> None:
    new_address = "0xdeadbeef" + "ab" * 16
    env = {"HL_API_WALLET_ADDRESS": new_address}
    cfg = load_config(yaml_file, env=env, dotenv_path=tmp_path / "missing.env")
    assert cfg.hyperliquid.api_wallet_address == new_address


def test_env_overrides_yaml_nested_delimiter(yaml_file: Path, tmp_path: Path) -> None:
    env = {"TRADING__SPREAD_BPS": "10", "TRADING__NUM_LEVELS": "3"}
    cfg = load_config(yaml_file, env=env, dotenv_path=tmp_path / "missing.env")
    assert cfg.trading.spread_bps == 10.0
    assert cfg.trading.num_levels == 3


def test_telegram_secrets_from_env(yaml_file: Path, tmp_path: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "abc:def",
        "TELEGRAM_CHAT_ID": "12345",
    }
    cfg = load_config(yaml_file, env=env, dotenv_path=tmp_path / "missing.env")
    assert cfg.telegram.bot_token is not None
    assert cfg.telegram.bot_token.get_secret_value() == "abc:def"
    assert cfg.telegram.chat_id == "12345"


def test_dotenv_loaded(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    address = "0xaaa" + "0" * 37
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"HL_API_WALLET_ADDRESS={address}\n", encoding="utf-8")
    cfg = load_config(yaml_path, env={}, dotenv_path=dotenv)
    assert cfg.hyperliquid.api_wallet_address == address


def test_os_environ_overrides_dotenv(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    dotenv_address = "0xddd" + "0" * 37
    env_address = "0xeee" + "0" * 37
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"HL_API_WALLET_ADDRESS={dotenv_address}\n", encoding="utf-8")
    cfg = load_config(
        yaml_path, env={"HL_API_WALLET_ADDRESS": env_address}, dotenv_path=dotenv
    )
    assert cfg.hyperliquid.api_wallet_address == env_address


def test_dotenv_comments_and_blanks_ignored(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    address = "0xfff" + "0" * 37
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"# comment line\n\nHL_API_WALLET_ADDRESS={address}\n# trailing\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path, env={}, dotenv_path=dotenv)
    assert cfg.hyperliquid.api_wallet_address == address


def test_mainnet_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = _write_yaml(tmp_path, "network: testnet", "network: mainnet")
    import logging

    with caplog.at_level(logging.WARNING):
        cfg = load_config(p, env={}, dotenv_path=tmp_path / "missing.env")
    assert cfg.hyperliquid.network == "mainnet"
    assert any("MAINNET" in rec.message for rec in caplog.records)
