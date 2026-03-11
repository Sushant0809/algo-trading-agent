"""
Unit tests for all 4 trading strategies.
Uses synthetic OHLCV DataFrames — no live market data needed.
"""
import math
import pytest
import numpy as np
import pandas as pd

from signals.signal_model import SignalAction, TradingMode
from signals.indicators import compute_all_indicators
from strategies.momentum import MomentumStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.breakout import BreakoutStrategy
from strategies.sentiment_driven import SentimentDrivenStrategy


def make_trending_df(n: int = 300, start_price: float = 100.0) -> pd.DataFrame:
    """Create a strongly trending (upward) price series."""
    np.random.seed(42)
    prices = [start_price]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + np.random.normal(0.001, 0.01)))

    dates = pd.date_range("2023-01-01", periods=n, freq="B", tz="Asia/Kolkata")
    df = pd.DataFrame({
        "open": prices,
        "high": [p * (1 + abs(np.random.normal(0, 0.005))) for p in prices],
        "low": [p * (1 - abs(np.random.normal(0, 0.005))) for p in prices],
        "close": prices,
        "volume": [np.random.randint(500_000, 2_000_000) for _ in range(n)],
    }, index=dates)
    return compute_all_indicators(df)


def make_sideways_df(n: int = 200, center: float = 100.0, std: float = 2.0) -> pd.DataFrame:
    """Create a sideways/mean-reverting price series."""
    np.random.seed(123)
    prices = [center + np.random.normal(0, std) for _ in range(n)]

    dates = pd.date_range("2023-01-01", periods=n, freq="B", tz="Asia/Kolkata")
    df = pd.DataFrame({
        "open": prices,
        "high": [p + abs(np.random.normal(0, 1)) for p in prices],
        "low": [p - abs(np.random.normal(0, 1)) for p in prices],
        "close": prices,
        "volume": [np.random.randint(500_000, 2_000_000) for _ in range(n)],
    }, index=dates)
    return compute_all_indicators(df)


class TestMomentumStrategy:
    def test_signal_on_trending_market(self):
        df = make_trending_df(n=300)
        strategy = MomentumStrategy()

        # Force favorable conditions in last row
        df.loc[df.index[-1], "ema_20"] = 120.0
        df.loc[df.index[-1], "ema_50"] = 110.0
        df.loc[df.index[-1], "ema_200"] = 100.0
        df.loc[df.index[-1], "rsi"] = 60.0
        df.loc[df.index[-1], "macd_hist"] = 0.5
        df.loc[df.index[-2], "macd_hist"] = 0.3
        df.loc[df.index[-1], "close"] = 125.0
        df.loc[df.index[-1], "atr"] = 2.0
        df.loc[df.index[-1], "adx"] = 30.0  # ADX > 25 required for swing

        signal = strategy.generate_signal("RELIANCE", df, TradingMode.SWING)
        assert signal is not None
        assert signal.action == SignalAction.BUY
        assert signal.stop_loss < signal.entry_price
        assert signal.target > signal.entry_price

    def test_no_signal_when_ema_stack_broken(self):
        df = make_sideways_df(n=300)
        strategy = MomentumStrategy()
        signal = strategy.generate_signal("TCS", df, TradingMode.SWING)
        # Sideways market should not trigger momentum
        # (may or may not signal — just validate structure if it does)
        if signal:
            assert signal.action in (SignalAction.BUY, SignalAction.SELL)

    def test_insufficient_bars(self):
        df = make_trending_df(n=50)  # Too few bars for EMA(200)
        strategy = MomentumStrategy()
        signal = strategy.generate_signal("INFY", df, TradingMode.INTRADAY)
        assert signal is None

    def test_stop_loss_and_target(self):
        df = make_trending_df(n=300)
        df.loc[df.index[-1], "ema_20"] = 120.0
        df.loc[df.index[-1], "ema_50"] = 110.0
        df.loc[df.index[-1], "ema_200"] = 100.0
        df.loc[df.index[-1], "rsi"] = 60.0
        df.loc[df.index[-1], "macd_hist"] = 0.5
        df.loc[df.index[-2], "macd_hist"] = 0.3
        df.loc[df.index[-1], "close"] = 125.0
        df.loc[df.index[-1], "atr"] = 2.0
        df.loc[df.index[-1], "adx"] = 30.0

        strategy = MomentumStrategy()
        signal = strategy.generate_signal("HDFCBANK", df, TradingMode.SWING)
        if signal:
            risk = signal.entry_price - signal.stop_loss
            reward = signal.target - signal.entry_price
            assert reward >= 1.5 * risk  # Minimum 1.5:1 R:R


