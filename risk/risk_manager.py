"""
Risk Manager: Hard rules gating layer + optional Claude review for borderline signals.
Hard rules are NEVER overridden by LLM.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from config.risk_params_loader import load_risk_params
from config.universes import get_sector_for_symbol, SECTOR_MAP
from monitoring.audit_trail import AuditTrail
from risk.portfolio_state import PortfolioState
from risk.position_sizer import PositionSizer
from signals.signal_model import ApprovedSignal, Signal, TradingMode

logger = logging.getLogger(__name__)


class KillSwitchError(Exception):
    """Raised when a kill switch is triggered."""


class RiskManager:
    def __init__(
        self,
        portfolio: PortfolioState,
        audit: AuditTrail,
        params: dict | None = None,
    ):
        self.portfolio = portfolio
        self.audit = audit
        self.params = params or load_risk_params()
        self.sizer = PositionSizer(
            max_position_pct=self.params["position_limits"]["max_position_size_pct"],
            atr_stop_mult=self.params["stop_loss"]["stop_loss_atr_multiplier"],
            max_risk_per_trade_pct=self.params["loss_limits"]["max_single_trade_loss_pct"],
        )
        self._killed = False

    # -------------------------------------------------------------------------
    # Kill switch checks (run first — bail immediately if triggered)
    # -------------------------------------------------------------------------
    def check_kill_switches(self) -> None:
        """Raise KillSwitchError if any kill switch condition is met."""
        limits = self.params["loss_limits"]
        pos_limits = self.params["position_limits"]

        if self._killed:
            raise KillSwitchError("Kill switch already active.")

        daily_loss = self.portfolio.daily_loss_pct
        drawdown = self.portfolio.drawdown_pct

        if daily_loss >= limits["max_daily_loss_pct"]:
            self._kill(f"Daily loss {daily_loss*100:.2f}% ≥ limit {limits['max_daily_loss_pct']*100}%")

        if drawdown >= limits["max_drawdown_pct"]:
            self._kill(f"Drawdown {drawdown*100:.2f}% ≥ limit {limits['max_drawdown_pct']*100}%")

    def _kill(self, reason: str) -> None:
        self._killed = True
        logger.critical(f"KILL SWITCH: {reason}")
        self.audit.log_kill_switch(reason)
        raise KillSwitchError(reason)

    def reset_kill_switch(self) -> None:
        """Manually reset kill switch (requires human intervention in live)."""
        self._killed = False
        logger.warning("Kill switch manually reset.")

    @property
    def is_killed(self) -> bool:
        return self._killed

    # -------------------------------------------------------------------------
    # Main gate: approve or reject a signal
    # -------------------------------------------------------------------------
    async def evaluate(self, signal: Signal) -> Optional[ApprovedSignal]:
        """
        Evaluate a signal against all hard rules.
        Returns ApprovedSignal if approved, None if rejected.
        """
        try:
            self.check_kill_switches()
        except KillSwitchError as e:
            self.audit.log_risk_decision(signal, False, str(e))
            return None

        rejection = self._hard_rules_check(signal)
        if rejection:
            logger.info(f"Signal rejected [{signal.symbol}]: {rejection}")
            self.audit.log_risk_decision(signal, False, rejection)
            return None

        # Size the position
        capital = self.portfolio.total_capital
        atr = signal.indicators.get("atr")
        price = signal.entry_price or 0.0
        if price <= 0:
            self.audit.log_risk_decision(signal, False, "Invalid entry price")
            return None

        qty, allocated = self.sizer.size_signal(
            capital=capital,
            price=price,
            signal_size_pct=signal.position_size_pct,
            atr=float(atr) if atr else None,
        )

        if qty <= 0:
            self.audit.log_risk_decision(signal, False, "Calculated qty = 0 (insufficient capital)")
            return None

        # Final capital check
        if allocated > self.portfolio.cash:
            self.audit.log_risk_decision(signal, False, f"Insufficient cash: need ₹{allocated:.0f}, have ₹{self.portfolio.cash:.0f}")
            return None

        approved = ApprovedSignal(
            signal=signal,
            approved_qty=qty,
            approved_capital=allocated,
            risk_note="Passed all hard rules.",
        )
        self.audit.log_risk_decision(signal, True, f"Approved: qty={qty}, capital=₹{allocated:.0f}")
        return approved

    def _hard_rules_check(self, signal: Signal) -> Optional[str]:
        """Return rejection reason string or None if all rules pass."""
        pos_limits = self.params["position_limits"]
        liquidity = self.params["liquidity"]
        timing = self.params["timing"]

        # --- Duplicate position check ---
        if self.portfolio.has_position(signal.symbol):
            return f"Already have position in {signal.symbol}"

        # --- Position count limits ---
        if signal.mode == TradingMode.INTRADAY:
            count = self.portfolio.count_by_product("MIS")
            max_count = pos_limits["max_intraday_positions"]
            if count >= max_count:
                return f"Max intraday positions reached ({count}/{max_count})"
        else:
            count = self.portfolio.count_by_product("CNC")
            max_count = pos_limits["max_swing_positions"]
            if count >= max_count:
                return f"Max swing positions reached ({count}/{max_count})"

        # --- Sector exposure ---
        sector = get_sector_for_symbol(signal.symbol)
        if sector:
            sector_symbols = SECTOR_MAP.get(sector, [])
            current_exposure = self.portfolio.sector_exposure(sector_symbols)
            max_sector = pos_limits["max_sector_exposure_pct"]
            if current_exposure >= max_sector:
                return f"Sector {sector} exposure {current_exposure*100:.1f}% ≥ max {max_sector*100}%"

        # --- Correlated positions ---
        if sector:
            sector_count = sum(
                1 for sym in self.portfolio.positions
                if get_sector_for_symbol(sym) == sector
            )
            max_corr = pos_limits["max_correlated_positions"]
            if sector_count >= max_corr:
                return f"Too many correlated positions in {sector} ({sector_count}/{max_corr})"

        # --- Price sanity ---
        if signal.entry_price < liquidity["min_price"]:
            return f"Price ₹{signal.entry_price} below minimum ₹{liquidity['min_price']}"

        # --- Market hours check ---
        now_ist = _current_ist_time()
        if signal.mode == TradingMode.INTRADAY:
            cutoff = timing["intraday_cutoff"]  # e.g. "15:15"
            if now_ist > cutoff:
                return f"Past intraday cutoff {cutoff} IST"

        return None  # All rules passed


def _current_ist_time() -> str:
    """Return current IST time as HH:MM string."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return now.strftime("%H:%M")
