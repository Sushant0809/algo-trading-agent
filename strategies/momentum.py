"""
Momentum Strategy: EMA stack + RSI + MACD + VWAP
Entry:
  - EMA(20) > EMA(50) > EMA(200) (bullish EMA stack)
  - RSI 50–70 (momentum zone, not overbought)
  - Price above VWAP (intraday)
  - MACD histogram expanding (positive and increasing)
  - Swing extra: ADX > 25 (trend strength)
Stop-loss: Entry − 1.5 × ATR(14)
Target: Entry + 2 × (Entry − Stop) = 2:1 R:R
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from signals.signal_model import Product, Signal, SignalAction, SignalStrength, TradingMode
from strategies.base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.ema_fast = self.params.get("ema_fast", 20)
        self.ema_mid = self.params.get("ema_mid", 50)
        self.ema_slow = self.params.get("ema_slow", 200)
        self.rsi_min = self.params.get("rsi_min", 50)
        self.rsi_max = self.params.get("rsi_max", 70)
        self.adx_min = self.params.get("adx_min_swing", 25)
        self.atr_stop_mult = self.params.get("atr_stop_multiplier", 1.5)

    def generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
    ) -> Optional[Signal]:
        if not self.has_min_bars(df, 210):  # Need 200+ bars for EMA(200)
            return None

        close = self._last(df, "close")
        ema_fast = self._last(df, f"ema_{self.ema_fast}")
        ema_mid = self._last(df, f"ema_{self.ema_mid}")
        ema_slow = self._last(df, f"ema_{self.ema_slow}")
        rsi = self._last(df, "rsi")
        macd_hist = self._last(df, "macd_hist")
        macd_hist_prev = self._prev(df, "macd_hist")
        atr = self._last(df, "atr")

        if any(math.isnan(x) for x in [close, ema_fast, ema_mid, ema_slow, rsi, atr]):
            return None

        # Core conditions
        ema_stack = ema_fast > ema_mid > ema_slow
        rsi_ok = self.rsi_min <= rsi <= self.rsi_max
        macd_expanding = macd_hist > 0 and macd_hist > macd_hist_prev

        if not (ema_stack and rsi_ok and macd_expanding):
            return None

        # Mode-specific filters
        if mode == TradingMode.INTRADAY:
            vwap = self._last(df, "vwap")
            if not math.isnan(vwap) and close < vwap:
                return None
        elif mode == TradingMode.SWING:
            adx = self._last(df, "adx")
            if not math.isnan(adx) and adx < self.adx_min:
                return None

        # Risk levels
        stop = round(close - self.atr_stop_mult * atr, 2)
        target = round(close + 2 * (close - stop), 2)

        # Strength
        strength = SignalStrength.STRONG if rsi > 60 and adx_ok(df, self.adx_min) else SignalStrength.MODERATE

        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            strategy=self.name,
            mode=mode,
            product=Product.MIS if mode == TradingMode.INTRADAY else Product.CNC,
            entry_price=close,
            stop_loss=stop,
            target=target,
            strength=strength,
            confidence=0.7 if strength == SignalStrength.STRONG else 0.55,
            reasoning=(
                f"EMA stack: {ema_fast:.1f}>{ema_mid:.1f}>{ema_slow:.1f}, "
                f"RSI={rsi:.1f}, MACD hist expanding={macd_hist:.4f}, "
                f"ATR={atr:.2f}, Stop={stop}, Target={target}"
            ),
            indicators={
                "ema_fast": ema_fast, "ema_mid": ema_mid, "ema_slow": ema_slow,
                "rsi": rsi, "macd_hist": macd_hist, "atr": atr,
            },
        )


def adx_ok(df: pd.DataFrame, min_adx: float) -> bool:
    if "adx" not in df.columns:
        return True
    val = df["adx"].iloc[-1]
    return float(val) >= min_adx if pd.notna(val) else True
