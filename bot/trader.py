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
    leverage: int = 0


@dataclass
class TakeProfitCheckResult:
    enabled: bool
    triggered: bool
    open_positions_count: int
    total_pnl: float
    total_position_value: float
    total_margin: float
    avg_leverage: float
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
                leverage=0,
            )

        logger.info(
            "Step 7: allocating balance across %s signals", total_signals
        )
        per_symbol_usdt = balance / total_signals

        all_signals = long_signals + short_signals
        symbols = [signal.symbol for signal in all_signals]
        common_leverage = self.client.resolve_common_leverage(
            symbols, cfg.leverage
        )
        logger.info(
            "Step 8: opening %s positions (%.4f %s each, common leverage x%s)",
            total_signals,
            per_symbol_usdt,
            cfg.settle_coin,
            common_leverage,
        )

        for signal in all_signals:
            order = self.client.open_market_position(
                symbol=signal.symbol,
                side=signal.side,
                usdt_amount=per_symbol_usdt,
                leverage=common_leverage,
            )
            executed.append(order)

        return TradingCycleResult(
            closed_symbols=closed_symbols,
            available_balance=balance,
            long_signals=long_signals,
            short_signals=short_signals,
            per_symbol_usdt=per_symbol_usdt,
            executed_orders=executed,
            leverage=common_leverage,
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
            lev = result.leverage or cfg.trading.leverage
            lev_note = f"плечо x{lev}"
            if result.leverage and result.leverage < cfg.trading.leverage:
                lev_note += f" (config x{cfg.trading.leverage}, взято min max по группе)"
            lines.append(
                f"На каждый символ: {result.per_symbol_usdt:.4f} "
                f"{cfg.trading.settle_coin} ({lev_note})"
            )

        if result.executed_orders:
            lines.append("")
            lines.append("Сделки:")
            for order in result.executed_orders:
                status = "✅" if order.success else "❌"
                side_label = "Long" if order.side == "Buy" else "Short"
                lev = order.leverage or result.leverage or cfg.trading.leverage
                line = (
                    f"{status} {order.symbol} {side_label} "
                    f"qty={order.qty} margin={order.usdt_amount:.4f} x{lev}"
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
            margin = Trader.position_margin(pos)
            pnl_text = Trader._format_pnl_with_pct(
                pos.unrealised_pnl, margin, settle_coin
            )
            lines.append(
                f"{pos.symbol} {side} x{pos.leverage}\n"
                f"  size={pos.size} entry={pos.entry_price:.6f} "
                f"mark={pos.mark_price:.6f}\n"
                f"  uPnL={pnl_text}"
            )

        lines.append("")
        total_margin = Trader.portfolio_margin(positions)
        if total_margin > 0:
            lines.append(
                "Итого uPnL: "
                + Trader._format_pnl_with_pct(
                    total_pnl, total_margin, settle_coin
                )
                + " (ROI на марже)"
            )
        else:
            lines.append(
                f"Итого uPnL: {total_pnl:+.4f} {settle_coin}"
            )
        return "\n".join(lines)

    @staticmethod
    def position_margin(pos: PositionInfo) -> float:
        leverage = float(pos.leverage or 1) or 1.0
        return pos.position_value / leverage

    @staticmethod
    def portfolio_position_value(positions: list[PositionInfo]) -> float:
        return sum(pos.position_value for pos in positions)

    @staticmethod
    def portfolio_margin(positions: list[PositionInfo]) -> float:
        return sum(Trader.position_margin(pos) for pos in positions)

    @staticmethod
    def portfolio_avg_leverage(positions: list[PositionInfo]) -> float:
        total_margin = Trader.portfolio_margin(positions)
        if total_margin <= 0:
            return 1.0
        return Trader.portfolio_position_value(positions) / total_margin

    def take_profit_target_pct(
        self, positions: list[PositionInfo] | None = None
    ) -> float:
        """
        Target ROI % on deployed margin.

        Config take_profit_pct is a notional (price-move) target; equivalent
        margin ROI uses the portfolio's actual average leverage so mixed
        leverages are handled correctly.
        """
        cfg = self.config.trading
        if positions:
            avg_lev = self.portfolio_avg_leverage(positions)
            if avg_lev > 0:
                return cfg.take_profit_pct * avg_lev
        return cfg.take_profit_pct * cfg.leverage

    @staticmethod
    def portfolio_pnl_pct(positions: list[PositionInfo]) -> float:
        """Unrealized PnL as % of deployed margin (per-symbol leverage aware)."""
        total_pnl = sum(pos.unrealised_pnl for pos in positions)
        total_margin = Trader.portfolio_margin(positions)
        if total_margin <= 0:
            return 0.0
        return total_pnl / total_margin * 100.0

    def run_take_profit_check(self) -> TakeProfitCheckResult:
        cfg = self.config.trading
        positions = self.client.get_open_positions()
        target_pct = self.take_profit_target_pct(positions)

        if not positions:
            return TakeProfitCheckResult(
                enabled=cfg.take_profit_enabled,
                triggered=False,
                open_positions_count=0,
                total_pnl=0.0,
                total_position_value=0.0,
                total_margin=0.0,
                avg_leverage=float(cfg.leverage),
                current_pct=0.0,
                target_pct=target_pct,
                target_pnl_usdt=0.0,
                remaining_pnl_usdt=0.0,
                remaining_pct=target_pct,
                closed_positions=[],
            )

        total_pnl = sum(pos.unrealised_pnl for pos in positions)
        total_value = self.portfolio_position_value(positions)
        total_margin = self.portfolio_margin(positions)
        avg_leverage = self.portfolio_avg_leverage(positions)
        current_pct = self.portfolio_pnl_pct(positions)
        target_pnl_usdt = total_margin * target_pct / 100.0
        remaining_pnl_usdt = max(0.0, target_pnl_usdt - total_pnl)
        remaining_pct = max(0.0, target_pct - current_pct)
        triggered = cfg.take_profit_enabled and current_pct >= target_pct
        closed: list[ClosedPosition] = []

        if triggered:
            logger.info(
                "Take profit triggered: margin ROI %.2f%% >= %.2f%% "
                "(uPnL=%.4f / margin=%.4f, avg_lev=x%.2f, positions=%s)",
                current_pct,
                target_pct,
                total_pnl,
                total_margin,
                avg_leverage,
                len(positions),
            )
            closed = self.client.close_all_positions()
        else:
            logger.debug(
                "Take profit not reached: margin ROI %.2f%% < target %.2f%% "
                "(uPnL=%.4f, margin=%.4f, avg_lev=x%.2f, positions=%s)",
                current_pct,
                target_pct,
                total_pnl,
                total_margin,
                avg_leverage,
                len(positions),
            )

        return TakeProfitCheckResult(
            enabled=cfg.take_profit_enabled,
            triggered=triggered,
            open_positions_count=len(positions),
            total_pnl=total_pnl,
            total_position_value=total_value,
            total_margin=total_margin,
            avg_leverage=avg_leverage,
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
            "Формула: sum(uPnL) / sum(margin), "
            "margin = position_value / leverage по каждому символу"
        )
        lines.append(
            f"Цель: {result.target_pct:.2f}% ROI на марже "
            f"(take_profit_pct {cfg.take_profit_pct:.2f}% × "
            f"среднее плечо x{result.avg_leverage:.2f})"
        )
        lines.append("")

        if result.open_positions_count == 0:
            lines.append("Открытых позиций нет — проверять нечего.")
            return "\n".join(lines)

        lines.append(
            "Текущая прибыль: "
            + self._format_pnl_with_pct(
                result.total_pnl, result.total_margin, settle_coin
            )
            + " (ROI на марже)"
        )
        lines.append(
            f"Маржа портфеля: {result.total_margin:.4f} {settle_coin}, "
            f"notional: {result.total_position_value:.4f} {settle_coin}"
        )
        lines.append(
            f"Ожидаемая (целевая) прибыль: "
            f"{result.target_pnl_usdt:+.4f} {settle_coin} "
            f"(≥ {result.target_pct:.2f}% ROI)"
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
