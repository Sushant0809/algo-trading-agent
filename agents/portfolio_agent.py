"""
Portfolio Agent: 1-minute heartbeat loop.
Monitors open positions, manages exits:
- Stop-loss hits
- Target hits
- Trailing stop
- EOD MIS close at 3:15pm IST
- Kill switch triggered
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config.risk_params_loader import load_risk_params
from data.market_data import fetch_ltp
from monitoring.alerting import TelegramAlerter
from monitoring.audit_trail import AuditTrail
from risk.portfolio_state import Position, PortfolioState
from risk.risk_manager import RiskManager
from signals.signal_bus import SignalBus
from signals.signal_model import ExitSignal, Product, SignalAction, TradingMode

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class PortfolioAgent:
    """
    Heartbeat agent running every ~60 seconds.
    Manages all open positions: exits, trailing stops, EOD closing.
    """

    def __init__(
        self,
        portfolio: PortfolioState,
        signal_bus: SignalBus,
        risk_manager: RiskManager,
        audit: AuditTrail,
        alerter: TelegramAlerter | None = None,
        heartbeat_secs: int = 60,
    ):
        self.portfolio = portfolio
        self.bus = signal_bus
        self.risk_mgr = risk_manager
        self.audit = audit
        self.alerter = alerter
        self.heartbeat_secs = heartbeat_secs
        self.params = load_risk_params()

    async def run(self) -> None:
        """Main 1-minute heartbeat loop."""
        logger.info("PortfolioAgent started (heartbeat every 60s)")
        while True:
            try:
                await self._heartbeat()
            except Exception as exc:
                logger.error(f"PortfolioAgent heartbeat error: {exc}")
            await asyncio.sleep(self.heartbeat_secs)

    async def _heartbeat(self) -> None:
        positions = self.portfolio.positions
        if not positions:
            return

        now_ist = datetime.now(IST)
        now_str = now_ist.strftime("%H:%M")
        eod_close_time = self.params["timing"]["mis_close_time"]  # "15:15"

        # --- Fetch current prices ---
        from config.instruments import get_token
        token_map = {get_token(sym): sym for sym in positions if get_token(sym)}
        if not token_map:
            return

        prices = fetch_ltp(list(token_map.keys()))
        # Remap token→symbol→price
        symbol_prices: dict[str, float] = {
            token_map[token]: price for token, price in prices.items() if token in token_map
        }

        for symbol, pos in list(positions.items()):
            price = symbol_prices.get(symbol)
            if not price:
                continue

            exit_reason = self._check_exit_conditions(pos, price, now_str, eod_close_time)
            if exit_reason:
                await self._trigger_exit(pos, price, exit_reason)

            # Update trailing stop
            elif pos.stop_loss > 0:
                new_trail = await self._update_trailing_stop(pos, price)
                if new_trail:
                    await self.portfolio.update_trailing_stop(symbol, new_trail)

    def _check_exit_conditions(
        self,
        pos: Position,
        current_price: float,
        now_str: str,
        eod_close_time: str,
    ) -> str | None:
        """Check all exit conditions. Returns reason string if exit needed."""

        # EOD MIS close
        if pos.product == "MIS" and now_str >= eod_close_time:
            return "eod_close"

        # Kill switch active
        if self.risk_mgr.is_killed:
            return "kill_switch"

        # Stop-loss — direction aware
        effective_stop = pos.trailing_stop or pos.stop_loss
        if effective_stop > 0:
            if pos.is_short and current_price >= effective_stop:
                return "stop_loss"
            elif not pos.is_short and current_price <= effective_stop:
                return "stop_loss"

        # Target — direction aware
        if pos.target > 0:
            if pos.is_short and current_price <= pos.target:
                return "target"
            elif not pos.is_short and current_price >= pos.target:
                return "target"

        return None

    async def _update_trailing_stop(self, pos: Position, current_price: float) -> float | None:
        """Update trailing stop if price moved favorably (direction-aware)."""
        if pos.stop_loss <= 0 or pos.avg_price <= 0:
            return None

        trail_params = self.params["stop_loss"]
        activation_pct = trail_params["trailing_stop_activation_pct"] / 100
        distance_pct = trail_params["trailing_stop_distance_pct"] / 100

        if pos.is_short:
            profit_pct = (pos.avg_price - current_price) / pos.avg_price
            if profit_pct < activation_pct:
                return None
            new_trail = round(current_price * (1 + distance_pct), 2)   # trail above price for short
            current_stop = pos.trailing_stop or pos.stop_loss
            if new_trail < current_stop:   # lower stop = better for short
                logger.debug(f"Trailing stop updated (short): {pos.symbol} {current_stop:.2f} → {new_trail:.2f}")
                return new_trail
        else:
            profit_pct = (current_price - pos.avg_price) / pos.avg_price
            if profit_pct < activation_pct:
                return None
            new_trail = round(current_price * (1 - distance_pct), 2)
            current_stop = pos.trailing_stop or pos.stop_loss
            if new_trail > current_stop:
                logger.debug(f"Trailing stop updated (long): {pos.symbol} {current_stop:.2f} → {new_trail:.2f}")
                return new_trail
        return None

    async def _trigger_exit(self, pos: Position, price: float, reason: str) -> None:
        """Create and publish an exit signal."""
        mode = TradingMode.INTRADAY if pos.product == "MIS" else TradingMode.SWING
        product = Product.MIS if pos.product == "MIS" else Product.CNC

        exit_sig = ExitSignal(
            symbol=pos.symbol,
            action=SignalAction.SELL,
            strategy=pos.strategy,
            mode=mode,
            product=product,
            reason=reason,
            exit_price=price,
        )

        await self.bus.publish_exit(exit_sig)
        logger.info(f"Exit triggered: {pos.symbol} @ ₹{price:.2f} reason={reason}")

        if self.alerter and reason == "kill_switch":
            await self.alerter.alert_kill_switch(f"Kill switch — closing {pos.symbol}")

    async def close_all_mis(self) -> None:
        """Force-close all MIS positions (called at 3:15pm IST)."""
        mis_positions = self.portfolio.get_intraday_positions()
        logger.warning(f"EOD: force-closing {len(mis_positions)} MIS positions")

        from config.instruments import get_token
        tokens = [get_token(p.symbol) for p in mis_positions if get_token(p.symbol)]
        prices = fetch_ltp(tokens)
        token_to_price = {v: prices.get(k) for k, v in {get_token(p.symbol): p.symbol for p in mis_positions}.items()}

        for pos in mis_positions:
            price = token_to_price.get(pos.symbol, pos.avg_price)
            await self._trigger_exit(pos, price or pos.avg_price, "eod_close")
