"""
Portfolio Allocator: decides how to split available cash across competing signals.

Problem it solves:
    Without this, 5 strategies could each independently claim 5% of capital.
    If all 5 fire at once, the system tries to allocate 25% total — fine.
    But if 15 fire at once, it tries to allocate 75% with no coordination.
    Worse: it could leave < 10% cash reserve, which the hackathon env taught us
    is essential for flexibility.

How it works:
    1. Collects all signals pending in the raw queue (non-blocking drain).
    2. Scores each signal: strategy_weight × signal.confidence × strength_mult.
    3. Sorts by score descending.
    4. Greedily allocates cash, respecting:
       - 10% cash floor (CASH_RESERVE_PCT)
       - max_position_size_pct per position (from risk_params)
       - max_swing_positions / max_intraday_positions counts
       - No duplicate symbols
    5. Returns the shortlisted signals; others are dropped.

Usage:
    Called from RiskAgent before individual signal evaluation.
    Replaces the raw queue drain with a pre-filtered list.
"""
from __future__ import annotations

import logging
from typing import Optional

from config.risk_params_loader import load_risk_params
from risk.portfolio_state import PortfolioState
from signals.signal_model import Signal, SignalStrength, TradingMode

logger = logging.getLogger(__name__)

CASH_RESERVE_PCT    = 0.10   # Always keep 10% cash
MAX_SIGNALS_SWING   = 3      # Max new swing positions per allocation cycle
MAX_SIGNALS_INTRA   = 5      # Max new intraday positions per allocation cycle

STRENGTH_MULT = {
    SignalStrength.STRONG:   1.0,
    SignalStrength.MODERATE: 0.75,
    SignalStrength.WEAK:     0.40,
}


def _score(signal: Signal, strategy_weights: dict[str, float]) -> float:
    """
    Composite score for ranking signals.
    score = strategy_weight × confidence × strength_multiplier
    """
    weight   = strategy_weights.get(signal.strategy, 0.25)
    conf     = signal.confidence or 0.5
    strength = STRENGTH_MULT.get(signal.strength, 0.75)
    return weight * conf * strength


class PortfolioAllocator:
    """
    Pre-filters and ranks a batch of signals before they enter the risk pipeline.
    Ensures the system never over-commits capital and always holds a cash reserve.
    """

    def __init__(self, portfolio: PortfolioState, params: dict | None = None):
        self.portfolio = portfolio
        self.params = params or load_risk_params()

    def allocate(
        self,
        signals: list[Signal],
        strategy_weights: dict[str, float] | None = None,
    ) -> list[Signal]:
        """
        Given a batch of candidate signals, return the shortlist that can be
        funded within the cash reserve constraint.

        Args:
            signals:          All candidate entry signals for this cycle.
            strategy_weights: Regime-based weights from StrategySelector.
                              Defaults to equal weights if not provided.

        Returns:
            Ordered list of signals that passed allocation (best first).
            The caller (RiskAgent) still runs full hard-rule checks on each.
        """
        if not signals:
            return []

        weights = strategy_weights or {}

        # --- Available capital after reserve ---
        total_capital    = self.portfolio.total_capital
        spendable        = total_capital * (1.0 - CASH_RESERVE_PCT)
        already_deployed = total_capital - self.portfolio.cash
        available_cash   = max(0.0, spendable - already_deployed)

        if available_cash <= 0:
            logger.info("PortfolioAllocator: no cash available after reserve — all signals dropped")
            return []

        max_pos_pct  = self.params["position_limits"]["max_position_size_pct"]
        max_swing    = self.params["position_limits"]["max_swing_positions"]
        max_intraday = self.params["position_limits"]["max_intraday_positions"]

        current_swing   = self.portfolio.count_by_product("CNC")
        current_intraday = self.portfolio.count_by_product("MIS")

        # --- Deduplicate by symbol (keep highest-scoring) ---
        best: dict[str, Signal] = {}
        for sig in signals:
            s = _score(sig, weights)
            if sig.symbol not in best or s > _score(best[sig.symbol], weights):
                best[sig.symbol] = sig

        # Already have a position? Drop.
        candidates = [
            sig for sym, sig in best.items()
            if not self.portfolio.has_position(sym)
        ]

        # Sort by score descending
        candidates.sort(key=lambda s: _score(s, weights), reverse=True)

        # --- Greedy allocation ---
        allocated:       list[Signal] = []
        remaining_cash   = available_cash
        swing_slots      = MAX_SIGNALS_SWING   - max(0, current_swing   - (max_swing   - MAX_SIGNALS_SWING))
        intraday_slots   = MAX_SIGNALS_INTRA   - max(0, current_intraday - (max_intraday - MAX_SIGNALS_INTRA))
        swing_slots      = max(0, min(swing_slots,   max_swing   - current_swing))
        intraday_slots   = max(0, min(intraday_slots, max_intraday - current_intraday))

        for sig in candidates:
            if remaining_cash <= 0:
                break

            is_intraday = sig.mode == TradingMode.INTRADAY

            # Slot check
            if is_intraday and intraday_slots <= 0:
                logger.debug(f"PortfolioAllocator: {sig.symbol} dropped — intraday slots full")
                continue
            if not is_intraday and swing_slots <= 0:
                logger.debug(f"PortfolioAllocator: {sig.symbol} dropped — swing slots full")
                continue

            # Size estimate: min(signal's pct, max_pos_pct) × total_capital
            size_pct      = min(sig.position_size_pct or max_pos_pct, max_pos_pct)
            est_allocation = total_capital * size_pct

            if est_allocation > remaining_cash:
                # Scale down to what's available
                if remaining_cash < total_capital * 0.01:
                    # Less than 1% left — not worth allocating
                    logger.debug(f"PortfolioAllocator: {sig.symbol} dropped — remaining cash too small")
                    continue
                est_allocation = remaining_cash

            allocated.append(sig)
            remaining_cash -= est_allocation

            if is_intraday:
                intraday_slots -= 1
            else:
                swing_slots -= 1

        deployed_pct = (available_cash - remaining_cash) / total_capital * 100
        logger.info(
            f"PortfolioAllocator: {len(candidates)} candidates → {len(allocated)} allocated | "
            f"deploying ≈{deployed_pct:.1f}% of capital | "
            f"cash after reserve: ₹{remaining_cash:,.0f}"
        )
        return allocated

    def cash_reserve_breached(self) -> bool:
        """True if current cash is below the 10% reserve floor."""
        floor = self.portfolio.total_capital * CASH_RESERVE_PCT
        return self.portfolio.cash < floor

    def available_cash(self) -> float:
        """Cash available to deploy (above the 10% reserve floor)."""
        floor = self.portfolio.total_capital * CASH_RESERVE_PCT
        return max(0.0, self.portfolio.cash - floor)
