#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from bot.bybit_client import BybitClient
from bot.config import DEFAULT_CONFIG_PATH, load_config, setup_logging
from bot.scheduler import DailyScheduler
from bot.telegram_bot import TelegramService
from bot.trader import Trader

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bybit futures trading bot")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run trading cycle immediately (for testing) and exit",
    )
    return parser.parse_args()


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass


async def main_async() -> None:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config.logging.level)

    client = BybitClient.from_app_config(config)
    trader = Trader(config, client)
    telegram = TelegramService(config, client, trader)
    scheduler = DailyScheduler(config, trader, telegram)

    if args.run_once:
        logger.info("Running single trading cycle (--run-once)")
        outcome = await scheduler.run_once_now()
        if outcome is not None:
            result, closed = outcome
            print(trader.format_balance_message(
                result.available_balance,
                config.trading.settle_coin,
                config.bybit.mode,
                closed,
            ))
            print()
            print(trader.format_cycle_result_message(result, config))
        return

    scheduler.start()

    try:
        await telegram.run_polling()
    finally:
        scheduler.shutdown()


def main() -> None:
    _configure_stdout()
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
