"""
pandas-ta wrapper functions for technical indicators.
All functions accept a DataFrame with OHLCV columns and return Series or scalar.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)


def add_ema(df: pd.DataFrame, period: int, col: str = "close") -> pd.Series:
    return ta.ema(df[col], length=period)


def add_rsi(df: pd.DataFrame, period: int = 14, col: str = "close") -> pd.Series:
    return ta.rsi(df[col], length=period)


def add_macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """Returns DataFrame with columns: MACD_{fast}_{slow}_{signal}, MACDs_, MACDh_"""
    return ta.macd(df["close"], fast=fast, slow=slow, signal=signal)


def add_bollinger_bands(
    df: pd.DataFrame, period: int = 20, std: float = 2.0
) -> pd.DataFrame:
    """Returns DataFrame with BBL, BBM, BBU, BBB, BBP columns."""
    return ta.bbands(df["close"], length=period, std=std)


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return ta.atr(df["high"], df["low"], df["close"], length=period)


def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Returns DataFrame with ADX, DMP, DMN columns."""
    return ta.adx(df["high"], df["low"], df["close"], length=period)


def add_vwap(df: pd.DataFrame) -> pd.Series:
    """Calculate VWAP for intraday data (resets each day)."""
    return ta.vwap(df["high"], df["low"], df["close"], df["volume"])


def add_volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return ta.sma(df["volume"], length=period)


def add_roc(df: pd.DataFrame, period: int = 10, col: str = "close") -> pd.Series:
    """Rate of change: (close - close[N]) / close[N]."""
    return df[col].pct_change(periods=period)


def rolling_high(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return df["high"].rolling(period).max()


def rolling_low(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return df["low"].rolling(period).min()


def compute_all_indicators(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """
    Add a comprehensive set of indicators to the DataFrame.
    Returns the same DataFrame with new columns added.
    """
    p = params or {}
    df = df.copy()

    # EMAs
    for period in [9, 20, 50, 200]:
        df[f"ema_{period}"] = add_ema(df, period)

    # RSI
    df["rsi"] = add_rsi(df, p.get("rsi_period", 14))

    # MACD
    macd = add_macd(df)
    if macd is not None and not macd.empty:
        df[["macd", "macd_signal", "macd_hist"]] = macd.iloc[:, :3].values

    # Bollinger Bands
    bb = add_bollinger_bands(df, p.get("bb_period", 20), p.get("bb_std", 2.0))
    if bb is not None and not bb.empty:
        df["bb_lower"] = bb.iloc[:, 0]
        df["bb_mid"] = bb.iloc[:, 1]
        df["bb_upper"] = bb.iloc[:, 2]

    # ATR
    df["atr"] = add_atr(df, p.get("atr_period", 14))

    # ADX
    adx = add_adx(df)
    if adx is not None and not adx.empty:
        df["adx"] = adx.iloc[:, 0]

    # Volume SMA
    df["volume_sma"] = add_volume_sma(df, p.get("vol_period", 20))
    df["volume_ratio"] = df["volume"] / df["volume_sma"].replace(0, np.nan)

    # Rolling highs/lows
    period = p.get("breakout_period", 20)
    df["roll_high"] = rolling_high(df, period)
    df["roll_low"] = rolling_low(df, period)

    # Rate of change (for crash detection)
    df["roc_5"] = add_roc(df, 5)
    df["roc_10"] = add_roc(df, 10)

    return df.dropna(subset=["ema_20", "rsi"])
