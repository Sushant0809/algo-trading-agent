"""
Thread-safe portfolio state tracker.
Tracks open positions, realized/unrealized P&L, drawdown, and daily loss.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    product: str        # MIS or CNC
    qty: int
    avg_price: float
    strategy: str
    mode: str           # intraday or swing
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stop_loss: float = 0.0
    target: float = 0.0
    trailing_stop: Optional[float] = None
    order_id: str = ""

    @property
    def cost_basis(self) -> float:
        return self.avg_price * self.qty

    def unrealized_pnl(self, current_price: float) -> float:
        return (current_price - self.avg_price) * self.qty

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.avg_price == 0:
            return 0.0
        return (current_price - self.avg_price) / self.avg_price


@dataclass
class Trade:
    symbol: str
    product: str
    qty: int
    entry_price: float
    exit_price: float
    strategy: str
    realized_pnl: float
    entered_at: datetime
    exited_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    exit_reason: str = ""


class PortfolioState:
    """
    Central portfolio state. Thread-safe via asyncio.Lock.
    Shared between intraday and swing modes.
    """

    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.peak_capital = initial_capital

        self.positions: dict[str, Position] = {}   # symbol → Position
        self.trades: list[Trade] = []
        self.daily_realized_pnl: float = 0.0
        self.session_date: date = date.today()

        self._lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # Capital
    # -------------------------------------------------------------------------
    @property
    def total_capital(self) -> float:
        return self.cash + self.gross_position_value

    @property
    def gross_position_value(self) -> float:
        return sum(p.avg_price * p.qty for p in self.positions.values())

    @property
    def drawdown_pct(self) -> float:
        if self.peak_capital == 0:
            return 0.0
        return (self.peak_capital - self.total_capital) / self.peak_capital

    @property
    def daily_loss_pct(self) -> float:
        start = self.initial_capital  # Simplification: day-start capital
        return (start - self.total_capital) / start if start > 0 else 0.0

    # -------------------------------------------------------------------------
    # Position management
    # -------------------------------------------------------------------------
    async def open_position(self, position: Position) -> None:
        async with self._lock:
            self.positions[position.symbol] = position
            self.cash -= position.cost_basis
            self.peak_capital = max(self.peak_capital, self.total_capital)
            logger.info(
                f"Position opened: {position.symbol} {position.qty}@{position.avg_price} "
                f"({position.strategy}, {position.product})"
            )

    async def close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str = "",
    ) -> Optional[Trade]:
        async with self._lock:
            pos = self.positions.pop(symbol, None)
            if not pos:
                logger.warning(f"No open position for {symbol}")
                return None

            realized = (exit_price - pos.avg_price) * pos.qty
            self.cash += exit_price * pos.qty
            self.daily_realized_pnl += realized
            self.peak_capital = max(self.peak_capital, self.total_capital)

            trade = Trade(
                symbol=symbol,
                product=pos.product,
                qty=pos.qty,
                entry_price=pos.avg_price,
                exit_price=exit_price,
                strategy=pos.strategy,
                realized_pnl=realized,
                entered_at=pos.entered_at,
                exit_reason=exit_reason,
            )
            self.trades.append(trade)
            logger.info(
                f"Position closed: {symbol} {pos.qty}@{exit_price} "
                f"P&L=₹{realized:.2f} reason={exit_reason}"
            )
            return trade

    async def update_trailing_stop(self, symbol: str, new_stop: float) -> None:
        async with self._lock:
            if symbol in self.positions:
                self.positions[symbol].trailing_stop = new_stop

    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------
    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def get_intraday_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.product == "MIS"]

    def get_swing_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.product == "CNC"]

    def count_by_product(self, product: str) -> int:
        return sum(1 for p in self.positions.values() if p.product == product)

    def sector_exposure(self, sector_symbols: list[str]) -> float:
        """Return capital allocated to a sector as fraction of total."""
        sector_value = sum(
            p.avg_price * p.qty
            for sym, p in self.positions.items()
            if sym in sector_symbols
        )
        return sector_value / self.total_capital if self.total_capital > 0 else 0.0

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            if sym in prices:
                total += pos.unrealized_pnl(prices[sym])
        return total

    def reset_daily_pnl(self) -> None:
        """Call at session start each day."""
        self.daily_realized_pnl = 0.0
        self.session_date = date.today()

    def summary(self) -> dict:
        return {
            "cash": round(self.cash, 2),
            "total_capital": round(self.total_capital, 2),
            "daily_realized_pnl": round(self.daily_realized_pnl, 2),
            "drawdown_pct": round(self.drawdown_pct * 100, 2),
            "open_positions": len(self.positions),
            "intraday_positions": self.count_by_product("MIS"),
            "swing_positions": self.count_by_product("CNC"),
            "total_trades": len(self.trades),
        }
