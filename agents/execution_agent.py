"""
Execution Agent: Consumes approved signals from the bus and places orders.
Also consumes exit signals from the portfolio agent.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from execution.order_manager import OrderManager
from monitoring.audit_trail import AuditTrail
from risk.risk_manager import KillSwitchError
from signals.signal_bus import SignalBus

logger = logging.getLogger(__name__)


class ExecutionAgent:
    def __init__(
        self,
        order_manager: OrderManager,
        signal_bus: SignalBus,
        audit: AuditTrail,
    ):
        self.order_mgr = order_manager
        self.bus = signal_bus
        self.audit = audit

    async def run_entries(self) -> None:
        """Continuous loop: consume approved signals → place entry orders."""
        logger.info("ExecutionAgent (entries) started...")
        while True:
            try:
                approved = await asyncio.wait_for(self.bus.consume_approved(), timeout=1.0)
                order_id = await self.order_mgr.place_entry(approved)
                if order_id:
                    logger.info(
                        f"Entry executed: {approved.signal.symbol} "
                        f"qty={approved.approved_qty} order_id={order_id}"
                    )
                self.bus.approved_done()
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.error(f"Entry execution error: {exc}")
                await asyncio.sleep(0.5)

    async def run_exits(self) -> None:
        """Continuous loop: consume exit signals → place exit orders."""
        logger.info("ExecutionAgent (exits) started...")
        while True:
            try:
                exit_sig = await asyncio.wait_for(self.bus.consume_exit(), timeout=1.0)
                order_id = await self.order_mgr.place_exit(exit_sig)
                if order_id:
                    logger.info(
                        f"Exit executed: {exit_sig.symbol} reason={exit_sig.reason} "
                        f"order_id={order_id}"
                    )
                self.bus.exit_done()
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.error(f"Exit execution error: {exc}")
                await asyncio.sleep(0.5)

    async def run(self) -> None:
        """Run both entry and exit loops concurrently."""
        await asyncio.gather(
            self.run_entries(),
            self.run_exits(),
        )
