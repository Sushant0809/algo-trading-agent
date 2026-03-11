"""
WebSocket order update handler: tracks fills from Kite ticker → updates portfolio state.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from monitoring.audit_trail import AuditTrail
from risk.portfolio_state import PortfolioState

logger = logging.getLogger(__name__)


class FillTracker:
    """
    Subscribes to KiteTicker order updates (via postback/WebSocket).
    Updates portfolio state on fills.
    """

    def __init__(self, portfolio: PortfolioState, audit: AuditTrail):
        self.portfolio = portfolio
        self.audit = audit
        self._callbacks: list[Callable] = []

    def on_order_update(self, data: dict) -> None:
        """
        Called by KiteTicker on_order_update callback.
        data: Zerodha order update dict.
        """
        status = data.get("status", "").lower()
        symbol = data.get("tradingsymbol", "")
        order_id = data.get("order_id", "")
        avg_price = float(data.get("average_price", 0) or 0)
        filled_qty = int(data.get("filled_quantity", 0) or 0)
        txn_type = data.get("transaction_type", "")

        logger.info(
            f"Order update: {symbol} {txn_type} status={status} "
            f"qty={filled_qty} @ ₹{avg_price:.2f} id={order_id}"
        )

        if status == "complete":
            self.audit.log_fill(order_id, avg_price, filled_qty)
            # Update position avg price if needed
            pos = self.portfolio.get_position(symbol)
            if pos and txn_type == "BUY" and avg_price > 0:
                pos.avg_price = avg_price  # Sync with actual fill price

            # Notify callbacks
            for cb in self._callbacks:
                try:
                    cb(data)
                except Exception as exc:
                    logger.warning(f"Fill callback error: {exc}")

        elif status in ("rejected", "cancelled"):
            logger.warning(f"Order {status}: {symbol} id={order_id}")
            # If we pre-opened a position, we need to close it
            if txn_type == "BUY" and self.portfolio.has_position(symbol):
                asyncio.create_task(
                    self.portfolio.close_position(symbol, 0, "order_rejected")
                )

    def register_callback(self, fn: Callable) -> None:
        self._callbacks.append(fn)

    def setup_ticker_callbacks(self, ticker) -> None:
        """Wire this tracker into the KiteTicker instance."""
        ticker.on_order_update = self.on_order_update
        logger.info("FillTracker connected to KiteTicker.")
