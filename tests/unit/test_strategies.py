"""
Unit tests for all trading strategies.
Uses synthetic OHLCV DataFrames — no live market data needed.
"""
import math
import pytest
import numpy as np
import pandas as pd

from signals.signal_model import SignalAction, TradingMode, Product
from signals.indicators import compute_all_indicators
from strategies.momentum import MomentumStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.breakout import BreakoutStrategy
from strategies.sentiment_driven import SentimentDrivenStrategy
from strategies.oversold_bounce import OversoldBounceStrategy
from strategies.overbought_short import OverboughtShortStrategy


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

def make_trending_df(n: int = 300, start_price: float = 100.0) -> pd.DataFrame:
    """Strongly uptrending price series."""
    np.random.seed(42)
    prices = [start_price]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + np.random.normal(0.001, 0.01)))
    dates = pd.date_range("2023-01-01", periods=n, freq="B", tz="Asia/Kolkata")
    df = pd.DataFrame({
        "open":   prices,
        "high":   [p * (1 + abs(np.random.normal(0, 0.005))) for p in prices],
        "low":    [p * (1 - abs(np.random.normal(0, 0.005))) for p in prices],
        "close":  prices,
        "volume": [np.random.randint(500_000, 2_000_000) for _ in range(n)],
    }, index=dates)
    return compute_all_indicators(df)


def make_sideways_df(n: int = 200, center: float = 100.0, std: float = 2.0) -> pd.DataFrame:
    """Mean-reverting sideways price series."""
    np.random.seed(123)
    prices = [center + np.random.normal(0, std) for _ in range(n)]
    dates = pd.date_range("2023-01-01", periods=n, freq="B", tz="Asia/Kolkata")
    df = pd.DataFrame({
        "open":   prices,
        "high":   [p + abs(np.random.normal(0, 1)) for p in prices],
        "low":    [p - abs(np.random.normal(0, 1)) for p in prices],
        "close":  prices,
        "volume": [np.random.randint(500_000, 2_000_000) for _ in range(n)],
    }, index=dates)
    return compute_all_indicators(df)


def force_long_conditions(df: pd.DataFrame, close: float = 125.0) -> pd.DataFrame:
    """Force last-row indicator values that satisfy long entry conditions."""
    df = df.copy()
    df.loc[df.index[-1], "ema_20"]    = close * 1.02
    df.loc[df.index[-1], "ema_50"]    = close * 0.95
    df.loc[df.index[-1], "ema_200"]   = close * 0.88
    df.loc[df.index[-1], "rsi"]       = 60.0
    df.loc[df.index[-1], "macd_hist"] = 0.5
    df.loc[df.index[-2], "macd_hist"] = 0.3
    df.loc[df.index[-1], "close"]     = close
    df.loc[df.index[-1], "atr"]       = 2.0
    df.loc[df.index[-1], "adx"]       = 30.0
    df.loc[df.index[-1], "vwap"]      = close - 1  # price above VWAP
    return df


# ---------------------------------------------------------------------------
# MomentumStrategy
# ---------------------------------------------------------------------------

