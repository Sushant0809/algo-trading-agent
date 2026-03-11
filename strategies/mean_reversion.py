"""
Mean Reversion Strategy: Bollinger Bands + RSI + Volume Spike
Entry: Price at lower BB (2σ), RSI < 30, volume spike 1.5x avg
Exit: Price returns to BB midline or RSI > 50
Stop: Entry − 1.5 × ATR(14)
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from signals.signal_model import Product, Signal, SignalAction, SignalStrength, TradingMode
from strategies.base import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.rsi_oversold = self.params.get("rsi_oversold", 30)
        self.vol_spike = self.params.get("volume_spike_multiplier", 1.5)
        self.atr_mult = self.params.get("atr_stop_multiplier", 1.5)
        self.min_price = self.params.get("min_price", 10.0)

    def generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
    ) -> Optional[Signal]:
        if not self.has_min_bars(df, 30):
            return None

        close = self._last(df, "close")
        bb_lower = self._last(df, "bb_lower")
        bb_mid = self._last(df, "bb_mid")
        rsi = self._last(df, "rsi")
        volume = self._last(df, "volume")
        volume_sma = self._last(df, "volume_sma")
        atr = self._last(df, "atr")

        if any(math.isnan(x) for x in [close, bb_lower, bb_mid, rsi, atr]):
            return None

        if close < self.min_price:
            return None

        # Core conditions
        at_lower_band = close <= bb_lower * 1.005  # within 0.5% of lower band
        rsi_oversold = rsi < self.rsi_oversold
        volume_spike = (
            not math.isnan(volume_sma)
            and volume_sma > 0
            and volume >= volume_sma * self.vol_spike
        )

        if not (at_lower_band and rsi_oversold and volume_spike):
            return None

        # Risk levels
        stop = round(close - self.atr_mult * atr, 2)
        target = round(bb_mid, 2)  # Exit at midline

        if target <= close:  # Degenerate case
            return None

        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            strategy=self.name,
            mode=mode,
            product=Product.MIS if mode == TradingMode.INTRADAY else Product.CNC,
            entry_price=close,
            stop_loss=stop,
            target=target,
            strength=SignalStrength.STRONG if rsi < 25 else SignalStrength.MODERATE,
            confidence=0.65,
            reasoning=(
                f"BB lower touch: close={close:.2f} ≤ bb_lower={bb_lower:.2f}, "
                f"RSI={rsi:.1f} (oversold), "
                f"Volume spike {volume/volume_sma:.1f}x (need {self.vol_spike}x), "
                f"Stop={stop:.2f}, Target={target:.2f} (BB midline)"
            ),
            indicators={
                "bb_lower": bb_lower, "bb_mid": bb_mid, "rsi": rsi,
                "volume_ratio": round(volume / volume_sma, 2) if volume_sma else None,
                "atr": atr,
            },
        )
