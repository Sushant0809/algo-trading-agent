"""
Breakout Strategy: Rolling 20-period high/low + volume 2x confirmation
Entry: Price closes above rolling 20-period high with 2x avg volume
       + confirmation on next bar (avoids false breakouts)
Stop: SL-M immediately after entry, placed at (entry − 1.5 × ATR)
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from signals.signal_model import Product, Signal, SignalAction, SignalStrength, TradingMode
from strategies.base import BaseStrategy


class BreakoutStrategy(BaseStrategy):
    name = "breakout"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.lookback = self.params.get("lookback_periods", 20)
        self.vol_mult = self.params.get("volume_multiplier", 2.0)
        self.confirm_bars = self.params.get("confirmation_bars", 1)
        self.atr_mult = self.params.get("atr_stop_multiplier", 1.5)

    def generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
    ) -> Optional[Signal]:
        if not self.has_min_bars(df, self.lookback + self.confirm_bars + 5):
            return None

        close = self._last(df, "close")
        roll_high = self._prev(df, "roll_high", self.confirm_bars + 1)  # Prior period's high
        volume = self._last(df, "volume")
        volume_sma = self._last(df, "volume_sma")
        atr = self._last(df, "atr")

        if any(math.isnan(x) for x in [close, roll_high, atr]):
            return None

        # Breakout condition: current close above the PREVIOUS period's rolling high
        # (already closed above it, so breakout confirmed on prior bar)
        price_broke_out = close > roll_high

        # Volume surge
        vol_ok = (
            not math.isnan(volume_sma)
            and volume_sma > 0
            and volume >= volume_sma * self.vol_mult
        )

        if not (price_broke_out and vol_ok):
            return None

        # Check that prior bar also closed above (confirmation)
        if self.confirm_bars > 0:
            prev_close = self._prev(df, "close", 2)
            prev_roll_high = self._prev(df, "roll_high", 3)
            if math.isnan(prev_close) or math.isnan(prev_roll_high):
                return None
            if prev_close <= prev_roll_high:
                return None  # No confirmation

        stop = round(close - self.atr_mult * atr, 2)
        target = round(close + 2 * (close - stop), 2)

        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            strategy=self.name,
            mode=mode,
            product=Product.MIS if mode == TradingMode.INTRADAY else Product.CNC,
            entry_price=close,
            stop_loss=stop,
            target=target,
            strength=SignalStrength.STRONG,
            confidence=0.68,
            reasoning=(
                f"Breakout: close={close:.2f} > {self.lookback}-period high={roll_high:.2f}, "
                f"Volume {volume/volume_sma:.1f}x (need {self.vol_mult}x), "
                f"ATR={atr:.2f}, Stop={stop:.2f}, Target={target:.2f}"
            ),
            indicators={
                "roll_high": roll_high,
                "volume_ratio": round(volume / volume_sma, 2) if volume_sma else None,
                "atr": atr,
            },
        )
