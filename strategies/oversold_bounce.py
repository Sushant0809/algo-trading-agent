"""
Oversold Bounce Strategy: RSI < 30 + MACD turning up + price above key support.

This is the core hackathon strategy rule brought into production:
  RSI < 30  = oversold → good to buy
  RSI > 70  = overbought → skip (handled by MomentumStrategy exit logic)
  MACD turning from negative → positive = trend reversal confirmation

Entry conditions:
  1. RSI(14) < 30 (oversold zone)
  2. MACD histogram turning up: current hist > previous hist (momentum shift)
  3. Price ≥ BB lower band (not in free fall — has support)
  4. Price > EMA(200) or EMA(200) missing (don't buy falling knives in downtrend)
  5. Volume ratio > 0.8 (some participation — avoids dead stocks)

Exit levels:
  Stop:   Entry − 1.5 × ATR (hard stop below entry)
  Target: EMA(20) — RSI bounces typically mean-revert to moving average

Position size:
  Scales inversely with RSI depth:
    RSI 25–30 → smaller size (mild oversold, could go lower)
    RSI < 20  → larger size (deeply oversold, higher bounce probability)
  Base 2% of capital; scalar = (30 - RSI) / 30 capped at 1.0
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from signals.signal_model import Product, Signal, SignalAction, SignalStrength, TradingMode
from strategies.base import BaseStrategy


class OversoldBounceStrategy(BaseStrategy):
    name = "oversold_bounce"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.rsi_threshold   = self.params.get("rsi_oversold_threshold", 30.0)
        self.min_vol_ratio   = self.params.get("min_volume_ratio", 0.8)
        self.atr_stop_mult   = self.params.get("atr_stop_multiplier", 1.5)
        self.require_ema200  = self.params.get("require_above_ema200", True)

    def generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
    ) -> Optional[Signal]:
        if not self.has_min_bars(df, 60):
            return None

        close        = self._last(df, "close")
        rsi          = self._last(df, "rsi")
        macd_hist    = self._last(df, "macd_hist")
        macd_hist_p  = self._prev(df, "macd_hist")
        bb_lower     = self._last(df, "bb_lower")
        ema_200      = self._last(df, "ema_200")
        ema_20       = self._last(df, "ema_20")
        atr          = self._last(df, "atr")
        vol_ratio    = self._last(df, "volume_ratio")

        if any(math.isnan(x) for x in [close, rsi, atr]):
            return None

        # --- Core conditions ---
        # 1. RSI oversold
        if rsi >= self.rsi_threshold:
            return None

        # 2. MACD histogram turning up (momentum shift)
        macd_turning_up = (
            not math.isnan(macd_hist) and
            not math.isnan(macd_hist_p) and
            macd_hist > macd_hist_p
        )
        if not macd_turning_up:
            return None

        # 3. Price at or above BB lower band (has support, not in free fall)
        if not math.isnan(bb_lower) and close < bb_lower * 0.98:
            return None

        # 4. Long-term trend: price should be above EMA(200) if available
        if self.require_ema200 and not math.isnan(ema_200) and close < ema_200:
            return None

        # 5. Volume participation
        if not math.isnan(vol_ratio) and vol_ratio < self.min_vol_ratio:
            return None

        # --- Risk levels ---
        stop   = round(close - self.atr_stop_mult * atr, 2)
        # Target: EMA(20) if available (typical RSI bounce mean-reversion target)
        if not math.isnan(ema_20) and ema_20 > close:
            target = round(ema_20, 2)
        else:
            target = round(close + 2 * (close - stop), 2)

        # Validate R:R
        risk   = close - stop
        reward = target - close
        if risk <= 0 or reward / risk < 1.5:
            return None

        # --- Sizing: deeper oversold = larger position ---
        depth      = max(0.0, self.rsi_threshold - rsi)  # e.g. RSI=22 → depth=8
        size_scalar = min(depth / self.rsi_threshold, 1.0)  # 0.0–1.0
        position_size_pct = round(0.02 * (0.5 + 0.5 * size_scalar), 4)  # 1%–2%

        # --- Strength ---
        if rsi < 20:
            strength   = SignalStrength.STRONG
            confidence = 0.72
        elif rsi < 25:
            strength   = SignalStrength.MODERATE
            confidence = 0.62
        else:
            strength   = SignalStrength.WEAK
            confidence = 0.52

        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            strategy=self.name,
            mode=mode,
            product=Product.MIS if mode == TradingMode.INTRADAY else Product.CNC,
            entry_price=close,
            stop_loss=stop,
            target=target,
            position_size_pct=position_size_pct,
            strength=strength,
            confidence=confidence,
            reasoning=(
                f"Oversold bounce: RSI={rsi:.1f} (<{self.rsi_threshold}), "
                f"MACD hist turning up ({macd_hist_p:.4f}→{macd_hist:.4f}), "
                f"Vol ratio={vol_ratio:.2f}, "
                f"Stop={stop:.2f}, Target={target:.2f} (R:R={reward/risk:.1f})"
            ),
            indicators={
                "rsi": rsi,
                "macd_hist": macd_hist,
                "macd_hist_prev": macd_hist_p,
                "bb_lower": bb_lower,
                "atr": atr,
                "volume_ratio": vol_ratio,
            },
        )
