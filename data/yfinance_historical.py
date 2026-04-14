"""
Historical data fetcher using yfinance.
Replaces the broken nsepy-based data/historical.py.

Why yfinance:
  - nsepy is unmaintained and broken for modern Python / NSE changes
  - yfinance works reliably for NSE data using ".NS" suffix
  - Supports: daily bars back to 2000, 5-min bars for last 60 days
  - NIFTY50 index: ticker = "^NSEI"

Data source rules:
  - Daily bars:   SYMBOL.NS   e.g. RELIANCE.NS, TCS.NS
  - Index daily:  ^NSEI (NIFTY50), ^NSEBANK (BANKNIFTY)
  - 5-min intraday: SYMBOL.NS, interval="5m", max 60 days back
  - All returned DataFrames: lowercase columns [open, high, low, close, volume]
    with timezone-aware DatetimeIndex (Asia/Kolkata)

Caching:
  - All fetched data saved to logs/cache/historical/{ticker}_{interval}_{start}_{end}.parquet
  - Cache hit skips the network call entirely
  - Call clear_cache() to force re-fetch
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent / "logs" / "cache" / "historical"
_RATE_LIMIT_DELAY = 0.5   # seconds between yfinance API calls


def _nse_ticker(symbol: str) -> str:
    """Convert bare NSE symbol to yfinance ticker. Leaves ^NSEI, ^NSEBANK, etc. as-is."""
    if symbol.startswith("^") or symbol.endswith(".NS") or symbol.endswith(".BO"):
        return symbol
    return f"{symbol}.NS"


def _cache_path(ticker: str, interval: str, start: date, end: date) -> Path:
    safe = ticker.replace("^", "IDX_").replace(".", "_")
    return _CACHE_DIR / f"{safe}_{interval}_{start}_{end}.parquet"


def _load_cache(path: Path) -> Optional[pd.DataFrame]:
    if path.exists():
        try:
            df = pd.read_parquet(path)
            logger.debug(f"Cache hit: {path.name}")
            return df
        except Exception as exc:
            logger.warning(f"Cache read failed ({path.name}): {exc}")
    return None


def _save_cache(df: pd.DataFrame, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)
    except Exception as exc:
        logger.warning(f"Cache write failed ({path.name}): {exc}")


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns, drop Dividends/Stock Splits, localise index to IST."""
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]

    for drop_col in ["dividends", "stock splits", "capital gains"]:
        if drop_col in df.columns:
            df.drop(columns=[drop_col], inplace=True)

    if "open" not in df.columns:
        return pd.DataFrame()

    df = df[["open", "high", "low", "close", "volume"]].copy()

    # Convert index to IST
    if df.index.tz is None:
        df.index = df.index.tz_localize("Asia/Kolkata")
    else:
        df.index = df.index.tz_convert("Asia/Kolkata")

    df = df.dropna(subset=["close"])
    df = df[df["volume"] > 0]
    return df


