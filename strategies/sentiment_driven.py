"""
Sentiment-Driven Strategy: Uses Claude sentiment scores from NSE news.
Entry: Sentiment score ≥ 7 (scale -10 to +10) AND price not already moved > 3%
Position size scales with score: 7 → 70% of normal, 10 → 100%
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from signals.signal_model import Product, Signal, SignalAction, SignalStrength, TradingMode
from strategies.base import BaseStrategy


class SentimentDrivenStrategy(BaseStrategy):
    name = "sentiment_driven"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.min_score = self.params.get("min_sentiment_score", 7)
        self.max_price_move_pct = self.params.get("max_price_move_pct", 3.0)
        self.score_factor = self.params.get("score_to_size_factor", 0.1)
        self.trend_ema = self.params.get("trend_ema_period", 50)
        self.atr_mult = 1.5

    def generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
        sentiment_score: float | None = None,
        sentiment_reasoning: str = "",
    ) -> Optional[Signal]:
        """
        Generate a signal based on sentiment score and price action.
        sentiment_score: float from -10 to +10, provided by SentimentAgent.
        """
        if sentiment_score is None:
            return None

        if sentiment_score < self.min_score:
            return None

        if not self.has_min_bars(df, 60):
            return None

        close = self._last(df, "close")
        prev_close = self._prev(df, "close", 2)  # Yesterday's close
        ema_trend = self._last(df, f"ema_{self.trend_ema}")
        atr = self._last(df, "atr")

        if any(math.isnan(x) for x in [close, atr]):
            return None

        # Don't enter if price already moved > 3% (news already priced in)
        if not math.isnan(prev_close) and prev_close > 0:
            price_move_pct = abs((close - prev_close) / prev_close) * 100
            if price_move_pct > self.max_price_move_pct:
                return None

        # Trend filter: price above EMA(50)
        if not math.isnan(ema_trend) and close < ema_trend:
            return None

        # Size scales with sentiment score
        base_size_pct = 0.02  # 2% base
        size_scalar = sentiment_score * self.score_factor  # 7 → 0.7, 10 → 1.0
        position_size_pct = round(base_size_pct * size_scalar, 4)

        stop = round(close - self.atr_mult * atr, 2)
        target = round(close + 2 * (close - stop), 2)

        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            strategy=self.name,
            mode=mode,
            product=Product.CNC,  # Sentiment trades are swing (overnight)
            entry_price=close,
            stop_loss=stop,
            target=target,
            position_size_pct=position_size_pct,
            sentiment_score=sentiment_score,
            strength=SignalStrength.STRONG if sentiment_score >= 9 else SignalStrength.MODERATE,
            confidence=round(min(sentiment_score / 10.0, 1.0), 2),
            reasoning=(
                f"Sentiment score={sentiment_score}/10 (≥{self.min_score} threshold). "
                f"{sentiment_reasoning} "
                f"Price move={price_move_pct:.1f}% (max {self.max_price_move_pct}%). "
                f"Size={position_size_pct*100:.1f}% of capital. "
                f"Stop={stop:.2f}, Target={target:.2f}"
            ),
            indicators={
                "sentiment_score": sentiment_score,
                "close": close,
                "ema_trend": ema_trend,
                "atr": atr,
            },
        )
