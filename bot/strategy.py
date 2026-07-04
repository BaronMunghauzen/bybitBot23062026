from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

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

    def _min_closed_candles(self) -> int:
        return max(
            self.cfg.min_candles_for_ma,
            self.cfg.ma_slow,
            self.cfg.signal_avg_candles,
        )

    def _kline_limit(self) -> int:
        return self._min_closed_candles() + 5

    def _load_closed_candles(self, symbol: str) -> list[Candle] | None:
        raw = self.client.get_klines(symbol, limit=self._kline_limit())
        candles = parse_klines(raw)

        if len(candles) < 2:
            return None

        # Drop the currently forming daily candle; use only closed bars.
        if len(candles) > 1:
            candles = candles[:-1]

        if len(candles) < self._min_closed_candles():
            logger.debug(
                "%s: not enough candles (%s < %s)",
                symbol,
                len(candles),
                self._min_closed_candles(),
            )
            return None

        return candles

    def _load_closed_candles_before(
        self, symbol: str, before_day: date
    ) -> list[Candle] | None:
        end_ms = int(
            datetime(
                before_day.year,
                before_day.month,
                before_day.day,
                tzinfo=timezone.utc,
            ).timestamp()
            * 1000
        ) - 1
        raw = self.client.get_klines(
            symbol, limit=self._kline_limit(), end_ms=end_ms
        )
        candles = parse_klines(raw)

        if len(candles) < self._min_closed_candles():
            logger.debug(
                "%s: not enough historical candles before %s (%s < %s)",
                symbol,
                before_day.isoformat(),
                len(candles),
                self._min_closed_candles(),
            )
            return None

        return candles

    @staticmethod
    def _entry_day_open(symbol: str, client: BybitClient, entry_date: date) -> float | None:
        start_ms = int(
            datetime(
                entry_date.year,
                entry_date.month,
                entry_date.day,
                tzinfo=timezone.utc,
            ).timestamp()
            * 1000
        )
        end_ms = int(
            datetime(
                (entry_date + timedelta(days=1)).year,
                (entry_date + timedelta(days=1)).month,
                (entry_date + timedelta(days=1)).day,
                tzinfo=timezone.utc,
            ).timestamp()
            * 1000
        ) - 1
        raw = client.get_klines(symbol, limit=1, start_ms=start_ms, end_ms=end_ms)
        candles = parse_klines(raw)
        if not candles or candles[-1].open <= 0:
            return None
        return candles[-1].open

    def _evaluate_long_on_candles(
        self,
        symbol: str,
        change_pct: float,
        candles: list[Candle],
        current_price: float,
    ) -> Signal | None:
        closes = [c.close for c in candles]
        ma_fast = sma(closes, self.cfg.ma_fast)
        ma_slow = sma(closes, self.cfg.ma_slow)
        recent_avg = sma(closes, self.cfg.signal_avg_candles)
        if ma_fast is None or ma_slow is None or recent_avg is None:
            return None

        if ma_fast <= ma_slow:
            return None

        last = candles[-1]
        prev = candles[-2]
        if not is_positive(last) or not is_negative(prev):
            return None

        if not (last.open > ma_fast and last.close > ma_fast):
            return None

        if current_price <= 0 or recent_avg >= current_price:
            return None

        logger.info(
            "Long signal: %s change=%.2f%% MA%d=%.6f MA%d=%.6f "
            "avg%d=%.6f price=%.6f",
            symbol,
            change_pct,
            self.cfg.ma_fast,
            ma_fast,
            self.cfg.ma_slow,
            ma_slow,
            self.cfg.signal_avg_candles,
            recent_avg,
            current_price,
        )
        return Signal(
            symbol=symbol,
            side="Buy",
            change_pct_24h=change_pct,
            ma_fast=ma_fast,
            ma_slow=ma_slow,
        )

    def evaluate_long_at_entry(self, symbol: str, entry_date: date) -> Signal | None:
        candles = self._load_closed_candles_before(symbol, entry_date)
        if candles is None or len(candles) < 2:
            return None

        change_pct = daily_change_pct(candles[-1])
        if change_pct <= self.cfg.long_min_change_pct:
            return None

        current_price = self._entry_day_open(symbol, self.client, entry_date)
        if current_price is None:
            return None

        return self._evaluate_long_on_candles(
            symbol, change_pct, candles, current_price
        )

    def _evaluate_short_on_candles(
        self,
        symbol: str,
        change_pct: float,
        candles: list[Candle],
        current_price: float,
    ) -> Signal | None:
        closes = [c.close for c in candles]
        ma_fast = sma(closes, self.cfg.ma_fast)
        ma_slow = sma(closes, self.cfg.ma_slow)
        recent_avg = sma(closes, self.cfg.signal_avg_candles)
        if ma_fast is None or ma_slow is None or recent_avg is None:
            return None

        if ma_fast >= ma_slow:
            return None

        last = candles[-1]
        prev = candles[-2]
        if not is_negative(last) or not is_positive(prev):
            return None

        if not (last.open < ma_fast and last.close < ma_fast):
            return None

        if current_price <= 0 or recent_avg <= current_price:
            return None

        logger.info(
            "Short signal: %s change=%.2f%% MA%d=%.6f MA%d=%.6f "
            "avg%d=%.6f price=%.6f",
            symbol,
            change_pct,
            self.cfg.ma_fast,
            ma_fast,
            self.cfg.ma_slow,
            ma_slow,
            self.cfg.signal_avg_candles,
            recent_avg,
            current_price,
        )
        return Signal(
            symbol=symbol,
            side="Sell",
            change_pct_24h=change_pct,
            ma_fast=ma_fast,
            ma_slow=ma_slow,
        )

    def evaluate_short_at_entry(self, symbol: str, entry_date: date) -> Signal | None:
        candles = self._load_closed_candles_before(symbol, entry_date)
        if candles is None or len(candles) < 2:
            return None

        change_pct = daily_change_pct(candles[-1])
        if change_pct >= self.cfg.short_max_change_pct:
            return None

        current_price = self._entry_day_open(symbol, self.client, entry_date)
        if current_price is None:
            return None

        return self._evaluate_short_on_candles(
            symbol, change_pct, candles, current_price
        )

    def _evaluate_short(
        self, symbol: str, change_pct: float, current_price: float
    ) -> Signal | None:
        candles = self._load_closed_candles(symbol)
        if candles is None:
            return None
        return self._evaluate_short_on_candles(
            symbol, change_pct, candles, current_price
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
            signal = self._evaluate_long(
                ticker.symbol,
                ticker.change_pct_24h,
                ticker.last_price,
            )
            if signal:
                signals.append(signal)
        return signals

    def _evaluate_long(
        self, symbol: str, change_pct: float, current_price: float
    ) -> Signal | None:
        candles = self._load_closed_candles(symbol)
        if candles is None:
            return None
        return self._evaluate_long_on_candles(
            symbol, change_pct, candles, current_price
        )

    def scan_short_candidates(self, tickers: list[SymbolTicker]) -> list[Signal]:
        filtered = [
            t
            for t in tickers
            if t.change_pct_24h < self.cfg.short_max_change_pct
        ]
        filtered.sort(key=lambda t: t.change_pct_24h)

        signals: list[Signal] = []
        for ticker in filtered:
            signal = self._evaluate_short(
                ticker.symbol,
                ticker.change_pct_24h,
                ticker.last_price,
            )
            if signal:
                signals.append(signal)
        return signals

    @staticmethod
    def format_last_candle_change(candles: list[Candle]) -> float:
        if not candles:
            return 0.0
        return daily_change_pct(candles[-1])
