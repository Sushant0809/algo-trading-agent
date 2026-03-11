"""
Order manager: routes approved signals to either paper simulator or Kite executor.
Single interface for the execution agent.
"""
from __future__ import annotations

import logging
from typing import Optional

from config.settings import get_settings
from execution.kite_executor import KiteExecutor
from execution.paper_simulator import PaperSimulator
from monitoring.alerting import TelegramAlerter
from monitoring.audit_trail import AuditTrail
from risk.portfolio_state import PortfolioState
from signals.signal_model import ApprovedSignal, ExitSignal

logger = logging.getLogger(__name__)


class OrderManager:
    """Routes orders to paper simulator or live Kite executor based on PAPER_TRADING flag."""

    def __init__(
        self,
        portfolio: PortfolioState,
        audit: AuditTrail,
        alerter: TelegramAlerter | None = None,
    ):
        self.portfolio = portfolio
        self.audit = audit
        self.alerter = alerter
        settings = get_settings()

        self.paper = PaperSimulator(portfolio, audit)
        self.live = KiteExecutor(portfolio, audit)
        self._is_paper = settings.is_paper

        mode = "PAPER" if self._is_paper else "LIVE"
        logger.info(f"OrderManager initialized in {mode} mode.")

    async def place_entry(self, approved: ApprovedSignal) -> Optional[str]:
        """Execute an entry order."""
        if self._is_paper:
            order_id = await self.paper.execute_entry(approved)
        else:
            order_id = await self.live.execute_entry(approved)

        if order_id and self.alerter:
            sig = approved.signal
            price = sig.entry_price or 0
            await self.alerter.alert_fill(
                sig.symbol, "BUY", approved.approved_qty, price, sig.product.value
            )
        return order_id

    async def place_exit(
        self,
        exit_signal: ExitSignal,
        fill_price: float = 0.0,
    ) -> Optional[str]:
        """Execute an exit order."""
        pos = self.portfolio.get_position(exit_signal.symbol)
        if not pos:
            return None

        if self._is_paper:
            order_id = await self.paper.execute_exit(exit_signal, fill_price)
        else:
            order_id = await self.live.execute_exit(exit_signal, fill_price)

        if order_id and self.alerter:
            await self.alerter.alert_fill(
                exit_signal.symbol, "SELL", pos.qty, fill_price or 0.0, exit_signal.product.value
            )
        return order_id

    def get_ledger(self) -> list[dict]:
        """Return paper trading ledger (empty in live mode)."""
        if self._is_paper:
            return self.paper.get_ledger()
        return []
