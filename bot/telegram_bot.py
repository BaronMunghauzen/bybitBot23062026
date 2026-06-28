from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import TYPE_CHECKING

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from bot.config import AppConfig
from bot.hypothetical_analysis import HypotheticalAnalyzer
from bot.trader import Trader

if TYPE_CHECKING:
    from bot.bybit_client import BybitClient, ClosedPosition

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE_LEN = 4000


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
        self.hypothetical = HypotheticalAnalyzer(client, config.trading)
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
        hypo = self.config.telegram.hypothetical_trigger
        await update.effective_message.reply_text(
            "Bybit futures bot is running.\n"
            f"Send `{trigger}` to get open positions PnL.\n"
            f"Send `{hypo}` for hypothetical long analysis.\n"
            f"Send `{hypo} 2026-06-24` for a specific entry day (UTC).",
            parse_mode="Markdown",
        )

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._reject_unauthorized(update)
            return
        await self._send_pnl(update)

    async def cmd_whatif(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._reject_unauthorized(update)
            return
        entry_date = None
        if context.args:
            entry_date = HypotheticalAnalyzer.parse_entry_date(context.args[0])
            if entry_date is None:
                if update.effective_message:
                    await update.effective_message.reply_text(
                        "Формат: /whatif или /whatif 2026-06-24 (также 24.06.2026). "
                        "Дата — день входа по UTC."
                    )
                return
        await self._send_hypothetical(update, entry_date)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        message = update.effective_message
        if message is None or message.text is None:
            return
        text = message.text.strip()
        if text.lower() == self.config.telegram.pnl_trigger.strip().lower():
            await self._send_pnl(update)
            return
        matched, entry_date, error = HypotheticalAnalyzer.parse_request(
            text,
            self.config.telegram.hypothetical_trigger,
        )
        if matched:
            if error and update.effective_message:
                await update.effective_message.reply_text(error)
                return
            await self._send_hypothetical(update, entry_date)

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
            for chunk in self._split_message(text):
                await update.effective_message.reply_text(chunk)

    async def _send_hypothetical(
        self,
        update: Update,
        entry_date: date | None = None,
    ) -> None:
        chat_id = self.config.telegram.allowed_user_id
        bot = await self._get_bot()
        if entry_date is not None:
            intro = (
                f"Считаю гипотетический анализ за {entry_date.isoformat()}, "
                "загружаю фьючерсы..."
            )
        else:
            intro = "Считаю гипотетический анализ, загружаю фьючерсы..."
        progress_message = await bot.send_message(chat_id=chat_id, text=intro)
        loop = asyncio.get_running_loop()
        last_progress_update = 0.0

        async def update_progress(checked: int, total: int) -> None:
            if entry_date is not None:
                header = f"Гипотетический анализ за {entry_date.isoformat()}"
            else:
                header = "Гипотетический анализ"
            progress_text = f"{header}\nПроверено {checked} из {total}"
            try:
                await bot.edit_message_text(
                    text=progress_text,
                    chat_id=chat_id,
                    message_id=progress_message.message_id,
                )
            except Exception:
                logger.debug("Skipped hypothetical progress update", exc_info=True)

        def on_progress(checked: int, total: int) -> None:
            nonlocal last_progress_update
            now = time.monotonic()
            if checked not in (0, total) and checked % 25 != 0:
                if now - last_progress_update < 2.0:
                    return
            last_progress_update = now
            asyncio.run_coroutine_threadsafe(
                update_progress(checked, total),
                loop,
            )

        result = None
        try:
            result = await asyncio.to_thread(
                self.hypothetical.run_long_analysis,
                on_progress,
                entry_date,
            )
            text = HypotheticalAnalyzer.format_message(
                result, self.config.trading.settle_coin
            )
        except Exception as exc:
            logger.exception("Failed to run hypothetical analysis")
            text = f"Ошибка гипотетического анализа: {exc}"

        if result is not None:
            try:
                await update_progress(result.scanned_symbols, result.total_symbols)
            except Exception:
                pass

        try:
            for chunk in self._split_message(text):
                await bot.send_message(chat_id=chat_id, text=chunk)
        except Exception:
            logger.exception("Failed to send hypothetical analysis to Telegram")
            if update.effective_message:
                await update.effective_message.reply_text(
                    "Не удалось отправить результат в Telegram. Смотрите логи."
                )

    async def _get_bot(self) -> Bot:
        if self.app is not None:
            return self.app.bot
        if self._standalone_bot is None:
            self._standalone_bot = Bot(
                self.config.telegram.bot_token,
                request=self._http_request,
            )
        return self._standalone_bot

    @staticmethod
    def _split_message(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LEN) -> list[str]:
        if len(text) <= max_len:
            return [text]

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for line in text.split("\n"):
            line_len = len(line) + 1
            if current and current_len + line_len > max_len:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_len
            else:
                current.append(line)
                current_len += line_len

        if current:
            chunks.append("\n".join(current))
        return chunks

    async def send_message(self, text: str) -> None:
        try:
            bot = await self._get_bot()
            chunks = self._split_message(text)
            for index, chunk in enumerate(chunks, start=1):
                if len(chunks) > 1:
                    prefix = f"[{index}/{len(chunks)}]\n"
                    payload = prefix + chunk if len(prefix) + len(chunk) <= TELEGRAM_MAX_MESSAGE_LEN else chunk
                else:
                    payload = chunk
                await bot.send_message(
                    chat_id=self.config.telegram.allowed_user_id,
                    text=payload,
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
        hypo_command = self.config.telegram.hypothetical_trigger.strip().lstrip("/")
        if hypo_command and hypo_command.isalnum():
            self.app.add_handler(CommandHandler(hypo_command, self.cmd_whatif))
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
