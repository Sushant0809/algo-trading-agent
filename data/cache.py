"""
File-based bar cache for OHLCV data.
Avoids redundant KiteConnect API calls within the same session.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_CACHE_DIR = Path("./logs/bar_cache")


def _cache_key(token: int, interval: str) -> str:
    today = datetime.now().strftime("%Y%m%d")
    return f"{token}_{interval}_{today}"


def _cache_path(token: int, interval: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{_cache_key(token, interval)}.parquet"


def load_bars(token: int, interval: str) -> Optional[pd.DataFrame]:
    """Load bars from today's cache if available."""
    path = _cache_path(token, interval)
    if path.exists():
        try:
            df = pd.read_parquet(path)
            logger.debug(f"Cache hit: {path.name} ({len(df)} bars)")
            return df
        except Exception as exc:
            logger.warning(f"Cache read failed: {exc}")
    return None


def save_bars(token: int, interval: str, df: pd.DataFrame) -> None:
    """Save bars to today's cache."""
    if df.empty:
        return
    path = _cache_path(token, interval)
    try:
        df.to_parquet(path)
        logger.debug(f"Cached {len(df)} bars → {path.name}")
    except Exception as exc:
        logger.warning(f"Cache write failed: {exc}")


def fetch_or_cache(
    token: int,
    interval: str,
    fetch_fn,  # Callable that returns DataFrame
    *args,
    **kwargs,
) -> pd.DataFrame:
    """Try cache first, then fetch and cache."""
    cached = load_bars(token, interval)
    if cached is not None:
        return cached
    df = fetch_fn(*args, **kwargs)
    save_bars(token, interval, df)
    return df


def clear_old_cache(max_age_days: int = 3) -> None:
    """Remove cache files older than max_age_days."""
    if not _CACHE_DIR.exists():
        return
    today = datetime.now()
    for f in _CACHE_DIR.glob("*.parquet"):
        try:
            age = (today - datetime.fromtimestamp(f.stat().st_mtime)).days
            if age > max_age_days:
                f.unlink()
                logger.debug(f"Removed old cache: {f.name}")
        except Exception:
            pass