def fetch_daily(
    symbol: str,
    start: date,
    end: date,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Fetch daily OHLCV bars for a single NSE symbol from yfinance.

    Args:
        symbol: NSE symbol e.g. "RELIANCE", "TCS", or full ticker "^NSEI"
        start:  Start date (inclusive)
        end:    End date (inclusive)
        use_cache: If True, tries disk cache before network call.

    Returns:
        DataFrame with columns [open, high, low, close, volume],
        IST-localised DatetimeIndex. Empty DataFrame on failure.
    """
    ticker = _nse_ticker(symbol)
    cache_file = _cache_path(ticker, "1d", start, end)

    if use_cache:
        cached = _load_cache(cache_file)
        if cached is not None:
            return cached

    try:
        import yfinance as yf
        time.sleep(_RATE_LIMIT_DELAY)
        raw = yf.download(
            ticker,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # yfinance end is exclusive
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        if raw.empty:
            logger.warning(f"yfinance returned no data for {ticker} ({start} to {end})")
            return pd.DataFrame()

        # yfinance returns MultiIndex columns when downloading single ticker sometimes
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.droplevel(1)

        df = _normalise(raw)
        if df.empty:
            return df

        if use_cache:
            _save_cache(df, cache_file)

        logger.info(f"Fetched {len(df)} daily bars for {ticker} ({start} to {end})")
        return df

    except Exception as exc:
        logger.error(f"yfinance daily fetch failed for {ticker}: {exc}")
        return pd.DataFrame()


def fetch_intraday(
    symbol: str,
    interval: str = "5m",
    days_back: int = 59,
    use_cache: bool = False,   # intraday not cached (changes daily)
) -> pd.DataFrame:
    """
    Fetch intraday bars (5m, 15m, 60m) — yfinance limit: last 60 days only.

    Args:
        symbol:    NSE symbol
        interval:  "5m", "15m", "60m", "1h"
        days_back: How many days back (max 59 for 5m)
        use_cache: Disable by default for intraday (stale within hours)

    Returns:
        DataFrame with OHLCV, IST-localised index.
    """
    ticker = _nse_ticker(symbol)
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days_back)

    cache_file = _cache_path(ticker, interval, start_dt.date(), end_dt.date())
    if use_cache:
        cached = _load_cache(cache_file)
        if cached is not None:
            return cached

    try:
        import yfinance as yf
        time.sleep(_RATE_LIMIT_DELAY)
        raw = yf.download(
            ticker,
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
            interval=interval,
            auto_adjust=True,
            progress=False,
        )
        if raw.empty:
            logger.warning(f"No intraday data for {ticker}")
            return pd.DataFrame()

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.droplevel(1)

        df = _normalise(raw)
        if use_cache and not df.empty:
            _save_cache(df, cache_file)

        logger.info(f"Fetched {len(df)} {interval} bars for {ticker}")
        return df

    except Exception as exc:
        logger.error(f"yfinance intraday fetch failed for {ticker}: {exc}")
        return pd.DataFrame()


def fetch_nifty50(start: date, end: date, use_cache: bool = True) -> pd.DataFrame:
    """Fetch NIFTY50 daily bars (^NSEI)."""
    return fetch_daily("^NSEI", start, end, use_cache=use_cache)


def fetch_nifty_bank(start: date, end: date, use_cache: bool = True) -> pd.DataFrame:
    """Fetch BANKNIFTY daily bars (^NSEBANK)."""
    return fetch_daily("^NSEBANK", start, end, use_cache=use_cache)


def fetch_multi_symbol(
    symbols: list[str],
    start: date,
    end: date,
    interval: str = "1d",
    use_cache: bool = True,
    delay: float = 0.3,
) -> dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV for a list of NSE symbols.

    Returns:
        {symbol: DataFrame}  — only populated for symbols that returned data.
    """
    results: dict[str, pd.DataFrame] = {}
    for i, symbol in enumerate(symbols):
        if interval == "1d":
            df = fetch_daily(symbol, start, end, use_cache=use_cache)
        else:
            df = fetch_intraday(symbol, interval=interval, days_back=(end - start).days)

        if not df.empty:
            results[symbol] = df
        else:
            logger.warning(f"No data for {symbol} — skipping")

        if delay > 0 and i < len(symbols) - 1:
            time.sleep(delay)

    logger.info(
        f"fetch_multi_symbol: {len(results)}/{len(symbols)} symbols fetched "
        f"({start} to {end}, interval={interval})"
    )
    return results


def nifty50_daily_returns(start: date, end: date) -> pd.Series:
    """
    Returns a Series of daily NIFTY50 percentage returns (as fractions, 0.01 = 1%).
    Used as benchmark for alpha/beta calculations.
    """
    df = fetch_nifty50(start, end)
    if df.empty:
        return pd.Series(dtype=float)
    returns = df["close"].pct_change().dropna()
    returns.name = "nifty50"
    return returns


def clear_cache(symbol: str | None = None) -> None:
    """
    Delete cached parquet files.
    If symbol is given, only deletes that symbol's cache files.
    If None, deletes all cached historical data.
    """
    if not _CACHE_DIR.exists():
        return

    pattern = f"{_nse_ticker(symbol).replace('^', 'IDX_').replace('.', '_')}*" if symbol else "*.parquet"
    deleted = 0
    for f in _CACHE_DIR.glob(pattern):
        f.unlink()
        deleted += 1
    logger.info(f"Cache cleared: {deleted} files removed")
