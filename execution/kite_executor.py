"""
Live order execution via KiteConnect API.
Used only when PAPER_TRADING=false.
"""
from __future__ import annotations

import logging
from typing import Optional

from kiteconnect import KiteConnect

from data.kite_client import get_kite
from monitoring.audit_trail import AuditTrail
from risk.portfolio_state import Position, PortfolioState
from signals.signal_model import ApprovedSignal, ExitSignal

logger = logging.getLogger(__name__)


class KiteExecutor:
    """Executes real orders on Zerodha KiteConnect."""

    def __init__(self, portfolio: PortfolioState, audit: AuditTrail):
        self.portfolio = portfolio
        self.audit = audit

    def _kite(self) -> KiteConnect:
        return get_kite()

    async def execute_entry(self, approved: ApprovedSignal) -> Optional[str]:
        """Place a real BUY order on Zerodha."""
        signal = approved.signal
        qty = approved.approved_qty

        if qty <= 0:
            return None

        # Determine order type
        if signal.entry_price > 0:
            order_type = "LIMIT"
            price = signal.entry_price
        else:
            order_type = "MARKET"
            price = 0

        try:
            order_id = self._kite().place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange="NSE",
                tradingsymbol=signal.symbol,
                transaction_type=KiteConnect.TRANSACTION_TYPE_BUY,
                quantity=qty,
                product=signal.product.value,
                order_type=order_type,
                price=price if price > 0 else None,
                validity=KiteConnect.VALIDITY_DAY,
                tag=f"{signal.strategy[:8].upper()}",
            )

            logger.info(
                f"[LIVE] BUY order placed: {signal.symbol} qty={qty} "
                f"price={price} type={order_type} id={order_id}"
            )
            self.audit.log_order(str(order_id), signal.symbol, "BUY", qty, price, signal.product.value)

            # Register position (will be updated on fill via WebSocket)
            position = Position(
                symbol=signal.symbol,
                product=signal.product.value,
                qty=qty,
                avg_price=price or signal.entry_price,
                strategy=signal.strategy,
                mode=signal.mode.value,
                stop_loss=signal.stop_loss,
                target=signal.target,
                order_id=str(order_id),
            )
            await self.portfolio.open_position(position)

            # Place stop-loss order immediately
            if signal.stop_loss > 0:
                await self._place_stop_loss(signal.symbol, qty, signal.stop_loss, signal.product.value)

            return str(order_id)

        except Exception as exc:
            logger.error(f"Live order failed for {signal.symbol}: {exc}")
            self.audit.log_agent_decision("KiteExecutor", f"Order failed: {exc}")
            return None

    async def _place_stop_loss(self, symbol: str, qty: int, stop_price: float, product: str) -> Optional[str]:
        """Place a stop-loss market order (SL-M)."""
        try:
            sl_order_id = self._kite().place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange="NSE",
                tradingsymbol=symbol,
                transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
                quantity=qty,
                product=product,
                order_type=KiteConnect.ORDER_TYPE_SLM,
                trigger_price=round(stop_price, 1),
                validity=KiteConnect.VALIDITY_DAY,
            )
            logger.info(f"[LIVE] SL-M order placed: {symbol} trigger=₹{stop_price:.2f} id={sl_order_id}")
            return str(sl_order_id)
        except Exception as exc:
            logger.warning(f"Failed to place SL order for {symbol}: {exc}")
            return None

    async def execute_exit(self, exit_signal: ExitSignal, fill_price: float = 0.0) -> Optional[str]:
        """Place a real SELL order on Zerodha."""
        pos = self.portfolio.get_position(exit_signal.symbol)
        if not pos:
            logger.warning(f"[LIVE] No position to exit: {exit_signal.symbol}")
            return None

        try:
            order_id = self._kite().place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange="NSE",
                tradingsymbol=exit_signal.symbol,
                transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
                quantity=pos.qty,
                product=exit_signal.product.value,
                order_type="MARKET",
                validity=KiteConnect.VALIDITY_DAY,
                tag=f"EXIT_{exit_signal.reason[:8].upper()}",
            )
            logger.info(
                f"[LIVE] SELL order placed: {exit_signal.symbol} qty={pos.qty} "
                f"reason={exit_signal.reason} id={order_id}"
            )
            self.audit.log_order(str(order_id), exit_signal.symbol, "SELL", pos.qty, 0.0, exit_signal.product.value)
            return str(order_id)
        except Exception as exc:
            logger.error(f"Live exit order failed for {exit_signal.symbol}: {exc}")
            return None
