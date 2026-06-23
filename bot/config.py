from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass
class BybitConfig:
    api_key: str
    api_secret: str
    mode: str  # demo | mainnet | testnet
    recv_window: int


@dataclass
class TradingConfig:
    category: str
    settle_coin: str
    candle_interval: str
    delay_after_candle_seconds: int
    ma_fast: int
    ma_slow: int
    min_candles_for_ma: int
    long_min_change_pct: float
    short_max_change_pct: float
    leverage: int
    account_type: str


@dataclass
class TelegramConfig:
    bot_token: str
    allowed_user_id: int
    pnl_trigger: str
    proxy_host: str
    proxy_port: int
    proxy_username: str
    proxy_password: str

    def proxy_url(self) -> str | None:
        host = self.proxy_host.strip()
        if not host or self.proxy_port <= 0:
            return None
        if self.proxy_username.strip():
            user = quote(self.proxy_username.strip(), safe="")
            password = quote(self.proxy_password, safe="")
            return f"http://{user}:{password}@{host}:{self.proxy_port}"
        return f"http://{host}:{self.proxy_port}"


@dataclass
class SchedulerConfig:
    candle_close_hour_utc: int
    candle_close_minute_utc: int


@dataclass
class LoggingConfig:
    level: str


@dataclass
class AppConfig:
    bybit: BybitConfig
    trading: TradingConfig
    telegram: TelegramConfig
    scheduler: SchedulerConfig
    logging: LoggingConfig


def _require(section: dict[str, Any], key: str) -> Any:
    if key not in section:
        raise ValueError(f"Missing required config key: {key}")
    return section[key]


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            f"Copy config.yaml.example to config.yaml and fill in values."
        )

    with config_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    bybit_raw = raw.get("bybit", {})
    trading_raw = raw.get("trading", {})
    telegram_raw = raw.get("telegram", {})
    scheduler_raw = raw.get("scheduler", {})
    logging_raw = raw.get("logging", {})

    mode = str(_require(bybit_raw, "mode")).lower()
    if mode not in {"demo", "mainnet", "testnet"}:
        raise ValueError("bybit.mode must be one of: demo, mainnet, testnet")

    return AppConfig(
        bybit=BybitConfig(
            api_key=str(_require(bybit_raw, "api_key")),
            api_secret=str(_require(bybit_raw, "api_secret")),
            mode=mode,
            recv_window=int(bybit_raw.get("recv_window", 60000)),
        ),
        trading=TradingConfig(
            category=str(trading_raw.get("category", "linear")),
            settle_coin=str(trading_raw.get("settle_coin", "USDT")),
            candle_interval=str(trading_raw.get("candle_interval", "D")),
            delay_after_candle_seconds=int(
                trading_raw.get("delay_after_candle_seconds", 30)
            ),
            ma_fast=int(trading_raw.get("ma_fast", 50)),
            ma_slow=int(trading_raw.get("ma_slow", 200)),
            min_candles_for_ma=int(trading_raw.get("min_candles_for_ma", 200)),
            long_min_change_pct=float(trading_raw.get("long_min_change_pct", 4.0)),
            short_max_change_pct=float(trading_raw.get("short_max_change_pct", -4.0)),
            leverage=int(trading_raw.get("leverage", 1)),
            account_type=str(trading_raw.get("account_type", "UNIFIED")),
        ),
        telegram=TelegramConfig(
            bot_token=str(_require(telegram_raw, "bot_token")),
            allowed_user_id=int(_require(telegram_raw, "allowed_user_id")),
            pnl_trigger=str(telegram_raw.get("pnl_trigger", "/pnl")),
            proxy_host=str(telegram_raw.get("proxy_host", "")),
            proxy_port=int(telegram_raw.get("proxy_port", 0)),
            proxy_username=str(telegram_raw.get("proxy_username", "")),
            proxy_password=str(telegram_raw.get("proxy_password", "")),
        ),
        scheduler=SchedulerConfig(
            candle_close_hour_utc=int(scheduler_raw.get("candle_close_hour_utc", 0)),
            candle_close_minute_utc=int(scheduler_raw.get("candle_close_minute_utc", 0)),
        ),
        logging=LoggingConfig(level=str(logging_raw.get("level", "INFO"))),
    )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
