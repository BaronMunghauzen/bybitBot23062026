from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from bot.config import AppConfig
from bot.trader import Trader

if TYPE_CHECKING:
    from bot.bybit_client import BybitClient, ClosedPosition

logger = logging.getLogger(__name__)


class TelegramService:
    def __init__(
        self,
        config: AppConfig,
        client: BybitClient,
        trader: Trader,
    ) -> None:
        self.config = config
        self.client = client
        self.trader = trader
        self.app: Application | None = None
        self._standalone_bot: Bot | None = None
        self._http_request = self._build_http_request()

    def _build_http_request(self) -> HTTPXRequest:
        proxy = self.config.telegram.proxy_url()
        if proxy:
            logger.info(
                "Telegram HTTP proxy enabled: %s:%s",
                self.config.telegram.proxy_host,
                self.config.telegram.proxy_port,
            )
        return HTTPXRequest(
            proxy=proxy,
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0,
        )

    def _is_allowed(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        return user.id == self.config.telegram.allowed_user_id

    async def _reject_unauthorized(self, update: Update) -> None:
        if update.effective_message:
            await update.effective_message.reply_text("Access denied.")

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._reject_unauthorized(update)
            return
        trigger = self.config.telegram.pnl_trigger
        await update.effective_message.reply_text(
            "Bybit futures bot is running.\n"
            f"Send `{trigger}` to get open positions PnL.",
            parse_mode="Markdown",
        )

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._reject_unauthorized(update)
            return
        await self._send_pnl(update)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        message = update.effective_message
        if message is None or message.text is None:
            return
        trigger = self.config.telegram.pnl_trigger.strip()
        if message.text.strip().lower() == trigger.lower():
            await self._send_pnl(update)

    async def _send_pnl(self, update: Update) -> None:
        try:
            positions = await asyncio.to_thread(self.client.get_open_positions)
            balance = await asyncio.to_thread(self.client.get_available_balance)
            text = self.trader.format_pnl_message(
                positions,
                self.config.trading.settle_coin,
                balance,
            )
        except Exception as exc:
            logger.exception("Failed to fetch PnL")
            text = f"Ошибка получения PnL: {exc}"

        if update.effective_message:
            await update.effective_message.reply_text(text)

    async def _get_bot(self) -> Bot:
        if self.app is not None:
            return self.app.bot
        if self._standalone_bot is None:
            self._standalone_bot = Bot(
                self.config.telegram.bot_token,
                request=self._http_request,
            )
        return self._standalone_bot

    async def send_message(self, text: str) -> None:
        try:
            bot = await self._get_bot()
            await bot.send_message(
                chat_id=self.config.telegram.allowed_user_id,
                text=text,
            )
        except Exception:
            logger.exception("Failed to send Telegram message")

    async def notify_balance(
        self,
        balance: float,
        closed_positions: list[ClosedPosition] | None = None,
    ) -> None:
        text = self.trader.format_balance_message(
            balance,
            self.config.trading.settle_coin,
            self.config.bybit.mode,
            closed_positions,
        )
        await self.send_message(text)

    async def notify_cycle_result(self, result) -> None:
        text = self.trader.format_cycle_result_message(result, self.config)
        await self.send_message(text)

    def build_application(self) -> Application:
        self.app = (
            Application.builder()
            .token(self.config.telegram.bot_token)
            .request(self._http_request)
            .get_updates_request(self._http_request)
            .build()
        )
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("pnl", self.cmd_pnl))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message)
        )
        return self.app

    async def run_polling(self) -> None:
        app = self.build_application()
        logger.info("Starting Telegram polling")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
