"""
Overbought Short Strategy: RSI > 75 + MACD turning down + price below EMA(20).

Mirror of OversoldBounceStrategy on the short side.

Entry conditions:
  1. RSI(14) > 75  (overbought zone)
  2. MACD histogram turning DOWN: current hist < previous hist (momentum fading)
  3. Price ≤ EMA(20) — short-term trend already rolling over
  4. Price ≤ BB upper band × 1.02 — not in a parabolic blow-off (those can run further)
  5. Volume ratio > 0.8 — some participation confirms the rejection

Product: always MIS (intraday) — equity short selling in India is intraday-only.
Mode:    always INTRADAY regardless of what the caller passes.

Exit levels:
  Stop:   Entry + 1.5 × ATR  (hard stop above entry)
  Target: EMA(50) — overbought stocks typically mean-revert to the 50-day average

Position sizing:
  Scales with RSI depth above 75:
    RSI 75–80 → smaller size (mildly overbought)
    RSI > 85  → larger size (severely overbought, higher reversal probability)
  Base 2% of capital; scalar = (RSI - 75) / 25 capped at 1.0 → 1%–2%
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from signals.signal_model import Product, Signal, SignalAction, SignalStrength, TradingMode
from strategies.base import BaseStrategy


class OverboughtShortStrategy(BaseStrategy):
    name = "overbought_short"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.rsi_threshold  = self.params.get("rsi_overbought_threshold", 75.0)
        self.min_vol_ratio  = self.params.get("min_volume_ratio", 0.8)
        self.atr_stop_mult  = self.params.get("atr_stop_multiplier", 1.5)
        self.bb_upper_slack = self.params.get("bb_upper_slack", 1.02)  # allow 2% above BB upper

    def generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
    ) -> Optional[Signal]:
        if not self.has_min_bars(df, 60):
            return None

        close       = self._last(df, "close")
        rsi         = self._last(df, "rsi")
        macd_hist   = self._last(df, "macd_hist")
        macd_hist_p = self._prev(df, "macd_hist")
        ema_20      = self._last(df, "ema_20")
        ema_50      = self._last(df, "ema_50")
        bb_upper    = self._last(df, "bb_upper")
        atr         = self._last(df, "atr")
        vol_ratio   = self._last(df, "volume_ratio")

        if any(math.isnan(x) for x in [close, rsi, atr]):
            return None

        # 1. RSI overbought
        if rsi <= self.rsi_threshold:
            return None

        # 2. MACD histogram turning down (momentum fading)
        macd_turning_down = (
            not math.isnan(macd_hist) and
            not math.isnan(macd_hist_p) and
            macd_hist < macd_hist_p
        )
        if not macd_turning_down:
            return None

        # 3. Price at or below EMA(20) — trend already rolling over
        if not math.isnan(ema_20) and close > ema_20:
            return None

        # 4. Not in parabolic blow-off (price too far above BB upper = don't short yet)
        if not math.isnan(bb_upper) and close > bb_upper * self.bb_upper_slack:
            return None

        # 5. Volume participation
        if not math.isnan(vol_ratio) and vol_ratio < self.min_vol_ratio:
            return None

        # --- Risk levels ---
        stop = round(close + self.atr_stop_mult * atr, 2)   # stop ABOVE entry for short

        # Target: EMA(50) if below current price, else 2R below
        if not math.isnan(ema_50) and ema_50 < close:
            target = round(ema_50, 2)
        else:
            target = round(close - 2 * (stop - close), 2)

        # Validate R:R ≥ 1.5:1
        risk   = stop - close
        reward = close - target
        if risk <= 0 or reward / risk < 1.5:
            return None

        # --- Sizing: deeper overbought = larger position ---
        depth       = max(0.0, rsi - self.rsi_threshold)      # RSI=82 → depth=7
        size_scalar = min(depth / 25.0, 1.0)                  # 0.0–1.0
        position_size_pct = round(0.02 * (0.5 + 0.5 * size_scalar), 4)  # 1%–2%

        # --- Strength ---
        if rsi > 85:
            strength   = SignalStrength.STRONG
            confidence = 0.70
        elif rsi > 80:
            strength   = SignalStrength.MODERATE
            confidence = 0.61
        else:
            strength   = SignalStrength.WEAK
            confidence = 0.52

        return Signal(
            symbol=symbol,
            action=SignalAction.SELL,
            strategy=self.name,
            mode=TradingMode.INTRADAY,    # equity short = intraday only in India
            product=Product.MIS,
            entry_price=close,
            stop_loss=stop,
            target=target,
            position_size_pct=position_size_pct,
            strength=strength,
            confidence=confidence,
            reasoning=(
                f"Overbought short (MIS): RSI={rsi:.1f} (>{self.rsi_threshold}), "
                f"MACD hist turning down ({macd_hist_p:.4f}→{macd_hist:.4f}), "
                f"price={close:.2f} ≤ EMA(20)={ema_20:.2f}, "
                f"Vol ratio={vol_ratio:.2f}, "
                f"Stop={stop:.2f}, Target={target:.2f} (R:R={reward/risk:.1f})"
            ),
            indicators={
                "rsi": rsi,
                "macd_hist": macd_hist,
                "macd_hist_prev": macd_hist_p,
                "ema_20": ema_20,
                "bb_upper": bb_upper,
                "atr": atr,
                "volume_ratio": vol_ratio,
            },
        )
