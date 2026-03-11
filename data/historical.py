"""
Extended historical data fetcher using nsepy for backtesting.
Used when KiteConnect 60-day / 400-day limits are insufficient.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import pandas as pd

logger = logging.getLogger(__name__)


def fetch_nsepy(
    symbol: str,
    start: date,
    end: date,
    index: bool = False,
) -> pd.DataFrame:
    """
    Fetch daily OHLCV data from NSE via nsepy.
    Returns DataFrame with columns: [Open, High, Low, Close, Volume].
    """
    try:
        from nsepy import get_history
        from nsepy.symbols import get_index_pe_history

        if index:
            df = get_history(symbol=symbol, start=start, end=end, index=True)
        else:
            df = get_history(symbol=symbol, start=start, end=end)

        if df.empty:
            return pd.DataFrame()

        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df.index = pd.to_datetime(df.index)
        return df

    except Exception as exc:
        logger.error(f"nsepy fetch failed for {symbol}: {exc}")
        return pd.DataFrame()


def fetch_multi_symbol_history(
    symbols: list[str],
    start: date,
    end: date,
) -> dict[str, pd.DataFrame]:
    """
    Fetch historical daily data for multiple symbols.
    Returns {symbol: DataFrame}.
    """
    results = {}
    for symbol in symbols:
        logger.info(f"Fetching history for {symbol} ({start} to {end})")
        df = fetch_nsepy(symbol, start, end)
        if not df.empty:
            results[symbol] = df
        else:
            logger.warning(f"No data for {symbol}")
    return results


def fetch_nse_index_history(index_name: str, start: date, end: date) -> pd.DataFrame:
    """Fetch index (e.g. NIFTY 50) historical data."""
    return fetch_nsepy(index_name, start, end, index=True)
