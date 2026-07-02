from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import AppConfig
from bot.telegram_bot import TelegramService
from bot.trader import Trader

logger = logging.getLogger(__name__)


class DailyScheduler:
    def __init__(
        self,
        config: AppConfig,
        trader: Trader,
        telegram: TelegramService,
    ) -> None:
        self.config = config
        self.trader = trader
        self.telegram = telegram
        self.scheduler = AsyncIOScheduler(timezone=timezone.utc)

    def _run_time(self) -> tuple[int, int, int]:
        sched = self.config.scheduler
        delay = self.config.trading.delay_after_candle_seconds
        base = datetime(
            2000,
            1,
            1,
            sched.candle_close_hour_utc,
            sched.candle_close_minute_utc,
            0,
            tzinfo=timezone.utc,
        )
        run_at = base + timedelta(seconds=delay)
        return run_at.hour, run_at.minute, run_at.second

    async def run_trading_cycle(self):
        logger.info("Daily trading cycle started")
        try:
            closed, balance = await asyncio.to_thread(
                self.trader.close_positions_and_get_balance
            )
            await self.telegram.notify_balance(balance, closed)

            result = await asyncio.to_thread(
                self.trader.scan_and_execute, balance, closed
            )
            await self.telegram.notify_cycle_result(result)
            logger.info("Daily trading cycle finished")
            return result, closed
        except Exception:
            logger.exception("Daily trading cycle failed")
            await self.telegram.send_message(
                "❌ Ошибка при выполнении торгового цикла. Смотрите логи сервера."
            )
            raise

    async def run_take_profit_check(self) -> None:
        cfg = self.config.trading
        if not cfg.take_profit_enabled:
            return

        try:
            result = await asyncio.to_thread(self.trader.run_take_profit_check)
            if not result.triggered:
                return

            await self.telegram.notify_take_profit(result)
            logger.info(
                "Take profit check closed %s position record(s)",
                len(result.closed_positions),
            )
        except Exception:
            logger.exception("Take profit check failed")
            await self.telegram.send_message(
                "❌ Ошибка проверки take profit. Смотрите логи сервера."
            )

    def start(self) -> None:
        sched = self.config.scheduler
        delay = self.config.trading.delay_after_candle_seconds
        hour, minute, second = self._run_time()
        self.scheduler.add_job(
            self.run_trading_cycle,
            CronTrigger(
                hour=hour,
                minute=minute,
                second=second,
                timezone=timezone.utc,
            ),
            id="daily_trading_cycle",
            replace_existing=True,
            misfire_grace_time=300,
        )

        tp_cfg = self.config.trading
        if tp_cfg.take_profit_enabled:
            self.scheduler.add_job(
                self.run_take_profit_check,
                IntervalTrigger(
                    minutes=tp_cfg.take_profit_check_interval_minutes,
                    timezone=timezone.utc,
                ),
                id="take_profit_monitor",
                replace_existing=True,
                misfire_grace_time=60,
            )

        self.scheduler.start()
        close_utc = (
            f"{sched.candle_close_hour_utc:02d}:"
            f"{sched.candle_close_minute_utc:02d} UTC"
        )
        run_utc = f"{hour:02d}:{minute:02d}:{second:02d} UTC"
        # MSK = UTC+3 year-round (no DST)
        msk_offset = timedelta(hours=3)
        close_msk = (
            datetime(2000, 1, 1, sched.candle_close_hour_utc, sched.candle_close_minute_utc)
            + msk_offset
        ).strftime("%H:%M")
        run_msk = (
            datetime(2000, 1, 1, hour, minute, second) + msk_offset
        ).strftime("%H:%M:%S")
        logger.info(
            "Scheduler started: Bybit 1D candle closes at %s (%s MSK), "
            "bot runs %s (%s MSK), delay=%ss",
            close_utc,
            close_msk,
            run_utc,
            run_msk,
            delay,
        )
        if tp_cfg.take_profit_enabled:
            logger.info(
                "Take profit monitor: every %s min, target sum(uPnL)/sum(position_value) "
                ">= %.2f%% (take_profit_pct, ≈ %.2f%% on margin at x%s)",
                tp_cfg.take_profit_check_interval_minutes,
                tp_cfg.take_profit_pct,
                tp_cfg.take_profit_pct * tp_cfg.leverage,
                tp_cfg.leverage,
            )

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def run_once_now(self):
        return await self.run_trading_cycle()
