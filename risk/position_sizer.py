"""
Position sizing: fixed-fraction, volatility-ATR, and half-Kelly methods.
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


class PositionSizer:
    def __init__(
        self,
        max_position_pct: float = 0.05,
        atr_stop_mult: float = 1.5,
        max_risk_per_trade_pct: float = 0.01,
    ):
        self.max_position_pct = max_position_pct     # Max 5% per position
        self.atr_stop_mult = atr_stop_mult
        self.max_risk_per_trade_pct = max_risk_per_trade_pct  # Max 1% capital risk/trade

    def fixed_fraction(
        self,
        capital: float,
        fraction_pct: float,
        price: float,
        lot_size: int = 1,
    ) -> int:
        """Fixed fraction of capital. Returns number of shares."""
        if price <= 0:
            return 0
        max_capital = capital * min(fraction_pct, self.max_position_pct)
        qty = int(max_capital / price)
        return max(0, qty - (qty % lot_size))

    def volatility_atr(
        self,
        capital: float,
        price: float,
        atr: float,
        stop_mult: float | None = None,
        lot_size: int = 1,
    ) -> int:
        """
        Risk-based sizing: risk_capital / stop_distance.
        Stop = price − (stop_mult × ATR).
        """
        if price <= 0 or atr <= 0:
            return 0
        mult = stop_mult or self.atr_stop_mult
        stop_distance = mult * atr
        risk_capital = capital * self.max_risk_per_trade_pct
        qty = int(risk_capital / stop_distance)
        # Cap at max_position_pct
        max_qty = int(capital * self.max_position_pct / price)
        qty = min(qty, max_qty)
        return max(0, qty - (qty % lot_size))

    def half_kelly(
        self,
        capital: float,
        price: float,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        lot_size: int = 1,
    ) -> int:
        """
        Half-Kelly criterion for sizing.
        f = (win_rate / avg_loss - (1 - win_rate) / avg_win) / 2
        """
        if avg_win <= 0 or avg_loss <= 0 or price <= 0:
            return 0
        b = avg_win / avg_loss
        kelly_f = (win_rate * b - (1 - win_rate)) / b
        half_kelly_f = max(0, kelly_f / 2)
        capped_f = min(half_kelly_f, self.max_position_pct)
        qty = int(capital * capped_f / price)
        return max(0, qty - (qty % lot_size))

    def size_signal(
        self,
        capital: float,
        price: float,
        signal_size_pct: float,
        atr: float | None = None,
        lot_size: int = 1,
    ) -> tuple[int, float]:
        """
        Primary sizing method: use signal's position_size_pct if given,
        ATR-based if ATR available, else fixed fraction.
        Returns (qty, capital_allocated).
        """
        if atr and atr > 0:
            qty = self.volatility_atr(capital, price, atr, lot_size=lot_size)
        else:
            qty = self.fixed_fraction(capital, signal_size_pct, price, lot_size)

        capital_allocated = qty * price
        return qty, capital_allocated
