from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.bybit_client import BybitClient, ClosedPosition, ExecutedOrder, PositionInfo
from bot.config import AppConfig
from bot.strategy import Signal, StrategyEngine

logger = logging.getLogger(__name__)


@dataclass
class TradingCycleResult:
    closed_symbols: list[str]
    available_balance: float
    long_signals: list[Signal]
    short_signals: list[Signal]
    per_symbol_usdt: float
    executed_orders: list[ExecutedOrder]


@dataclass
class TakeProfitCheckResult:
    enabled: bool
    triggered: bool
    open_positions_count: int
    total_pnl: float
    total_position_value: float
    current_pct: float
    target_pct: float
    target_pnl_usdt: float
    remaining_pnl_usdt: float
    remaining_pct: float
    closed_positions: list[ClosedPosition]


class Trader:
    def __init__(self, config: AppConfig, client: BybitClient) -> None:
        self.config = config
        self.client = client
        self.strategy = StrategyEngine(client, config.trading)

    def close_positions_and_get_balance(
        self,
    ) -> tuple[list[ClosedPosition], float]:
        cfg = self.config.trading

        logger.info("Step 1: closing all open positions")
        closed = self.client.close_all_positions()

        logger.info("Step 2: reading available balance")
        balance = self.client.get_available_balance()
        logger.info("Available balance: %.4f %s", balance, cfg.settle_coin)
        return closed, balance

    def scan_and_execute(
        self,
        balance: float,
        closed_positions: list[ClosedPosition] | None = None,
    ) -> TradingCycleResult:
        cfg = self.config.trading
        closed = closed_positions or []
        closed_symbols = [cp.symbol for cp in closed if cp.success]

        logger.info("Step 3-4: scanning high-24h candidates (short entry)")
        tickers = self.client.get_linear_tickers()
        short_signals = self.strategy.scan_long_candidates(tickers)

        logger.info("Step 5-6: scanning low-24h candidates (long entry)")
        long_signals = self.strategy.scan_short_candidates(tickers)

        total_signals = len(long_signals) + len(short_signals)
        per_symbol_usdt = 0.0
        executed: list[ExecutedOrder] = []

        if total_signals == 0:
            logger.info("No trading signals found")
            return TradingCycleResult(
                closed_symbols=closed_symbols,
                available_balance=balance,
                long_signals=long_signals,
                short_signals=short_signals,
                per_symbol_usdt=0.0,
                executed_orders=executed,
            )

        logger.info(
            "Step 7: allocating balance across %s signals", total_signals
        )
        per_symbol_usdt = balance / total_signals

        all_signals = long_signals + short_signals
        logger.info(
            "Step 8: opening %s positions (%.4f %s each, leverage x%s)",
            total_signals,
            per_symbol_usdt,
            cfg.settle_coin,
            cfg.leverage,
        )

        for signal in all_signals:
            order = self.client.open_market_position(
                symbol=signal.symbol,
                side=signal.side,
                usdt_amount=per_symbol_usdt,
                leverage=cfg.leverage,
            )
            executed.append(order)

        return TradingCycleResult(
            closed_symbols=closed_symbols,
            available_balance=balance,
            long_signals=long_signals,
            short_signals=short_signals,
            per_symbol_usdt=per_symbol_usdt,
            executed_orders=executed,
        )

    def run_daily_cycle(self) -> tuple[TradingCycleResult, list[ClosedPosition]]:
        closed, balance = self.close_positions_and_get_balance()
        return self.scan_and_execute(balance, closed), closed

    @staticmethod
    def _format_pnl_with_pct(pnl: float, base: float, settle_coin: str) -> str:
        text = f"{pnl:+.4f} {settle_coin}"
        if base > 0:
            text += f" ({pnl / base * 100:+.2f}%)"
        return text

    @staticmethod
    def format_balance_message(
        balance: float,
        settle_coin: str,
        mode: str,
        closed_positions: list[ClosedPosition] | None = None,
    ) -> str:
        lines = [
            "💰 Доступные средства",
            f"Режим: {mode}",
            f"Баланс: {balance:.4f} {settle_coin}",
        ]

        if closed_positions is None:
            return "\n".join(lines)

        lines.append("")
        if not closed_positions:
            lines.append("Закрытых позиций не было")
            return "\n".join(lines)

        lines.append("P/L по закрытым позициям:")
        total_pnl = 0.0
        for pos in closed_positions:
            side = "Long" if pos.side == "Buy" else "Short"
            pnl_text = Trader._format_pnl_with_pct(
                pos.pnl, pos.position_value, settle_coin
            )
            if pos.success:
                total_pnl += pos.pnl
                lines.append(f"  {pos.symbol} {side}: {pnl_text}")
            else:
                lines.append(
                    f"  {pos.symbol} {side}: не закрыта (uPnL {pnl_text})"
                )
                if pos.error:
                    lines.append(f"    {pos.error}")

        successful = [p for p in closed_positions if p.success]
        if successful:
            lines.append("")
            lines.append(
                "Итого P/L: "
                + Trader._format_pnl_with_pct(total_pnl, balance, settle_coin)
            )

        return "\n".join(lines)

    @staticmethod
    def _short_error(error: str, limit: int = 100) -> str:
        first_line = error.split("\n", 1)[0].strip()
        if len(first_line) <= limit:
            return first_line
        return first_line[: limit - 3] + "..."

    @staticmethod
    def format_cycle_result_message(result: TradingCycleResult, cfg: AppConfig) -> str:
        lines = ["📊 Результат торгового цикла", ""]

        unique_closed = list(dict.fromkeys(result.closed_symbols))
        if unique_closed:
            lines.append(f"Закрыто позиций: {len(unique_closed)}")
            if len(unique_closed) <= 15:
                lines.append(", ".join(unique_closed))
        else:
            lines.append("Закрытые позиции: нет")

        lines.append(
            f"Баланс до сделок: {result.available_balance:.4f} "
            f"{cfg.trading.settle_coin}"
        )
        lines.append(
            f"Long сигналов: {len(result.long_signals)}, "
            f"Short сигналов: {len(result.short_signals)}"
        )

        if result.long_signals or result.short_signals:
            lines.append(
                f"На каждый символ: {result.per_symbol_usdt:.4f} "
                f"{cfg.trading.settle_coin} (плечо x{cfg.trading.leverage})"
            )

        if result.executed_orders:
            lines.append("")
            lines.append("Сделки:")
            for order in result.executed_orders:
                status = "✅" if order.success else "❌"
                side_label = "Long" if order.side == "Buy" else "Short"
                line = (
                    f"{status} {order.symbol} {side_label} "
                    f"qty={order.qty} margin={order.usdt_amount:.4f}"
                )
                if order.error:
                    line += f" — {Trader._short_error(order.error)}"
                lines.append(line)
        else:
            lines.append("")
            lines.append("Новых сделок не открыто")

        return "\n".join(lines)

    @staticmethod
    def format_pnl_message(
        positions,
        settle_coin: str,
    ) -> str:
        if not positions:
            return "📈 Открытых позиций нет"

        lines = ["📈 PnL открытых позиций", ""]
        total_pnl = 0.0
        for pos in positions:
            total_pnl += pos.unrealised_pnl
            side = "Long" if pos.side == "Buy" else "Short"
            pnl_text = Trader._format_pnl_with_pct(
                pos.unrealised_pnl, pos.position_value, settle_coin
            )
            lines.append(
                f"{pos.symbol} {side} x{pos.leverage}\n"
                f"  size={pos.size} entry={pos.entry_price:.6f} "
                f"mark={pos.mark_price:.6f}\n"
                f"  uPnL={pnl_text}"
            )

        lines.append("")
        total_position_value = Trader.portfolio_position_value(positions)
        if total_position_value > 0:
            lines.append(
                "Итого uPnL: "
                + Trader._format_pnl_with_pct(
                    total_pnl, total_position_value, settle_coin
                )
            )
        else:
            lines.append(
                f"Итого uPnL: {total_pnl:+.4f} {settle_coin}"
            )
        return "\n".join(lines)

    def take_profit_target_pct(self) -> float:
        """Target % = sum(uPnL) / sum(position_value), same as /pnl total."""
        return self.config.trading.take_profit_pct

    @staticmethod
    def portfolio_position_value(positions: list[PositionInfo]) -> float:
        return sum(pos.position_value for pos in positions)

    @staticmethod
    def portfolio_pnl_pct(positions: list[PositionInfo]) -> float:
        total_pnl = sum(pos.unrealised_pnl for pos in positions)
        total_value = Trader.portfolio_position_value(positions)
        if total_value <= 0:
            return 0.0
        return total_pnl / total_value * 100.0

    def run_take_profit_check(self) -> TakeProfitCheckResult:
        cfg = self.config.trading
        target_pct = self.take_profit_target_pct()
        positions = self.client.get_open_positions()

        if not positions:
            return TakeProfitCheckResult(
                enabled=cfg.take_profit_enabled,
                triggered=False,
                open_positions_count=0,
                total_pnl=0.0,
                total_position_value=0.0,
                current_pct=0.0,
                target_pct=target_pct,
                target_pnl_usdt=0.0,
                remaining_pnl_usdt=0.0,
                remaining_pct=target_pct,
                closed_positions=[],
            )

        total_pnl = sum(pos.unrealised_pnl for pos in positions)
        total_value = self.portfolio_position_value(positions)
        current_pct = self.portfolio_pnl_pct(positions)
        target_pnl_usdt = total_value * target_pct / 100.0
        remaining_pnl_usdt = max(0.0, target_pnl_usdt - total_pnl)
        remaining_pct = max(0.0, target_pct - current_pct)
        triggered = cfg.take_profit_enabled and current_pct >= target_pct
        closed: list[ClosedPosition] = []

        if triggered:
            logger.info(
                "Take profit triggered: portfolio %.2f%% >= %.2f%% "
                "(uPnL=%.4f / position_value=%.4f, positions=%s)",
                current_pct,
                target_pct,
                total_pnl,
                total_value,
                len(positions),
            )
            closed = self.client.close_all_positions()
        else:
            logger.debug(
                "Take profit not reached: portfolio %.2f%% < target %.2f%% "
                "(uPnL=%.4f, position_value=%.4f, positions=%s)",
                current_pct,
                target_pct,
                total_pnl,
                total_value,
                len(positions),
            )

        return TakeProfitCheckResult(
            enabled=cfg.take_profit_enabled,
            triggered=triggered,
            open_positions_count=len(positions),
            total_pnl=total_pnl,
            total_position_value=total_value,
            current_pct=current_pct,
            target_pct=target_pct,
            target_pnl_usdt=target_pnl_usdt,
            remaining_pnl_usdt=remaining_pnl_usdt,
            remaining_pct=remaining_pct,
            closed_positions=closed,
        )

    def check_take_profit_and_close(self) -> list[ClosedPosition] | None:
        result = self.run_take_profit_check()
        if result.triggered and result.closed_positions:
            return result.closed_positions
        return None

    def format_take_profit_check_message(
        self,
        result: TakeProfitCheckResult,
        *,
        manual: bool = False,
    ) -> str:
        cfg = self.config.trading
        settle_coin = cfg.settle_coin
        title = "🎯 Проверка take profit"
        if manual:
            title += " (вручную)"
        lines = [title, ""]

        if result.enabled:
            lines.append("Мониторинг: включён")
        else:
            lines.append("Мониторинг: отключён в config (позиции не закроются автоматически)")

        lines.append(f"Открытых позиций: {result.open_positions_count}")
        lines.append("")
        lines.append(
            f"Формула: sum(uPnL) / sum(position_value) "
            f"(как «Итого uPnL» в /pnl)"
        )
        lines.append(
            f"Цель: {result.target_pct:.2f}% "
            f"(≈ {cfg.take_profit_pct:.2f}% × x{cfg.leverage} ROI на марже)"
        )
        lines.append("")

        if result.open_positions_count == 0:
            lines.append("Открытых позиций нет — проверять нечего.")
            return "\n".join(lines)

        lines.append(
            "Текущая прибыль: "
            + self._format_pnl_with_pct(
                result.total_pnl, result.total_position_value, settle_coin
            )
        )
        lines.append(
            f"Ожидаемая (целевая) прибыль: "
            f"{result.target_pnl_usdt:+.4f} {settle_coin} "
            f"(≥ {result.target_pct:.2f}%)"
        )
        if result.remaining_pnl_usdt > 0 or result.remaining_pct > 0:
            lines.append(
                f"До цели: {result.remaining_pnl_usdt:+.4f} {settle_coin} "
                f"({result.remaining_pct:.2f}%)"
            )
        else:
            lines.append("До цели: 0 (порог достигнут)")

        lines.append("")
        if not result.enabled:
            lines.append(
                f"Порог {'достигнут' if result.current_pct >= result.target_pct else 'не достигнут'}, "
                "но take_profit_enabled=false — позиции не закрыты."
            )
        elif result.triggered:
            lines.append("Результат: порог достигнут — закрыты все позиции.")
            if result.closed_positions:
                lines.append("")
                total_closed_pnl = 0.0
                for pos in result.closed_positions:
                    side = "Long" if pos.side == "Buy" else "Short"
                    pnl_text = self._format_pnl_with_pct(
                        pos.pnl, pos.position_value, settle_coin
                    )
                    if pos.success:
                        total_closed_pnl += pos.pnl
                        lines.append(f"✅ {pos.symbol} {side}: {pnl_text}")
                    else:
                        lines.append(f"❌ {pos.symbol} {side}: {pnl_text}")
                        if pos.error:
                            lines.append(f"   {pos.error}")
                successful = [p for p in result.closed_positions if p.success]
                if successful:
                    lines.append("")
                    lines.append(
                        f"Итого P/L по закрытым: {total_closed_pnl:+.4f} {settle_coin}"
                    )
        else:
            lines.append("Результат: порог не достигнут — позиции не закрыты.")

        return "\n".join(lines)
