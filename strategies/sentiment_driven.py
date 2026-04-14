"""
Sentiment-Driven Strategy: Uses Claude adjusted sentiment scores from NSE/BSE news.

Long signals  (adjusted_score ≥ +7):
  - Swing (CNC):   price above EMA(50), price move < 3%
  - Intraday (MIS): mode=INTRADAY override; tighter stop (1× ATR)

Short signals (adjusted_score ≤ -7):
  - Swing (CNC):   price below EMA(50), price move < 3%
  - Intraday (MIS): mode=INTRADAY override; tighter stop (1× ATR)
  Note: equity short selling is intraday only (MIS) on NSE; CNC short is only
  available via F&O. The strategy enforces MIS for all short signals.

Position size scales with |adjusted_score|:
  score 7 → 70% of base, score 10 → 100% of base (base = 2% of capital)
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
        self.long_threshold  = self.params.get("long_sentiment_threshold",  7.0)
        self.short_threshold = self.params.get("short_sentiment_threshold", -7.0)
        self.max_price_move_pct = self.params.get("max_price_move_pct", 3.0)
        self.score_factor    = self.params.get("score_to_size_factor", 0.1)
        self.trend_ema       = self.params.get("trend_ema_period", 50)
        self.atr_mult_swing  = 1.5   # stop multiplier for swing trades
        self.atr_mult_intra  = 1.0   # tighter stop for intraday

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
        sentiment_score: float | None = None,   # raw score (kept for back-compat)
        adjusted_score: float | None = None,    # staleness/credibility-adjusted score
        sentiment_reasoning: str = "",
    ) -> Optional[Signal]:
        """
        Generate a long or short signal based on adjusted sentiment score.

        Callers should pass `adjusted_score` (from SentimentAgent result dict).
        If only `sentiment_score` is provided, it is used as the effective score.
        """
        # Prefer adjusted_score; fall back to raw sentiment_score
        effective_score = adjusted_score if adjusted_score is not None else sentiment_score
        if effective_score is None:
            return None

        if not self.has_min_bars(df, 60):
            return None

        if effective_score >= self.long_threshold:
            return self._long_signal(symbol, df, mode, effective_score, sentiment_reasoning)
        elif effective_score <= self.short_threshold:
            return self._short_signal(symbol, df, mode, effective_score, sentiment_reasoning)
        return None

    # ------------------------------------------------------------------
    # Long signal
    # ------------------------------------------------------------------

    def _long_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
        score: float,
        reasoning: str,
    ) -> Optional[Signal]:
        close     = self._last(df, "close")
        prev_close = self._prev(df, "close", 2)
        ema_trend = self._last(df, f"ema_{self.trend_ema}")
        atr       = self._last(df, "atr")

        if any(math.isnan(x) for x in [close, atr]):
            return None

        price_move_pct = 0.0
        if not math.isnan(prev_close) and prev_close > 0:
            price_move_pct = abs((close - prev_close) / prev_close) * 100
            if price_move_pct > self.max_price_move_pct:
                return None

        # Trend filter: price must be above EMA(50)
        if not math.isnan(ema_trend) and close < ema_trend:
            return None

        product, atr_mult = self._product_and_mult(mode)
        stop   = round(close - atr_mult * atr, 2)
        target = round(close + 2 * (close - stop), 2)

        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            strategy=self.name,
            mode=mode,
            product=product,
            entry_price=close,
            stop_loss=stop,
            target=target,
            position_size_pct=self._size(score),
            sentiment_score=score,
            strength=SignalStrength.STRONG if score >= 9 else SignalStrength.MODERATE,
            confidence=round(min(score / 10.0, 1.0), 2),
            reasoning=(
                f"LONG | adj_score={score:+.2f}/10 (≥{self.long_threshold} threshold). "
                f"{reasoning} "
                f"Price move={price_move_pct:.1f}% (≤{self.max_price_move_pct}%). "
                f"Product={product.value} Stop={stop:.2f} Target={target:.2f}"
            ),
            indicators={
                "adjusted_sentiment_score": score,
                "close": close,
                "ema_trend": ema_trend,
                "atr": atr,
            },
        )

    # ------------------------------------------------------------------
    # Short signal
    # ------------------------------------------------------------------

    def _short_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
        score: float,
        reasoning: str,
    ) -> Optional[Signal]:
        close     = self._last(df, "close")
        prev_close = self._prev(df, "close", 2)
        ema_trend = self._last(df, f"ema_{self.trend_ema}")
        atr       = self._last(df, "atr")

        if any(math.isnan(x) for x in [close, atr]):
            return None

        price_move_pct = 0.0
        if not math.isnan(prev_close) and prev_close > 0:
            price_move_pct = abs((close - prev_close) / prev_close) * 100
            if price_move_pct > self.max_price_move_pct:
                return None

        # Trend filter: price must be below EMA(50) for short
        if not math.isnan(ema_trend) and close > ema_trend:
            return None

        # Equity short selling in India is intraday only (MIS) — enforce this
        # regardless of what mode the caller requested.
        product  = Product.MIS
        atr_mult = self.atr_mult_intra   # tighter stop for short intraday

        stop   = round(close + atr_mult * atr, 2)   # stop above entry for short
        target = round(close - 2 * (stop - close), 2)

        abs_score = abs(score)

        return Signal(
            symbol=symbol,
            action=SignalAction.SELL,
            strategy=self.name,
            mode=TradingMode.INTRADAY,   # always intraday for equity shorts
            product=product,
            entry_price=close,
            stop_loss=stop,
            target=target,
            position_size_pct=self._size(abs_score),
            sentiment_score=score,
            strength=SignalStrength.STRONG if abs_score >= 9 else SignalStrength.MODERATE,
            confidence=round(min(abs_score / 10.0, 1.0), 2),
            reasoning=(
                f"SHORT (MIS intraday) | adj_score={score:+.2f}/10 (≤{self.short_threshold} threshold). "
                f"{reasoning} "
                f"Price move={price_move_pct:.1f}% (≤{self.max_price_move_pct}%). "
                f"Product=MIS Stop={stop:.2f} Target={target:.2f}"
            ),
            indicators={
                "adjusted_sentiment_score": score,
                "close": close,
                "ema_trend": ema_trend,
                "atr": atr,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _product_and_mult(self, mode: TradingMode) -> tuple[Product, float]:
        if mode == TradingMode.INTRADAY:
            return Product.MIS, self.atr_mult_intra
        return Product.CNC, self.atr_mult_swing

    def _size(self, abs_score: float) -> float:
        """Position size: 2% base × (score × 0.1). Score 7→1.4%, 10→2%."""
        base = 0.02
        scalar = abs(abs_score) * self.score_factor   # 7→0.7, 10→1.0
        return round(base * scalar, 4)
