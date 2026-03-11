"""
Abstract base class for all trading strategies.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from signals.signal_model import Signal, TradingMode


class BaseStrategy(ABC):
    """All strategies must implement this interface."""

    name: str = "base"

    def __init__(self, params: dict | None = None):
        self.params = params or {}

    @abstractmethod
    def generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
    ) -> Optional[Signal]:
        """
        Analyze OHLCV + indicator data and return a Signal or None.
        df: DataFrame with OHLCV + computed indicators (from signals/indicators.py)
        mode: TradingMode.INTRADAY or TradingMode.SWING
        """
        ...

    def _last(self, df: pd.DataFrame, col: str) -> float:
        """Safely get the last value of a column."""
        if col not in df.columns:
            return float("nan")
        val = df[col].iloc[-1]
        return float(val) if pd.notna(val) else float("nan")

    def _prev(self, df: pd.DataFrame, col: str, n: int = 2) -> float:
        """Get the n-th from last value of a column."""
        if col not in df.columns or len(df) < n:
            return float("nan")
        val = df[col].iloc[-n]
        return float(val) if pd.notna(val) else float("nan")

    def has_min_bars(self, df: pd.DataFrame, min_bars: int = 50) -> bool:
        return len(df) >= min_bars
