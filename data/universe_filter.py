"""
Liquidity and volume filter for stock universes.
Removes illiquid stocks before they reach strategy analysis.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from config.risk_params_loader import load_risk_params
from config.universes import get_all_symbols, NIFTY_SMALLCAP_250_SAMPLE
from data.kite_client import get_kite
from data.market_data import fetch_ohlcv
from config.instruments import get_tokens

logger = logging.getLogger(__name__)


def filter_by_liquidity(
    symbols: list[str],
    min_daily_value_cr: float = 0.5,
    lookback_days: int = 20,
    is_smallcap: bool = False,
) -> list[str]:
    """
    Filter symbols by average daily traded value.
    min_daily_value_cr: minimum ₹ crore average daily turnover.
    Returns filtered list of symbols.
    """
    from datetime import datetime, timedelta
    from config.instruments import get_token

    liquid: list[str] = []
    min_value = min_daily_value_cr * 1e7  # Convert crore to rupees

    to_date = datetime.now()
    from_date = to_date - timedelta(days=lookback_days + 5)

    for symbol in symbols:
        token = get_token(symbol)
        if not token:
            logger.debug(f"No token for {symbol}, skipping")
            continue

        try:
            df = fetch_ohlcv(token, "day", from_date, to_date)
            if df.empty or len(df) < 5:
                continue

            # Average daily turnover = avg(close * volume)
            df["turnover"] = df["close"] * df["volume"]
            avg_turnover = df["turnover"].tail(lookback_days).mean()

            if avg_turnover >= min_value:
                if is_smallcap:
                    # Extra filter for smallcaps: min price ₹10
                    last_close = df["close"].iloc[-1]
                    if last_close < 10:
                        continue
                liquid.append(symbol)
            else:
                logger.debug(
                    f"{symbol}: avg daily turnover ₹{avg_turnover/1e7:.2f}Cr < min {min_daily_value_cr}Cr"
                )
        except Exception as exc:
            logger.warning(f"Liquidity filter failed for {symbol}: {exc}")

    logger.info(f"Liquidity filter: {len(liquid)}/{len(symbols)} symbols passed")
    return liquid


def filter_all_universes(
    nifty50: list[str],
    midcap: list[str],
    smallcap: list[str],
    min_daily_value_cr: float = 0.5,
) -> dict[str, list[str]]:
    """Filter all universes and return a dict of {universe: filtered_symbols}."""
    return {
        "nifty50": filter_by_liquidity(nifty50, min_daily_value_cr),
        "midcap": filter_by_liquidity(midcap, min_daily_value_cr),
        "smallcap": filter_by_liquidity(smallcap, min_daily_value_cr, is_smallcap=True),
    }