class TestMomentumStrategy:
    def test_buy_signal_on_ema_stack(self):
        df = force_long_conditions(make_trending_df(300))
        sig = MomentumStrategy().generate_signal("RELIANCE", df, TradingMode.SWING)
        assert sig is not None
        assert sig.action == SignalAction.BUY
        assert sig.stop_loss < sig.entry_price
        assert sig.target > sig.entry_price

    def test_rr_at_least_2_to_1(self):
        df = force_long_conditions(make_trending_df(300))
        sig = MomentumStrategy().generate_signal("RELIANCE", df, TradingMode.SWING)
        if sig:
            risk   = sig.entry_price - sig.stop_loss
            reward = sig.target - sig.entry_price
            assert reward >= 1.9 * risk  # ~2:1

    def test_no_signal_when_rsi_overbought(self):
        df = force_long_conditions(make_trending_df(300))
        df.loc[df.index[-1], "rsi"] = 75.0   # > 70 — overbought
        sig = MomentumStrategy().generate_signal("TCS", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_when_ema_stack_broken(self):
        df = force_long_conditions(make_trending_df(300))
        df.loc[df.index[-1], "ema_20"] = 100.0   # ema_20 < ema_50
        df.loc[df.index[-1], "ema_50"] = 110.0
        sig = MomentumStrategy().generate_signal("INFY", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_below_min_bars(self):
        df = make_trending_df(n=50)
        sig = MomentumStrategy().generate_signal("HDFCBANK", df, TradingMode.INTRADAY)
        assert sig is None

    def test_intraday_uses_mis_product(self):
        df = force_long_conditions(make_trending_df(300))
        sig = MomentumStrategy().generate_signal("RELIANCE", df, TradingMode.INTRADAY)
        if sig:
            assert sig.product == Product.MIS

    def test_swing_uses_cnc_product(self):
        df = force_long_conditions(make_trending_df(300))
        sig = MomentumStrategy().generate_signal("RELIANCE", df, TradingMode.SWING)
        if sig:
            assert sig.product == Product.CNC


# ---------------------------------------------------------------------------
# MeanReversionStrategy
# ---------------------------------------------------------------------------

class TestMeanReversionStrategy:
    def _oversold_df(self) -> pd.DataFrame:
        df = make_sideways_df(150)
        df.loc[df.index[-1], "rsi"]        = 25.0
        df.loc[df.index[-1], "bb_lower"]   = 95.0
        df.loc[df.index[-1], "bb_mid"]     = 100.0
        df.loc[df.index[-1], "close"]      = 95.2
        df.loc[df.index[-1], "volume"]     = 2_000_000
        df.loc[df.index[-1], "volume_sma"] = 1_000_000
        df.loc[df.index[-1], "atr"]        = 1.5
        return df

    def test_buy_signal_on_oversold(self):
        sig = MeanReversionStrategy().generate_signal("WIPRO", self._oversold_df(), TradingMode.SWING)
        assert sig is not None
        assert sig.action == SignalAction.BUY
        assert sig.target == round(self._oversold_df().loc[self._oversold_df().index[-1], "bb_mid"], 2)

    def test_no_signal_without_volume_spike(self):
        df = self._oversold_df()
        df.loc[df.index[-1], "volume"] = 800_000   # 0.8x — below 1.5x threshold
        sig = MeanReversionStrategy().generate_signal("WIPRO", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_when_rsi_above_threshold(self):
        df = self._oversold_df()
        df.loc[df.index[-1], "rsi"] = 35.0   # above 30 threshold
        sig = MeanReversionStrategy().generate_signal("WIPRO", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_price_not_at_band(self):
        df = self._oversold_df()
        df.loc[df.index[-1], "close"]    = 105.0   # well above bb_lower
        df.loc[df.index[-1], "bb_lower"] = 95.0
        sig = MeanReversionStrategy().generate_signal("WIPRO", df, TradingMode.SWING)
        assert sig is None


# ---------------------------------------------------------------------------
# BreakoutStrategy
# ---------------------------------------------------------------------------

class TestBreakoutStrategy:
    def _breakout_df(self) -> pd.DataFrame:
        df = make_trending_df(100)
        df.loc[df.index[-1], "close"]      = 151.0
        df.loc[df.index[-2], "close"]      = 150.5   # confirmation bar
        df.loc[df.index[-1], "roll_high"]  = 150.0
        df.loc[df.index[-2], "roll_high"]  = 150.0
        df.loc[df.index[-3], "roll_high"]  = 149.0
        df.loc[df.index[-1], "volume"]     = 3_000_000
        df.loc[df.index[-1], "volume_sma"] = 1_000_000
        df.loc[df.index[-1], "atr"]        = 2.0
        return df

    def test_breakout_buy_signal(self):
        sig = BreakoutStrategy().generate_signal("BAJFINANCE", self._breakout_df(), TradingMode.SWING)
        assert sig is not None
        assert sig.action == SignalAction.BUY

    def test_no_signal_without_volume(self):
        df = self._breakout_df()
        df.loc[df.index[-1], "volume"] = 1_200_000   # 1.2x, need 2x
        sig = BreakoutStrategy().generate_signal("BAJFINANCE", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_without_price_breakout(self):
        df = self._breakout_df()
        df.loc[df.index[-1], "close"]     = 149.5   # below roll_high
        df.loc[df.index[-1], "roll_high"] = 150.0
        sig = BreakoutStrategy().generate_signal("BAJFINANCE", df, TradingMode.SWING)
        assert sig is None


# ---------------------------------------------------------------------------
# OversoldBounceStrategy
# ---------------------------------------------------------------------------

class TestOversoldBounceStrategy:
    def _oversold_bounce_df(self, rsi: float = 22.0) -> pd.DataFrame:
        df = make_trending_df(200)
        close = 100.0
        df.loc[df.index[-1], "close"]      = close
        df.loc[df.index[-1], "rsi"]        = rsi
        df.loc[df.index[-1], "macd_hist"]  = -0.1   # negative but turning up
        df.loc[df.index[-2], "macd_hist"]  = -0.3   # prev was more negative
        df.loc[df.index[-1], "bb_lower"]   = 98.0   # price above lower band
        df.loc[df.index[-1], "ema_20"]     = 105.0  # target
        df.loc[df.index[-1], "ema_200"]    = 90.0   # price above 200
        df.loc[df.index[-1], "atr"]        = 1.5
        df.loc[df.index[-1], "volume_ratio"] = 1.2
        return df

    def test_buy_signal_when_oversold(self):
        sig = OversoldBounceStrategy().generate_signal("SBIN", self._oversold_bounce_df(), TradingMode.SWING)
        assert sig is not None
        assert sig.action == SignalAction.BUY

    def test_no_signal_when_rsi_above_threshold(self):
        df = self._oversold_bounce_df(rsi=35.0)
        sig = OversoldBounceStrategy().generate_signal("SBIN", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_when_macd_not_turning_up(self):
        df = self._oversold_bounce_df()
        df.loc[df.index[-1], "macd_hist"]  = -0.5   # worse than prev
        df.loc[df.index[-2], "macd_hist"]  = -0.3
        sig = OversoldBounceStrategy().generate_signal("SBIN", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_in_free_fall(self):
        df = self._oversold_bounce_df()
        df.loc[df.index[-1], "close"]    = 90.0    # well below bb_lower
        df.loc[df.index[-1], "bb_lower"] = 95.0
        sig = OversoldBounceStrategy().generate_signal("SBIN", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_below_ema200(self):
        df = self._oversold_bounce_df()
        df.loc[df.index[-1], "ema_200"] = 110.0   # price 100 < ema200 110
        sig = OversoldBounceStrategy().generate_signal("SBIN", df, TradingMode.SWING)
        assert sig is None

    def test_stop_below_entry(self):
        df = self._oversold_bounce_df()
        sig = OversoldBounceStrategy().generate_signal("SBIN", df, TradingMode.SWING)
        if sig:
            assert sig.stop_loss < sig.entry_price

    def test_rr_at_least_1_5(self):
        df = self._oversold_bounce_df()
        sig = OversoldBounceStrategy().generate_signal("SBIN", df, TradingMode.SWING)
        if sig:
            risk   = sig.entry_price - sig.stop_loss
            reward = sig.target - sig.entry_price
            assert reward / risk >= 1.5

    def test_deeper_rsi_gives_larger_size(self):
        sig_mild = OversoldBounceStrategy().generate_signal(
            "X", self._oversold_bounce_df(rsi=28.0), TradingMode.SWING
        )
        sig_deep = OversoldBounceStrategy().generate_signal(
            "X", self._oversold_bounce_df(rsi=18.0), TradingMode.SWING
        )
        if sig_mild and sig_deep:
            assert sig_deep.position_size_pct >= sig_mild.position_size_pct


# ---------------------------------------------------------------------------
# OverboughtShortStrategy
# ---------------------------------------------------------------------------

class TestOverboughtShortStrategy:
    def _overbought_df(self, rsi: float = 82.0) -> pd.DataFrame:
        df = make_trending_df(200)
        close = 150.0
        df.loc[df.index[-1], "close"]      = close
        df.loc[df.index[-1], "rsi"]        = rsi
        df.loc[df.index[-1], "macd_hist"]  = 0.3    # positive but turning down
        df.loc[df.index[-2], "macd_hist"]  = 0.5    # was higher
        df.loc[df.index[-1], "ema_20"]     = 152.0  # price below ema_20 — rolling over
        df.loc[df.index[-1], "ema_50"]     = 140.0  # target (below price)
        df.loc[df.index[-1], "bb_upper"]   = 155.0  # price not in blow-off
        df.loc[df.index[-1], "atr"]        = 2.0
        df.loc[df.index[-1], "volume_ratio"] = 1.2
        return df

    def test_sell_signal_when_overbought(self):
        sig = OverboughtShortStrategy().generate_signal("TATASTEEL", self._overbought_df(), TradingMode.SWING)
        assert sig is not None
        assert sig.action == SignalAction.SELL

    def test_always_mis_intraday(self):
        """Short selling must always be MIS intraday in India."""
        sig = OverboughtShortStrategy().generate_signal("TATASTEEL", self._overbought_df(), TradingMode.SWING)
        assert sig is not None
        assert sig.product == Product.MIS
        assert sig.mode == TradingMode.INTRADAY

    def test_stop_is_above_entry(self):
        sig = OverboughtShortStrategy().generate_signal("TATASTEEL", self._overbought_df(), TradingMode.SWING)
        if sig:
            assert sig.stop_loss > sig.entry_price

    def test_target_is_below_entry(self):
        sig = OverboughtShortStrategy().generate_signal("TATASTEEL", self._overbought_df(), TradingMode.SWING)
        if sig:
            assert sig.target < sig.entry_price

    def test_no_signal_when_rsi_below_threshold(self):
        df = self._overbought_df(rsi=65.0)
        sig = OverboughtShortStrategy().generate_signal("TATASTEEL", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_when_macd_not_turning_down(self):
        df = self._overbought_df()
        df.loc[df.index[-1], "macd_hist"]  = 0.6   # rising, not falling
        df.loc[df.index[-2], "macd_hist"]  = 0.4
        sig = OverboughtShortStrategy().generate_signal("TATASTEEL", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_when_price_above_ema20(self):
        df = self._overbought_df()
        df.loc[df.index[-1], "ema_20"] = 145.0   # price 150 > ema_20 145
        sig = OverboughtShortStrategy().generate_signal("TATASTEEL", df, TradingMode.SWING)
        assert sig is None

    def test_no_signal_in_parabolic_blowoff(self):
        """Don't short when price is more than 2% above BB upper."""
        df = self._overbought_df()
        df.loc[df.index[-1], "bb_upper"] = 140.0   # price 150 >> bb_upper 140
        sig = OverboughtShortStrategy().generate_signal("TATASTEEL", df, TradingMode.SWING)
        assert sig is None

    def test_rr_at_least_1_5(self):
        sig = OverboughtShortStrategy().generate_signal("TATASTEEL", self._overbought_df(), TradingMode.SWING)
        if sig:
            risk   = sig.stop_loss - sig.entry_price
            reward = sig.entry_price - sig.target
            assert reward / risk >= 1.5

    def test_deeper_overbought_gives_larger_size(self):
        sig_mild = OverboughtShortStrategy().generate_signal(
            "X", self._overbought_df(rsi=77.0), TradingMode.SWING
        )
        sig_deep = OverboughtShortStrategy().generate_signal(
            "X", self._overbought_df(rsi=88.0), TradingMode.SWING
        )
        if sig_mild and sig_deep:
            assert sig_deep.position_size_pct >= sig_mild.position_size_pct


# ---------------------------------------------------------------------------
# SentimentDrivenStrategy
# ---------------------------------------------------------------------------

class TestSentimentDrivenStrategy:
    def _base_df(self, close: float = 120.0) -> pd.DataFrame:
        df = make_trending_df(100)
        df.loc[df.index[-1], "close"]    = close
        df.loc[df.index[-2], "close"]    = close * 0.983  # 1.7% move — within 3% limit
        df.loc[df.index[-1], "ema_50"]   = close * 0.96
        df.loc[df.index[-1], "ema_20"]   = close * 0.98
        df.loc[df.index[-1], "atr"]      = 2.0
        df.loc[df.index[-1], "macd_hist"] = 0.3
        df.loc[df.index[-2], "macd_hist"] = 0.1
        df.loc[df.index[-1], "volume_ratio"] = 1.2
        return df

    def test_long_signal_on_high_adjusted_score(self):
        df = self._base_df()
        sig = SentimentDrivenStrategy().generate_signal(
            "SUNPHARMA", df, TradingMode.SWING,
            adjusted_score=8.0, sentiment_reasoning="Strong earnings beat"
        )
        assert sig is not None
        assert sig.action == SignalAction.BUY
        assert sig.product == Product.CNC

    def test_short_signal_on_negative_score(self):
        df = self._base_df()
        # Force price below EMA50 for short
        df.loc[df.index[-1], "close"]   = 100.0
        df.loc[df.index[-1], "ema_50"]  = 110.0   # price below EMA50
        df.loc[df.index[-2], "close"]   = 101.7
        sig = SentimentDrivenStrategy().generate_signal(
            "SUNPHARMA", df, TradingMode.SWING,
            adjusted_score=-8.0, sentiment_reasoning="Major earnings miss"
        )
        assert sig is not None
        assert sig.action == SignalAction.SELL
        assert sig.product == Product.MIS   # equity short always MIS

    def test_neutral_score_no_signal(self):
        df = self._base_df()
        sig = SentimentDrivenStrategy().generate_signal(
            "SUNPHARMA", df, TradingMode.SWING, adjusted_score=3.0
        )
        assert sig is None

    def test_prefers_adjusted_score_over_raw(self):
        """adjusted_score should take precedence when both provided."""
        df = self._base_df()
        # raw=3 (below threshold), adjusted=8 (above) → signal should fire
        sig = SentimentDrivenStrategy().generate_signal(
            "SUNPHARMA", df, TradingMode.SWING,
            sentiment_score=3.0, adjusted_score=8.0
        )
        assert sig is not None
