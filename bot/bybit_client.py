from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from pybit.unified_trading import HTTP
import pybit._helpers as pybit_helpers
from pybit.exceptions import InvalidRequestError

from bot.config import AppConfig, BybitConfig, TradingConfig

logger = logging.getLogger(__name__)


@dataclass
class SymbolTicker:
    symbol: str
    change_pct_24h: float
    last_price: float


@dataclass
class PositionInfo:
    symbol: str
    side: str
    size: float
    size_str: str
    entry_price: float
    mark_price: float
    unrealised_pnl: float
    leverage: str


@dataclass
class ClosedPosition:
    symbol: str
    side: str
    pnl: float
    success: bool
    error: str | None = None


@dataclass
class ExecutedOrder:
    symbol: str
    side: str
    qty: str
    usdt_amount: float
    order_id: str | None
    success: bool
    error: str | None = None


class BybitClient:
    _time_offset_ms: int = 0
    _original_generate_timestamp = pybit_helpers.generate_timestamp

    def __init__(self, bybit_cfg: BybitConfig, trading_cfg: TradingConfig) -> None:
        self.trading = trading_cfg
        self.session = self._create_session(bybit_cfg)
        self._sync_time_offset()

    @staticmethod
    def _create_session(cfg: BybitConfig) -> HTTP:
        common = {
            "api_key": cfg.api_key,
            "api_secret": cfg.api_secret,
            "timeout": 30,
            "recv_window": cfg.recv_window,
        }
        if cfg.mode == "demo":
            return HTTP(testnet=False, demo=True, **common)
        if cfg.mode == "testnet":
            return HTTP(testnet=True, demo=False, **common)
        return HTTP(testnet=False, demo=False, **common)

    @classmethod
    def _apply_time_offset(cls, offset_ms: int) -> None:
        cls._time_offset_ms = offset_ms

        def adjusted_timestamp() -> int:
            return cls._original_generate_timestamp() + cls._time_offset_ms

        pybit_helpers.generate_timestamp = adjusted_timestamp

    def _sync_time_offset(self) -> None:
        try:
            result = self.session.get_server_time()
            if result.get("retCode") != 0:
                return

            server_ms = int(result.get("time", 0))
            if server_ms <= 0:
                server_ms = int((result.get("result") or {}).get("timeSecond", 0)) * 1000
            if server_ms <= 0:
                return

            local_ms = int(time.time() * 1000)
            offset_ms = server_ms - local_ms
            self._apply_time_offset(offset_ms)

            skew_sec = abs(offset_ms) / 1000.0
            if skew_sec > 1:
                logger.warning(
                    "System clock differs from Bybit by %.1f s. "
                    "Applied automatic time offset for API requests. "
                    "Also sync Windows time: Settings -> Time & language -> Sync now.",
                    skew_sec,
                )
        except Exception:
            logger.debug("Could not sync Bybit server time", exc_info=True)

    def _unwrap(self, response: dict[str, Any]) -> dict[str, Any]:
        if response.get("retCode") != 0:
            raise RuntimeError(
                f"Bybit API error {response.get('retCode')}: {response.get('retMsg')}"
            )
        return response.get("result") or {}

    def get_available_balance(self) -> float:
        result = self._unwrap(
            self.session.get_wallet_balance(
                accountType=self.trading.account_type,
                coin=self.trading.settle_coin,
            )
        )
        accounts = result.get("list") or []
        if not accounts:
            return 0.0

        for coin in accounts[0].get("coin") or []:
            if coin.get("coin") == self.trading.settle_coin:
                wallet = float(coin.get("walletBalance") or 0)
                locked = float(coin.get("locked") or 0)
                return max(wallet - locked, 0.0)

        total = accounts[0].get("totalAvailableBalance")
        if total is not None:
            return float(total)
        return 0.0

    def get_linear_tickers(self) -> list[SymbolTicker]:
        result = self._unwrap(
            self.session.get_tickers(category=self.trading.category)
        )
        tickers: list[SymbolTicker] = []
        suffix = self.trading.settle_coin

        for item in result.get("list") or []:
            symbol = item.get("symbol", "")
            if not symbol.endswith(suffix):
                continue
            change_raw = item.get("price24hPcnt")
            last_price = item.get("lastPrice")
            if change_raw is None or last_price is None:
                continue
            change_pct = float(change_raw) * 100.0
            tickers.append(
                SymbolTicker(
                    symbol=symbol,
                    change_pct_24h=change_pct,
                    last_price=float(last_price),
                )
            )
        return tickers

    def get_klines(self, symbol: str, limit: int) -> list[list[str]]:
        result = self._unwrap(
            self.session.get_kline(
                category=self.trading.category,
                symbol=symbol,
                interval=self.trading.candle_interval,
                limit=limit,
            )
        )
        rows = result.get("list") or []
        rows.reverse()
        return rows

    def get_instrument_info(self, symbol: str) -> dict[str, Any]:
        result = self._unwrap(
            self.session.get_instruments_info(
                category=self.trading.category,
                symbol=symbol,
            )
        )
        items = result.get("list") or []
        if not items:
            raise RuntimeError(f"No instrument info for {symbol}")
        return items[0]

    def _format_qty(self, qty: float, step: float) -> str:
        if step >= 1:
            return str(int(qty))
        precision = max(0, int(round(-math.log10(step))))
        return f"{qty:.{precision}f}"

    def _round_qty(self, qty: float, symbol: str, *, cap_to_max: bool = True) -> str:
        info = self.get_instrument_info(symbol)
        lot = info.get("lotSizeFilter") or {}
        step = float(lot.get("qtyStep") or "0.001")
        min_qty = float(lot.get("minOrderQty") or step)
        max_qty = float(lot.get("maxOrderQty") or lot.get("maxMktOrderQty") or 0)

        if step <= 0:
            step = 0.001

        rounded = math.floor(qty / step) * step
        if cap_to_max and max_qty > 0:
            rounded = min(rounded, max_qty)
            rounded = math.floor(rounded / step) * step

        if rounded < min_qty:
            return ""
        return self._format_qty(rounded, step)

    def _qty_from_position(self, size_str: str, symbol: str) -> str:
        info = self.get_instrument_info(symbol)
        lot = info.get("lotSizeFilter") or {}
        step = float(lot.get("qtyStep") or "0.001")
        min_qty = float(lot.get("minOrderQty") or step)

        if step <= 0:
            step = 0.001

        qty = float(size_str)
        rounded = math.floor(qty / step) * step
        if rounded < min_qty:
            return ""
        return self._format_qty(rounded, step)

    def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            self._unwrap(
                self.session.set_leverage(
                    category=self.trading.category,
                    symbol=symbol,
                    buyLeverage=str(leverage),
                    sellLeverage=str(leverage),
                )
            )
        except (RuntimeError, InvalidRequestError) as exc:
            msg = str(exc).lower()
            if "leverage not modified" in msg or "110043" in msg:
                logger.debug("Leverage already set for %s: %sx", symbol, leverage)
                return
            raise

    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "category": self.trading.category,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty,
        }
        if reduce_only:
            params["reduceOnly"] = True
        return self._unwrap(self.session.place_order(**params))

    def open_market_position(
        self, symbol: str, side: str, usdt_amount: float, leverage: int
    ) -> ExecutedOrder:
        try:
            self.set_leverage(symbol, leverage)
            ticker = next(
                (t for t in self.get_linear_tickers() if t.symbol == symbol),
                None,
            )
            if ticker is None or ticker.last_price <= 0:
                return ExecutedOrder(
                    symbol=symbol,
                    side=side,
                    qty="0",
                    usdt_amount=usdt_amount,
                    order_id=None,
                    success=False,
                    error="Could not get last price",
                )

            notional = usdt_amount * leverage
            qty = self._round_qty(notional / ticker.last_price, symbol)
            if not qty:
                return ExecutedOrder(
                    symbol=symbol,
                    side=side,
                    qty="0",
                    usdt_amount=usdt_amount,
                    order_id=None,
                    success=False,
                    error="Calculated qty below minimum",
                )

            result = self.place_market_order(symbol, side, qty)
            order_id = (result.get("orderId") or None)
            return ExecutedOrder(
                symbol=symbol,
                side=side,
                qty=qty,
                usdt_amount=usdt_amount,
                order_id=order_id,
                success=True,
            )
        except Exception as exc:
            logger.exception("Failed to open position for %s", symbol)
            return ExecutedOrder(
                symbol=symbol,
                side=side,
                qty="0",
                usdt_amount=usdt_amount,
                order_id=None,
                success=False,
                error=str(exc),
            )

    def get_open_positions(self) -> list[PositionInfo]:
        result = self._unwrap(
            self.session.get_positions(
                category=self.trading.category,
                settleCoin=self.trading.settle_coin,
            )
        )
        positions: list[PositionInfo] = []
        for item in result.get("list") or []:
            size = float(item.get("size") or 0)
            if size == 0:
                continue
            positions.append(
                PositionInfo(
                    symbol=item.get("symbol", ""),
                    side=item.get("side", ""),
                    size=size,
                    size_str=str(item.get("size") or "0"),
                    entry_price=float(item.get("avgPrice") or 0),
                    mark_price=float(item.get("markPrice") or 0),
                    unrealised_pnl=float(item.get("unrealisedPnl") or 0),
                    leverage=str(item.get("leverage") or "1"),
                )
            )
        return positions

    def close_all_positions(self) -> list[ClosedPosition]:
        closed: list[ClosedPosition] = []
        positions = self.get_open_positions()
        for pos in positions:
            close_side = "Sell" if pos.side == "Buy" else "Buy"
            qty = self._qty_from_position(pos.size_str, pos.symbol)
            if not qty:
                logger.warning(
                    "Skip close %s: qty %s below minimum",
                    pos.symbol,
                    pos.size_str,
                )
                closed.append(
                    ClosedPosition(
                        symbol=pos.symbol,
                        side=pos.side,
                        pnl=pos.unrealised_pnl,
                        success=False,
                        error="qty below minimum",
                    )
                )
                continue
            try:
                self.place_market_order(
                    pos.symbol, close_side, qty, reduce_only=True
                )
                closed.append(
                    ClosedPosition(
                        symbol=pos.symbol,
                        side=pos.side,
                        pnl=pos.unrealised_pnl,
                        success=True,
                    )
                )
                logger.info(
                    "Closed position: %s (%s) PnL=%.4f",
                    pos.symbol,
                    pos.side,
                    pos.unrealised_pnl,
                )
            except Exception as exc:
                logger.error("Failed to close %s: %s", pos.symbol, exc)
                closed.append(
                    ClosedPosition(
                        symbol=pos.symbol,
                        side=pos.side,
                        pnl=pos.unrealised_pnl,
                        success=False,
                        error=str(exc),
                    )
                )
        return closed

    @classmethod
    def from_app_config(cls, config: AppConfig) -> "BybitClient":
        return cls(config.bybit, config.trading)
