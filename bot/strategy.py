from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.bybit_client import BybitClient, SymbolTicker
from bot.config import TradingConfig
from bot.indicators import (
    Candle,
    daily_change_pct,
    is_negative,
    is_positive,
    parse_klines,
    sma,
)

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    side: str  # Buy (long) or Sell (short)
    change_pct_24h: float
    ma_fast: float
    ma_slow: float


class StrategyEngine:
    def __init__(self, client: BybitClient, trading_cfg: TradingConfig) -> None:
        self.client = client
        self.cfg = trading_cfg

    def _load_closed_candles(self, symbol: str) -> list[Candle] | None:
        limit = max(self.cfg.min_candles_for_ma, self.cfg.ma_slow) + 5
        raw = self.client.get_klines(symbol, limit=limit)
        candles = parse_klines(raw)

        if len(candles) < 2:
            return None

        # Drop the currently forming daily candle; use only closed bars.
        if len(candles) > 1:
            candles = candles[:-1]

        if len(candles) < self.cfg.min_candles_for_ma:
            logger.debug(
                "%s: not enough candles (%s < %s)",
                symbol,
                len(candles),
                self.cfg.min_candles_for_ma,
            )
            return None

        return candles

    def _evaluate_long(self, symbol: str, change_pct: float) -> Signal | None:
        candles = self._load_closed_candles(symbol)
        if candles is None:
            return None

        closes = [c.close for c in candles]
        ma_fast = sma(closes, self.cfg.ma_fast)
        ma_slow = sma(closes, self.cfg.ma_slow)
        if ma_fast is None or ma_slow is None:
            return None

        if ma_fast <= ma_slow:
            return None

        last = candles[-1]
        prev = candles[-2]
        if not is_positive(last) or not is_negative(prev):
            return None

        if not (last.open > ma_fast or last.close > ma_fast):
            return None

        logger.info(
            "Long signal: %s change=%.2f%% MA%d=%.6f MA%d=%.6f",
            symbol,
            change_pct,
            self.cfg.ma_fast,
            ma_fast,
            self.cfg.ma_slow,
            ma_slow,
        )
        return Signal(
            symbol=symbol,
            side="Buy",
            change_pct_24h=change_pct,
            ma_fast=ma_fast,
            ma_slow=ma_slow,
        )

    def _evaluate_short(self, symbol: str, change_pct: float) -> Signal | None:
        candles = self._load_closed_candles(symbol)
        if candles is None:
            return None

        closes = [c.close for c in candles]
        ma_fast = sma(closes, self.cfg.ma_fast)
        ma_slow = sma(closes, self.cfg.ma_slow)
        if ma_fast is None or ma_slow is None:
            return None

        if ma_fast >= ma_slow:
            return None

        last = candles[-1]
        prev = candles[-2]
        if not is_negative(last) or not is_positive(prev):
            return None

        if not (last.open < ma_fast or last.close < ma_fast):
            return None

        logger.info(
            "Short signal: %s change=%.2f%% MA%d=%.6f MA%d=%.6f",
            symbol,
            change_pct,
            self.cfg.ma_fast,
            ma_fast,
            self.cfg.ma_slow,
            ma_slow,
        )
        return Signal(
            symbol=symbol,
            side="Sell",
            change_pct_24h=change_pct,
            ma_fast=ma_fast,
            ma_slow=ma_slow,
        )

    def scan_long_candidates(self, tickers: list[SymbolTicker]) -> list[Signal]:
        filtered = [
            t
            for t in tickers
            if t.change_pct_24h > self.cfg.long_min_change_pct
        ]
        filtered.sort(key=lambda t: t.change_pct_24h, reverse=True)

        signals: list[Signal] = []
        for ticker in filtered:
            signal = self._evaluate_long(ticker.symbol, ticker.change_pct_24h)
            if signal:
                signals.append(signal)
        return signals

    def scan_short_candidates(self, tickers: list[SymbolTicker]) -> list[Signal]:
        filtered = [
            t
            for t in tickers
            if t.change_pct_24h < self.cfg.short_max_change_pct
        ]
        filtered.sort(key=lambda t: t.change_pct_24h)

        signals: list[Signal] = []
        for ticker in filtered:
            signal = self._evaluate_short(ticker.symbol, ticker.change_pct_24h)
            if signal:
                signals.append(signal)
        return signals

    @staticmethod
    def format_last_candle_change(candles: list[Candle]) -> float:
        if not candles:
            return 0.0
        return daily_change_pct(candles[-1])
