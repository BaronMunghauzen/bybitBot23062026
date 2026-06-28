from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from bot.bybit_client import BybitClient
from bot.config import TradingConfig
from bot.indicators import daily_change_pct, parse_klines

logger = logging.getLogger(__name__)


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
    elapsed_seconds: float

    @property
    def average_move_pct(self) -> float:
        if not self.symbols:
            return 0.0
        return sum(s.move_since_open_pct for s in self.symbols) / len(self.symbols)


class HypotheticalAnalyzer:
    """On-demand analysis: does not affect the scheduled trading cycle."""

    def __init__(self, client: BybitClient, trading_cfg: TradingConfig) -> None:
        self.client = client
        self.cfg = trading_cfg

    def run_long_analysis(self) -> HypotheticalAnalysisResult:
        started = time.monotonic()
        tickers = self.client.get_linear_tickers()
        prices = {t.symbol: t.last_price for t in tickers}

        # Pre-filter by ticker 24h change to avoid hundreds of kline API calls.
        prefilter_pct = max(2.0, self.cfg.long_min_change_pct - 0.5)
        candidates = [t for t in tickers if t.change_pct_24h > prefilter_pct]
        logger.info(
            "Hypothetical analysis started: %s symbols to scan (prefilter > %.2f%%, total %s)",
            len(candidates),
            prefilter_pct,
            len(tickers),
        )

        results: list[HypotheticalSymbolResult] = []
        for index, ticker in enumerate(candidates, start=1):
            if index % 10 == 0 or index == len(candidates):
                logger.info(
                    "Hypothetical analysis progress: %s/%s",
                    index,
                    len(candidates),
                )
            try:
                item = self._analyze_symbol(ticker.symbol, prices)
                if item:
                    results.append(item)
            except Exception:
                logger.exception("Hypothetical analysis failed for %s", ticker.symbol)

        results.sort(key=lambda item: item.last_closed_change_pct, reverse=True)
        elapsed = time.monotonic() - started
        logger.info(
            "Hypothetical analysis finished: %s matches in %.1fs",
            len(results),
            elapsed,
        )
        return HypotheticalAnalysisResult(
            threshold_pct=self.cfg.long_min_change_pct,
            symbols=results,
            scanned_symbols=len(candidates),
            elapsed_seconds=elapsed,
        )

    def _analyze_symbol(
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

    @staticmethod
    def format_message(result: HypotheticalAnalysisResult, settle_coin: str) -> str:
        lines = [
            "📈 Гипотетический анализ (long)",
            "",
            f"Фильтр: последняя закрытая 1D свеча > {result.threshold_pct:.2f}%",
            "Прибыль: от open текущей 1D свечи до текущей цены",
            f"Проверено символов: {result.scanned_symbols} "
            f"({result.elapsed_seconds:.0f} сек)",
            "",
        ]

        if not result.symbols:
            lines.append(
                f"Подходящих фьючерсов не найдено (порог {result.threshold_pct:.2f}%)."
            )
            return "\n".join(lines)

        for item in result.symbols:
            lines.append(
                f"{item.symbol}: "
                f"закрытая {item.last_closed_change_pct:+.2f}%, "
                f"с open {item.move_since_open_pct:+.2f}%"
            )
            lines.append(
                f"  open={item.current_candle_open:.6f} "
                f"now={item.current_price:.6f} {settle_coin}"
            )

        lines.append("")
        lines.append(f"Найдено: {len(result.symbols)}")
        lines.append(f"Средняя прибыль: {result.average_move_pct:+.2f}%")
        return "\n".join(lines)
