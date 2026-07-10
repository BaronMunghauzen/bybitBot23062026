from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from bot.bybit_client import BybitClient
from bot.config import TradingConfig
from bot.indicators import parse_klines
from bot.strategy import Signal, StrategyEngine

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]


@dataclass
class HypotheticalSymbolResult:
    symbol: str
    side: str
    change_pct_24h: float
    ma_fast: float
    ma_slow: float
    entry_open: float
    exit_price: float
    move_since_open_pct: float


@dataclass
class HypotheticalAnalysisResult:
    long_threshold_pct: float
    short_threshold_pct: float
    ma_fast: int
    ma_slow: int
    signal_avg_candles: int
    long_symbols: list[HypotheticalSymbolResult]
    short_symbols: list[HypotheticalSymbolResult]
    scanned_symbols: int
    total_symbols: int
    elapsed_seconds: float
    entry_date: date | None = None

    @staticmethod
    def _average_move(symbols: list[HypotheticalSymbolResult]) -> float:
        if not symbols:
            return 0.0
        return sum(s.move_since_open_pct for s in symbols) / len(symbols)

    @property
    def average_long_move_pct(self) -> float:
        return self._average_move(self.long_symbols)

    @property
    def average_short_move_pct(self) -> float:
        return self._average_move(self.short_symbols)


