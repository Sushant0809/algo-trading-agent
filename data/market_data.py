"""
Fetch OHLCV bars and quotes for NSE instruments.
Primary: KiteConnect Historical API (requires Connect subscription).
Fallback: yfinance (.NS suffix) — free, no subscription needed.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# KiteConnect interval -> yfinance interval
_YF_INTERVAL_MAP = {
    "minute":    "1m",
    "3minute":   "5m",   # yfinance has no 3m, use 5m
    "5minute":   "5m",
    "10minute":  "15m",
    "15minute":  "15m",
    "30minute":  "30m",
    "60minute":  "60m",
    "day":       "1d",
}

_YF_PERIOD_MAP = {
    "1m":  "7d",
    "5m":  "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "60d",
    "1d":  "2y",
}

_RATE_LIMIT_DELAY = 0.4


def _token_to_symbol(instrument_token: int) -> str | None:
    """Reverse-lookup: instrument token → NSE trading symbol."""
    try:
        from config.instruments import get_instrument
        inst = get_instrument(instrument_token)
        if inst:
            return inst.get("tradingsymbol")
    except Exception:
        pass
    return None


def _fetch_yfinance(symbol: str, yf_interval: str, n_bars: int) -> pd.DataFrame:
    """Fetch OHLCV from yfinance using NSE .NS suffix."""
    try:
        import yfinance as yf
        period = _YF_PERIOD_MAP.get(yf_interval, "60d")
        ticker = yf.Ticker(f"{symbol}.NS")
        df = ticker.history(period=period, interval=yf_interval, auto_adjust=True)
        if df.empty:
            return pd.DataFrame()
        df.index = df.index.tz_convert("Asia/Kolkata")
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        return df.iloc[-n_bars:]
    except Exception as exc:
        logger.error(f"yfinance fetch failed for {symbol}: {exc}")
        return pd.DataFrame()


def fetch_ohlcv(
    instrument_token: int,
    interval: str,
    from_date: datetime,
    to_date: datetime,
    continuous: bool = False,
    oi: bool = False,
) -> pd.DataFrame:
    """Fetch OHLCV — tries KiteConnect first, falls back to yfinance."""
    # Try KiteConnect
    try:
        from data.kite_client import get_kite
        kite = get_kite()
        kite_interval = interval if interval in _YF_INTERVAL_MAP else "5minute"
        records = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=kite_interval,
            continuous=continuous,
            oi=oi,
        )
        time.sleep(_RATE_LIMIT_DELAY)
        if records:
            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            df.index = df.index.tz_localize("Asia/Kolkata") if df.index.tz is None else df.index
            return df
    except Exception as exc:
        logger.debug(f"KiteConnect fetch failed for token={instrument_token}: {exc}, trying yfinance")

    # Fallback: yfinance
    symbol = _token_to_symbol(instrument_token)
    if not symbol:
        logger.error(f"Cannot resolve symbol for token={instrument_token}")
        return pd.DataFrame()

    yf_interval = _YF_INTERVAL_MAP.get(interval, "5m")
    n_bars = int((to_date - from_date).total_seconds() / 60 / int(yf_interval.replace("m", "").replace("d", "1440")) + 10)
    return _fetch_yfinance(symbol, yf_interval, n_bars)


def fetch_latest_bars(
    instrument_token: int,
    interval: str,
    n_bars: int = 200,
) -> pd.DataFrame:
    """Fetch the last n_bars bars — tries KiteConnect, falls back to yfinance."""
    # Try KiteConnect
    interval_minutes = {
        "minute": 1, "3minute": 3, "5minute": 5, "10minute": 10,
        "15minute": 15, "30minute": 30, "60minute": 60, "day": 1440,
    }
    mins = interval_minutes.get(interval, 5)
    lookback_days = max(3, (n_bars * mins) // 390 + 5)
    to_date = datetime.now()
    from_date = to_date - timedelta(days=lookback_days)

    try:
        from data.kite_client import get_kite
        kite = get_kite()
        records = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
        )
        time.sleep(_RATE_LIMIT_DELAY)
        if records:
            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            df.index = df.index.tz_localize("Asia/Kolkata") if df.index.tz is None else df.index
            return df.iloc[-n_bars:]
    except Exception as exc:
        logger.debug(f"KiteConnect bars failed for token={instrument_token}: {exc}, using yfinance")

    # Fallback: yfinance
    symbol = _token_to_symbol(instrument_token)
    if not symbol:
        logger.error(f"Cannot resolve symbol for token={instrument_token}")
        return pd.DataFrame()

    yf_interval = _YF_INTERVAL_MAP.get(interval, "1d")
    return _fetch_yfinance(symbol, yf_interval, n_bars)


def fetch_ltp(instrument_tokens: list[int]) -> dict[int, float]:
    """Fetch last traded prices — tries KiteConnect, falls back to yfinance."""
    # Try KiteConnect
    try:
        from data.kite_client import get_kite
        kite = get_kite()
        quotes = kite.ltp(instrument_tokens)
        return {int(k.split(":")[1]) if ":" in k else int(k): v["last_price"] for k, v in quotes.items()}
    except Exception:
        pass

    # Fallback: yfinance per token
    result = {}
    for token in instrument_tokens:
        symbol = _token_to_symbol(token)
        if not symbol:
            continue
        try:
            import yfinance as yf
            ltp = yf.Ticker(f"{symbol}.NS").fast_info.get("last_price")
            if ltp:
                result[token] = float(ltp)
        except Exception as exc:
            logger.debug(f"yfinance LTP failed for {symbol}: {exc}")
    return result


def fetch_quote(instrument_tokens: list[int]) -> dict[int, dict]:
    """Fetch full quotes — tries KiteConnect, falls back to yfinance OHLC."""
    try:
        from data.kite_client import get_kite
        kite = get_kite()
        return kite.quote(instrument_tokens)
    except Exception:
        pass

    result = {}
    for token in instrument_tokens:
        symbol = _token_to_symbol(token)
        if not symbol:
            continue
        try:
            import yfinance as yf
            info = yf.Ticker(f"{symbol}.NS").fast_info
            result[token] = {
                "last_price": info.get("last_price", 0),
                "ohlc": {
                    "open": info.get("open", 0),
                    "high": info.get("day_high", 0),
                    "low": info.get("day_low", 0),
                    "close": info.get("previous_close", 0),
                },
            }
        except Exception as exc:
            logger.debug(f"yfinance quote failed for {symbol}: {exc}")
    return result


def fetch_ohlc(instrument_tokens: list[int]) -> dict[int, dict]:
    """Fetch today's OHLC — tries KiteConnect, falls back to yfinance."""
    try:
        from data.kite_client import get_kite
        kite = get_kite()
        return kite.ohlc(instrument_tokens)
    except Exception:
        pass
    return fetch_quote(instrument_tokens)
