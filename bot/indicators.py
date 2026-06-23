from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    start_time_ms: int


def parse_klines(raw: list[list[str]]) -> list[Candle]:
    candles: list[Candle] = []
    for row in raw:
        candles.append(
            Candle(
                start_time_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
            )
        )
    return candles


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def is_positive(candle: Candle) -> bool:
    return candle.close > candle.open


def is_negative(candle: Candle) -> bool:
    return candle.close < candle.open


def daily_change_pct(candle: Candle) -> float:
    if candle.open == 0:
        return 0.0
    return (candle.close - candle.open) / candle.open * 100.0