class HypotheticalAnalyzer:
    """On-demand analysis: does not affect the scheduled trading cycle."""

    _DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y")

    def __init__(self, client: BybitClient, trading_cfg: TradingConfig) -> None:
        self.client = client
        self.cfg = trading_cfg
        self.strategy = StrategyEngine(client, trading_cfg)

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

        logger.info(
            "Hypothetical live analysis started: %s futures "
            "(same long/short rules as main cycle)",
            total,
        )
        if on_progress is not None:
            on_progress(0, total)

        long_results: list[HypotheticalSymbolResult] = []
        short_results: list[HypotheticalSymbolResult] = []
        for index, ticker in enumerate(tickers, start=1):
            if index % 50 == 0 or index == total:
                logger.info(
                    "Hypothetical analysis progress: %s/%s",
                    index,
                    total,
                )
            try:
                if ticker.change_pct_24h > self.cfg.long_min_change_pct:
                    signal = self.strategy._evaluate_long(
                        ticker.symbol,
                        ticker.change_pct_24h,
                        ticker.last_price,
                    )
                    if signal is not None:
                        item = self._build_live_result(signal, prices)
                        if item:
                            short_results.append(item)

                if ticker.change_pct_24h < self.cfg.short_max_change_pct:
                    signal = self.strategy._evaluate_short(
                        ticker.symbol,
                        ticker.change_pct_24h,
                        ticker.last_price,
                    )
                    if signal is not None:
                        item = self._build_live_result(signal, prices)
                        if item:
                            long_results.append(item)
            except Exception:
                logger.exception("Hypothetical analysis failed for %s", ticker.symbol)
            finally:
                if on_progress is not None:
                    on_progress(index, total)

        long_results.sort(key=lambda item: item.change_pct_24h, reverse=True)
        short_results.sort(key=lambda item: item.change_pct_24h)
        elapsed = time.monotonic() - started
        logger.info(
            "Hypothetical live analysis finished: %s long, %s short in %.1fs",
            len(long_results),
            len(short_results),
            elapsed,
        )
        return self._build_result(
            long_results, short_results, total, elapsed, entry_date=None
        )

    def _run_historical_analysis(
        self,
        entry_date: date,
        on_progress: ProgressCallback | None = None,
    ) -> HypotheticalAnalysisResult:
        started = time.monotonic()
        tickers = self.client.get_linear_tickers()
        total = len(tickers)

        logger.info(
            "Hypothetical historical analysis for entry %s: %s futures",
            entry_date.isoformat(),
            total,
        )
        if on_progress is not None:
            on_progress(0, total)

        long_results: list[HypotheticalSymbolResult] = []
        short_results: list[HypotheticalSymbolResult] = []
        for index, ticker in enumerate(tickers, start=1):
            if index % 50 == 0 or index == total:
                logger.info(
                    "Hypothetical historical progress: %s/%s",
                    index,
                    total,
                )
            try:
                short_signal = self.strategy.evaluate_long_at_entry(
                    ticker.symbol,
                    entry_date,
                )
                if short_signal is not None:
                    item = self._build_historical_result(short_signal, entry_date)
                    if item:
                        short_results.append(item)

                long_signal = self.strategy.evaluate_short_at_entry(
                    ticker.symbol,
                    entry_date,
                )
                if long_signal is not None:
                    item = self._build_historical_result(long_signal, entry_date)
                    if item:
                        long_results.append(item)
            except Exception:
                logger.exception(
                    "Hypothetical historical analysis failed for %s", ticker.symbol
                )
            finally:
                if on_progress is not None:
                    on_progress(index, total)

        long_results.sort(key=lambda item: item.change_pct_24h, reverse=True)
        short_results.sort(key=lambda item: item.change_pct_24h)
        elapsed = time.monotonic() - started
        logger.info(
            "Hypothetical historical analysis finished for %s: %s long, %s short",
            entry_date.isoformat(),
            len(long_results),
            len(short_results),
        )
        return self._build_result(
            long_results, short_results, total, elapsed, entry_date=entry_date
        )

    def _build_live_result(
        self,
        signal: Signal,
        prices: dict[str, float],
    ) -> HypotheticalSymbolResult | None:
        raw = self.client.get_klines(signal.symbol, limit=1)
        candles = parse_klines(raw)
        if not candles:
            return None

        entry_candle = candles[-1]
        if entry_candle.open <= 0:
            return None

        exit_price = prices.get(signal.symbol, 0.0)
        if exit_price <= 0:
            exit_price = entry_candle.close

        return HypotheticalSymbolResult(
            symbol=signal.symbol,
            side=signal.side,
            change_pct_24h=signal.change_pct_24h,
            ma_fast=signal.ma_fast,
            ma_slow=signal.ma_slow,
            entry_open=entry_candle.open,
            exit_price=exit_price,
            move_since_open_pct=_move_since_open(
                signal.side, entry_candle.open, exit_price
            ),
        )

    def _build_historical_result(
        self,
        signal: Signal,
        entry_date: date,
    ) -> HypotheticalSymbolResult | None:
        start_ms = _day_start_ms(entry_date)
        end_ms = _day_start_ms(entry_date + timedelta(days=1)) - 1
        raw = self.client.get_klines(
            signal.symbol,
            limit=1,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        candles = parse_klines(raw)
        if not candles:
            return None

        entry_candle = candles[-1]
        if _candle_day(entry_candle) != entry_date or entry_candle.open <= 0:
            return None

        return HypotheticalSymbolResult(
            symbol=signal.symbol,
            side=signal.side,
            change_pct_24h=signal.change_pct_24h,
            ma_fast=signal.ma_fast,
            ma_slow=signal.ma_slow,
            entry_open=entry_candle.open,
            exit_price=entry_candle.close,
            move_since_open_pct=_move_since_open(
                signal.side, entry_candle.open, entry_candle.close
            ),
        )

    def _build_result(
        self,
        long_symbols: list[HypotheticalSymbolResult],
        short_symbols: list[HypotheticalSymbolResult],
        total: int,
        elapsed: float,
        entry_date: date | None,
    ) -> HypotheticalAnalysisResult:
        return HypotheticalAnalysisResult(
            long_threshold_pct=self.cfg.long_min_change_pct,
            short_threshold_pct=self.cfg.short_max_change_pct,
            ma_fast=self.cfg.ma_fast,
            ma_slow=self.cfg.ma_slow,
            signal_avg_candles=self.cfg.signal_avg_candles,
            long_symbols=long_symbols,
            short_symbols=short_symbols,
            scanned_symbols=total,
            total_symbols=total,
            elapsed_seconds=elapsed,
            entry_date=entry_date,
        )

    @staticmethod
    def format_message(result: HypotheticalAnalysisResult, settle_coin: str) -> str:
        lines = [
            "📈 Гипотетический анализ (long + short)",
            "",
            "Отбор как в основном алгоритме:",
            f"Short (шаг 4): 24h > {result.long_threshold_pct:.2f}%, "
            f"MA{result.ma_fast} > MA{result.ma_slow}, +/− свечи, "
            f"open < MA{result.ma_fast} < close",
            f"Long (шаг 6): 24h < {result.short_threshold_pct:.2f}%, "
            f"MA{result.ma_fast} < MA{result.ma_slow}, −/+ свечи, "
            f"close < MA{result.ma_fast} < open",
            "",
        ]

        if result.entry_date is not None:
            lines.extend(
                [
                    f"Дата входа: {result.entry_date.isoformat()}",
                    f"Прибыль: от open до close 1D свечи {result.entry_date.isoformat()}",
                ]
            )
        else:
            lines.append(
                "Прибыль: от open текущей 1D свечи до текущей цены"
            )

        lines.extend(
            [
                f"Проверено: {result.scanned_symbols} из {result.total_symbols} "
                f"({result.elapsed_seconds:.0f} сек)",
                "",
            ]
        )

        price_label = "close" if result.entry_date is not None else "now"
        lines.extend(
            HypotheticalAnalyzer._format_side_section(
                title="Long",
                symbols=result.long_symbols,
                ma_fast=result.ma_fast,
                ma_slow=result.ma_slow,
                price_label=price_label,
                settle_coin=settle_coin,
                average_move=result.average_long_move_pct,
            )
        )
        lines.append("")
        lines.extend(
            HypotheticalAnalyzer._format_side_section(
                title="Short",
                symbols=result.short_symbols,
                ma_fast=result.ma_fast,
                ma_slow=result.ma_slow,
                price_label=price_label,
                settle_coin=settle_coin,
                average_move=result.average_short_move_pct,
            )
        )

        total_signals = len(result.long_symbols) + len(result.short_symbols)
        if total_signals == 0:
            lines.append("Сигналов по правилам стратегии не найдено.")
        else:
            combined = (
                sum(s.move_since_open_pct for s in result.long_symbols)
                + sum(s.move_since_open_pct for s in result.short_symbols)
            ) / total_signals
            lines.append("")
            lines.append(f"Всего сигналов: {total_signals}")
            lines.append(f"Средняя прибыль (long + short): {combined:+.2f}%")

        return "\n".join(lines)

    @staticmethod
    def _format_side_section(
        title: str,
        symbols: list[HypotheticalSymbolResult],
        ma_fast: int,
        ma_slow: int,
        price_label: str,
        settle_coin: str,
        average_move: float,
    ) -> list[str]:
        lines = [f"=== {title} ==="]
        if not symbols:
            lines.append("нет сигналов")
            return lines

        for item in symbols:
            lines.append(
                f"{item.symbol}: 24h {item.change_pct_24h:+.2f}%, "
                f"с open {item.move_since_open_pct:+.2f}%"
            )
            lines.append(
                f"  MA{ma_fast}={item.ma_fast:.6f} MA{ma_slow}={item.ma_slow:.6f}"
            )
            lines.append(
                f"  open={item.entry_open:.6f} "
                f"{price_label}={item.exit_price:.6f} {settle_coin}"
            )

        lines.append(f"Найдено: {len(symbols)}")
        lines.append(f"Средняя прибыль: {average_move:+.2f}%")
        return lines


def _move_since_open(side: str, entry_open: float, exit_price: float) -> float:
    if side == "Buy":
        return (exit_price - entry_open) / entry_open * 100.0
    return (entry_open - exit_price) / entry_open * 100.0


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _day_start_ms(day: date) -> int:
    return int(
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000
    )


def _candle_day(candle) -> date:
    return datetime.fromtimestamp(
        candle.start_time_ms / 1000, tz=timezone.utc
    ).date()
