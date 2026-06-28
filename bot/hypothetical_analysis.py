from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from bot.bybit_client import BybitClient
from bot.config import TradingConfig
from bot.indicators import daily_change_pct, parse_klines

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]


@dataclass
class HypotheticalSymbolResult:
    symbol: str
    last_closed_change_pct: float
    current_candle_open: float
    current_price: float
    move_since_open_pct: float


@dataclass
class HypotheticalAnalysisResult:
    threshold_pct: float
    symbols: list[HypotheticalSymbolResult]
    scanned_symbols: int
    total_symbols: int
    elapsed_seconds: float
    entry_date: date | None = None

    @property
    def average_move_pct(self) -> float:
        if not self.symbols:
            return 0.0
        return sum(s.move_since_open_pct for s in self.symbols) / len(self.symbols)


class HypotheticalAnalyzer:
    """On-demand analysis: does not affect the scheduled trading cycle."""

    _DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y")

    def __init__(self, client: BybitClient, trading_cfg: TradingConfig) -> None:
        self.client = client
        self.cfg = trading_cfg

    @classmethod
    def parse_entry_date(cls, text: str) -> date | None:
        value = text.strip()
        if not value:
            return None
        for fmt in cls._DATE_FORMATS:
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        return None

    @classmethod
    def parse_request(
        cls, text: str, trigger: str
    ) -> tuple[bool, date | None, str | None]:
        normalized = text.strip()
        trigger_value = trigger.strip()
        if not normalized.lower().startswith(trigger_value.lower()):
            return False, None, None

        rest = normalized[len(trigger_value) :].strip()
        if not rest:
            return True, None, None

        entry_date = cls.parse_entry_date(rest)
        if entry_date is None:
            return (
                True,
                None,
                "Не удалось разобрать дату. Пример: /whatif 2026-06-24 или /whatif 24.06.2026",
            )
        return True, entry_date, None

    def run_long_analysis(
        self,
        on_progress: ProgressCallback | None = None,
        entry_date: date | None = None,
    ) -> HypotheticalAnalysisResult:
        if entry_date is not None:
            today = _utc_today()
            if entry_date > today:
                raise ValueError(
                    f"Дата {entry_date.isoformat()} в будущем. "
                    f"Сегодня по UTC: {today.isoformat()}."
                )
            if entry_date < today:
                return self._run_historical_analysis(entry_date, on_progress)

        return self._run_live_analysis(on_progress)

    def _run_live_analysis(
        self,
        on_progress: ProgressCallback | None = None,
    ) -> HypotheticalAnalysisResult:
        started = time.monotonic()
        tickers = self.client.get_linear_tickers()
        prices = {t.symbol: t.last_price for t in tickers}
        total = len(tickers)

        prefilter_pct = max(2.0, self.cfg.long_min_change_pct - 0.5)
        logger.info(
            "Hypothetical live analysis started: %s futures total "
            "(kline check if 24h > %.2f%%)",
            total,
            prefilter_pct,
        )
        if on_progress is not None:
            on_progress(0, total)

        results: list[HypotheticalSymbolResult] = []
        for index, ticker in enumerate(tickers, start=1):
            if index % 50 == 0 or index == total:
                logger.info(
                    "Hypothetical analysis progress: %s/%s",
                    index,
                    total,
                )
            try:
                if ticker.change_pct_24h <= prefilter_pct:
                    continue
                item = self._analyze_symbol_live(ticker.symbol, prices)
                if item:
                    results.append(item)
            except Exception:
                logger.exception("Hypothetical analysis failed for %s", ticker.symbol)
            finally:
                if on_progress is not None:
                    on_progress(index, total)

        results.sort(key=lambda item: item.last_closed_change_pct, reverse=True)
        elapsed = time.monotonic() - started
        logger.info(
            "Hypothetical live analysis finished: %s matches in %.1fs (%s futures checked)",
            len(results),
            elapsed,
            total,
        )
        return HypotheticalAnalysisResult(
            threshold_pct=self.cfg.long_min_change_pct,
            symbols=results,
            scanned_symbols=total,
            total_symbols=total,
            elapsed_seconds=elapsed,
            entry_date=None,
        )

    def _run_historical_analysis(
        self,
        entry_date: date,
        on_progress: ProgressCallback | None = None,
    ) -> HypotheticalAnalysisResult:
        started = time.monotonic()
        tickers = self.client.get_linear_tickers()
        total = len(tickers)
        trigger_day = entry_date - timedelta(days=1)
        logger.info(
            "Hypothetical historical analysis started for entry %s "
            "(trigger day %s, %s futures)",
            entry_date.isoformat(),
            trigger_day.isoformat(),
            total,
        )
        if on_progress is not None:
            on_progress(0, total)

        results: list[HypotheticalSymbolResult] = []
        for index, ticker in enumerate(tickers, start=1):
            if index % 50 == 0 or index == total:
                logger.info(
                    "Hypothetical historical progress: %s/%s",
                    index,
                    total,
                )
            try:
                item = self._analyze_symbol_historical(ticker.symbol, entry_date)
                if item:
                    results.append(item)
            except Exception:
                logger.exception(
                    "Hypothetical historical analysis failed for %s", ticker.symbol
                )
            finally:
                if on_progress is not None:
                    on_progress(index, total)

        results.sort(key=lambda item: item.last_closed_change_pct, reverse=True)
        elapsed = time.monotonic() - started
        logger.info(
            "Hypothetical historical analysis finished for %s: %s matches in %.1fs",
            entry_date.isoformat(),
            len(results),
            elapsed,
        )
        return HypotheticalAnalysisResult(
            threshold_pct=self.cfg.long_min_change_pct,
            symbols=results,
            scanned_symbols=total,
            total_symbols=total,
            elapsed_seconds=elapsed,
            entry_date=entry_date,
        )

    def _analyze_symbol_live(
        self, symbol: str, prices: dict[str, float]
    ) -> HypotheticalSymbolResult | None:
        raw = self.client.get_klines(symbol, limit=3)
        candles = parse_klines(raw)
        if len(candles) < 2:
            return None

        last_closed = candles[-2]
        current_candle = candles[-1]
        closed_change = daily_change_pct(last_closed)
        if closed_change <= self.cfg.long_min_change_pct:
            return None

        current_price = prices.get(symbol, 0.0)
        if current_price <= 0:
            current_price = current_candle.close
        if current_candle.open <= 0:
            return None

        move_since_open = (
            (current_price - current_candle.open) / current_candle.open * 100.0
        )
        return HypotheticalSymbolResult(
            symbol=symbol,
            last_closed_change_pct=closed_change,
            current_candle_open=current_candle.open,
            current_price=current_price,
            move_since_open_pct=move_since_open,
        )

    def _analyze_symbol_historical(
        self,
        symbol: str,
        entry_date: date,
    ) -> HypotheticalSymbolResult | None:
        trigger_day = entry_date - timedelta(days=1)
        start_ms = _day_start_ms(trigger_day)
        end_ms = _day_start_ms(entry_date + timedelta(days=1)) - 1
        raw = self.client.get_klines(
            symbol,
            limit=2,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        candles = parse_klines(raw)
        if len(candles) < 2:
            return None

        trigger_candle = candles[0]
        entry_candle = candles[1]
        if _candle_day(trigger_candle) != trigger_day:
            return None
        if _candle_day(entry_candle) != entry_date:
            return None

        closed_change = daily_change_pct(trigger_candle)
        if closed_change <= self.cfg.long_min_change_pct:
            return None
        if entry_candle.open <= 0:
            return None

        move_since_open = (
            (entry_candle.close - entry_candle.open) / entry_candle.open * 100.0
        )
        return HypotheticalSymbolResult(
            symbol=symbol,
            last_closed_change_pct=closed_change,
            current_candle_open=entry_candle.open,
            current_price=entry_candle.close,
            move_since_open_pct=move_since_open,
        )

    @staticmethod
    def format_message(result: HypotheticalAnalysisResult, settle_coin: str) -> str:
        lines = ["📈 Гипотетический анализ (long)", ""]

        if result.entry_date is not None:
            trigger_day = result.entry_date - timedelta(days=1)
            lines.extend(
                [
                    f"Дата входа: {result.entry_date.isoformat()}",
                    f"Фильтр: закрытая 1D свеча {trigger_day.isoformat()} "
                    f"> {result.threshold_pct:.2f}%",
                    f"Прибыль: от open до close 1D свечи {result.entry_date.isoformat()}",
                ]
            )
        else:
            lines.extend(
                [
                    f"Фильтр: последняя закрытая 1D свеча > {result.threshold_pct:.2f}%",
                    "Прибыль: от open текущей 1D свечи до текущей цены",
                ]
            )

        lines.extend(
            [
                f"Проверено: {result.scanned_symbols} из {result.total_symbols} "
                f"({result.elapsed_seconds:.0f} сек)",
                "",
            ]
        )

        if not result.symbols:
            lines.append(
                f"Подходящих фьючерсов не найдено (порог {result.threshold_pct:.2f}%)."
            )
            return "\n".join(lines)

        price_label = "close" if result.entry_date is not None else "now"
        for item in result.symbols:
            lines.append(
                f"{item.symbol}: "
                f"закрытая {item.last_closed_change_pct:+.2f}%, "
                f"с open {item.move_since_open_pct:+.2f}%"
            )
            lines.append(
                f"  open={item.current_candle_open:.6f} "
                f"{price_label}={item.current_price:.6f} {settle_coin}"
            )

        lines.append("")
        lines.append(f"Найдено: {len(result.symbols)}")
        lines.append(f"Средняя прибыль: {result.average_move_pct:+.2f}%")
        return "\n".join(lines)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _day_start_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def _candle_day(candle) -> date:
    return datetime.fromtimestamp(
        candle.start_time_ms / 1000, tz=timezone.utc
    ).date()
