"""
Paper trading simulator: virtual portfolio execution with live market data prices.
Routes orders to a virtual ledger instead of Zerodha.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from monitoring.audit_trail import AuditTrail
from risk.portfolio_state import Position, PortfolioState
from signals.signal_model import ApprovedSignal, ExitSignal

logger = logging.getLogger(__name__)


class PaperSimulator:
    """
    Simulates order execution using live prices.
    All orders are logged but NO real Zerodha orders are placed.
    """

    def __init__(self, portfolio: PortfolioState, audit: AuditTrail):
        self.portfolio = portfolio
        self.audit = audit
        self.virtual_orders: list[dict] = []

    async def execute_entry(
        self,
        approved: ApprovedSignal,
        fill_price: float | None = None,
    ) -> Optional[str]:
        """
        Simulate order execution for an entry signal.
        fill_price: if None, use signal's entry_price (market order simulation).
        Returns a virtual order ID.
        """
        signal = approved.signal
        price = fill_price or signal.entry_price
        qty = approved.approved_qty

        if price <= 0 or qty <= 0:
            logger.warning(f"Invalid paper order: {signal.symbol} price={price} qty={qty}")
            return None

        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        # Apply simple slippage model: 0.05% for market orders
        slippage = 0.0005 if signal.entry_price == 0 else 0.0
        effective_price = round(price * (1 + slippage), 2)

        position = Position(
            symbol=signal.symbol,
            product=signal.product.value,
            qty=qty,
            avg_price=effective_price,
            strategy=signal.strategy,
            mode=signal.mode.value,
            direction="short" if signal.action.value == "SELL" else "long",
            stop_loss=signal.stop_loss,
            target=signal.target,
            order_id=order_id,
        )

        await self.portfolio.open_position(position)

        order_record = {
            "order_id": order_id,
            "symbol": signal.symbol,
            "action": "BUY",
            "qty": qty,
            "price": effective_price,
            "product": signal.product.value,
            "strategy": signal.strategy,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "PAPER",
        }
        self.virtual_orders.append(order_record)
        self.audit.log_order(order_id, signal.symbol, "BUY", qty, effective_price, signal.product.value)
        self.audit.log_fill(order_id, effective_price, qty)

        logger.info(
            f"[PAPER] BUY {qty} {signal.symbol} @ ₹{effective_price:.2f} "
            f"({signal.strategy}, {signal.product.value}) | ID={order_id}"
        )
        return order_id

    async def execute_exit(
        self,
        exit_signal: ExitSignal,
        fill_price: float | None = None,
    ) -> Optional[str]:
        """Simulate exit order."""
        pos = self.portfolio.get_position(exit_signal.symbol)
        if not pos:
            logger.warning(f"[PAPER] No position to exit: {exit_signal.symbol}")
            return None

        price = fill_price or exit_signal.exit_price or pos.avg_price
        slippage = 0.0005
        effective_price = round(price * (1 - slippage), 2)  # Slippage on sells

        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        trade = await self.portfolio.close_position(
            exit_signal.symbol, effective_price, exit_signal.reason
        )

        if trade:
            order_record = {
                "order_id": order_id,
                "symbol": exit_signal.symbol,
                "action": "SELL",
                "qty": trade.qty,
                "price": effective_price,
                "product": exit_signal.product.value,
                "reason": exit_signal.reason,
                "pnl": round(trade.realized_pnl, 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": "PAPER",
            }
            self.virtual_orders.append(order_record)
            self.audit.log_order(order_id, exit_signal.symbol, "SELL", trade.qty, effective_price, exit_signal.product.value)
            self.audit.log_fill(order_id, effective_price, trade.qty)

            logger.info(
                f"[PAPER] SELL {trade.qty} {exit_signal.symbol} @ ₹{effective_price:.2f} "
                f"P&L=₹{trade.realized_pnl:.2f} reason={exit_signal.reason}"
            )

        return order_id

    def get_ledger(self) -> list[dict]:
        """Return all virtual orders for inspection."""
        return list(self.virtual_orders)

    def print_summary(self) -> None:
        summary = self.portfolio.summary()
        print("\n" + "=" * 50)
        print("PAPER TRADING SUMMARY")
        print("=" * 50)
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print(f"  total_orders: {len(self.virtual_orders)}")
        print("=" * 50)