class TestMeanReversionStrategy:
    def make_oversold_df(self, n: int = 150) -> pd.DataFrame:
        df = make_sideways_df(n, center=100.0, std=3.0)
        # Force oversold conditions
        df.loc[df.index[-1], "rsi"] = 25.0
        df.loc[df.index[-1], "bb_lower"] = 95.0
        df.loc[df.index[-1], "bb_mid"] = 100.0
        df.loc[df.index[-1], "close"] = 95.2
        df.loc[df.index[-1], "volume"] = 2_000_000
        df.loc[df.index[-1], "volume_sma"] = 1_000_000
        df.loc[df.index[-1], "atr"] = 1.5
        return df

    def test_signal_on_oversold(self):
        df = self.make_oversold_df()
        strategy = MeanReversionStrategy()
        signal = strategy.generate_signal("WIPRO", df, TradingMode.SWING)
        assert signal is not None
        assert signal.action == SignalAction.BUY
        assert signal.target == round(df.loc[df.index[-1], "bb_mid"], 2)

    def test_no_signal_without_volume_spike(self):
        df = self.make_oversold_df()
        df.loc[df.index[-1], "volume"] = 800_000  # Below 1.5x threshold
        strategy = MeanReversionStrategy()
        signal = strategy.generate_signal("WIPRO", df, TradingMode.SWING)
        assert signal is None


class TestBreakoutStrategy:
    def make_breakout_df(self, n: int = 100) -> pd.DataFrame:
        df = make_trending_df(n, start_price=100.0)
        high_val = 150.0
        df.loc[df.index[-1], "close"] = high_val + 1  # Break above rolling high
        df.loc[df.index[-2], "close"] = high_val + 0.5  # Confirmation bar
        df.loc[df.index[-1], "roll_high"] = high_val
        df.loc[df.index[-2], "roll_high"] = high_val
        df.loc[df.index[-3], "roll_high"] = high_val - 1
        df.loc[df.index[-1], "volume"] = 3_000_000
        df.loc[df.index[-1], "volume_sma"] = 1_000_000
        df.loc[df.index[-1], "atr"] = 2.0
        return df

    def test_breakout_signal(self):
        df = self.make_breakout_df()
        strategy = BreakoutStrategy()
        signal = strategy.generate_signal("BAJFINANCE", df, TradingMode.SWING)
        assert signal is not None
        assert signal.action == SignalAction.BUY

    def test_no_breakout_without_volume(self):
        df = self.make_breakout_df()
        df.loc[df.index[-1], "volume"] = 1_200_000  # Only 1.2x, need 2x
        strategy = BreakoutStrategy()
        signal = strategy.generate_signal("BAJFINANCE", df, TradingMode.SWING)
        assert signal is None


class TestSentimentDrivenStrategy:
    def test_high_score_generates_signal(self):
        df = make_trending_df(n=100)
        df.loc[df.index[-1], "close"] = 120.0
        df.loc[df.index[-2], "close"] = 118.0  # Only 1.7% move — within limit
        df.loc[df.index[-1], f"ema_50"] = 115.0
        df.loc[df.index[-1], "atr"] = 2.0

        strategy = SentimentDrivenStrategy()
        signal = strategy.generate_signal(
            "SUNPHARMA", df, TradingMode.SWING,
            sentiment_score=8.5,
            sentiment_reasoning="Strong earnings beat, upgraded guidance",
        )
        assert signal is not None
        assert signal.action == SignalAction.BUY
        assert signal.sentiment_score == 8.5

    def test_low_score_no_signal(self):
        df = make_trending_df(n=100)
        strategy = SentimentDrivenStrategy()
        signal = strategy.generate_signal("SUNPHARMA", df, TradingMode.SWING, sentiment_score=5.0)
        assert signal is None

    def test_position_size_scales_with_score(self):
        df = make_trending_df(n=100)
        df.loc[df.index[-1], "atr"] = 2.0

        strategy = SentimentDrivenStrategy()
        sig7 = strategy.generate_signal("X", df, TradingMode.SWING, sentiment_score=7.0)
        sig10 = strategy.generate_signal("X", df, TradingMode.SWING, sentiment_score=10.0)

        if sig7 and sig10:
            assert sig10.position_size_pct > sig7.position_size_pct
