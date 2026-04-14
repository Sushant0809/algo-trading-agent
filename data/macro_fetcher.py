"""
Macroeconomic signals: FII/DII flows, India VIX, RBI rate.

FII (Foreign Institutional Investors) drive >60% of NSE large-cap price movement.
When FIIs are net buyers, even weak stocks go up. When FIIs sell, even strong stocks fall.

This module provides leading indicators for market regime classification.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# RBI repo rate (changes ~4 times per year, maintain manually)
# Last updated: April 2026
RBI_REPO_RATE = 6.50  # Current as of Q4 2025-26


def get_rbi_rate() -> float:
    """Return current RBI repo rate (updated manually, changes ~4x/year)."""
    return RBI_REPO_RATE


def fetch_fii_dii_flows() -> dict:
    """
    Fetch FII/DII flows from NSE (no API key required, scraping).

    Returns:
        {
            'fii_net_5d': float (net inflow in ₹ cr for last 5 days),
            'dii_net_5d': float (net inflow in ₹ cr for last 5 days),
            'fii_trend': str ('buying' | 'selling' | 'neutral'),
            'fii_3mo_avg': float (3-month average daily net inflow),
        }

    Returns zero/neutral if fetch fails (graceful degradation).
    """
    try:
        import requests
        from datetime import datetime, timedelta

        url = "https://www.nseindia.com/api/fiidiiTradeReact"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        }

        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        # Parse latest data
        if not data or "data" not in data:
            logger.warning("FII/DII API returned empty data")
            return _default_fii_dii()

        latest = data["data"][0]  # Most recent entry

        fii_5d = float(latest.get("fiiInflow", 0) or 0)
        dii_5d = float(latest.get("diiInflow", 0) or 0)

        # Trend classification
        fii_trend = "buying" if fii_5d > 0 else ("selling" if fii_5d < 0 else "neutral")

        logger.info(f"FII/DII: FII 5d={fii_5d:,.0f}₹cr ({fii_trend}), DII 5d={dii_5d:,.0f}₹cr")

        return {
            "fii_net_5d": fii_5d,
            "dii_net_5d": dii_5d,
            "fii_trend": fii_trend,
            "fii_3mo_avg": fii_5d / 5,  # Approximation; actual 3mo data would require history
        }

    except Exception as exc:
        logger.warning(f"FII/DII fetch failed: {exc}")
        return _default_fii_dii()


def fetch_india_vix() -> float:
    """
    Fetch India VIX (^INDIAVIX) from yfinance.

    Returns:
        float: Current VIX level (e.g., 15.5)

    Returns 0.0 if fetch fails (graceful degradation).
    """
    try:
        import yfinance as yf

        vix_ticker = yf.Ticker("^INDIAVIX")
        hist = vix_ticker.history(period="1d")

        if hist.empty:
            logger.warning("India VIX returned empty data")
            return 0.0

        vix_level = float(hist["Close"].iloc[-1])
        logger.info(f"India VIX: {vix_level:.2f}")
        return vix_level

    except Exception as exc:
        logger.warning(f"India VIX fetch failed: {exc}")
        return 0.0


def fetch_vix_momentum(days: int = 5) -> float:
    """
    Fetch India VIX momentum (rate of change over last N days).

    Research: VIX momentum is more predictive than absolute level.
    Falling VIX (negative momentum) signals improving sentiment (bullish).
    Rising VIX (positive momentum) signals deteriorating sentiment (bearish).

    Args:
        days: Number of days to look back for momentum calculation

    Returns:
        float: VIX momentum (positive = rising, negative = falling)
               Returns 0.0 if fetch fails (graceful degradation)
    """
    try:
        import yfinance as yf

        vix_ticker = yf.Ticker("^INDIAVIX")
        hist = vix_ticker.history(period=f"{days+1}d")

        if hist.empty or len(hist) < 2:
            logger.warning("VIX momentum: insufficient data")
            return 0.0

        # Calculate momentum as rate of change
        closes = hist["Close"].values
        current = closes[-1]
        past = closes[0]
        momentum = (current - past) / past if past > 0 else 0.0

        logger.info(f"VIX Momentum ({days}d): {momentum:+.2%}")
        return momentum

    except Exception as exc:
        logger.warning(f"VIX momentum fetch failed: {exc}")
        return 0.0


def _default_fii_dii() -> dict:
    """Return neutral/default FII/DII values when fetch fails."""
    return {
        "fii_net_5d": 0.0,
        "dii_net_5d": 0.0,
        "fii_trend": "neutral",
        "fii_3mo_avg": 0.0,
    }


def score_macro_signals(
    fii_net_5d: float,
    india_vix: float,
    rbi_rate: float = None,
    vix_momentum: float = None,
) -> int:
    """
    Score macroeconomic signals as bullish factors (0-2 points).

    Args:
        fii_net_5d: Net FII inflow in ₹ crores for last 5 days
        india_vix: Current India VIX level
        rbi_rate: RBI repo rate (optional, for context)
        vix_momentum: VIX momentum (rate of change, unused for stability)

    Returns:
        int: Score 0-2 (add to regime_score for classification)
    """
    score = 0

    # FII buying = bullish
    if fii_net_5d > 0:
        score += 1
        if fii_net_5d > 1000:  # Strong buying
            score += 0.5
    elif fii_net_5d < -1000:  # Strong selling
        score -= 0.5

    # Low VIX = stable, calm market
    if india_vix < 15:
        score += 1
    elif india_vix > 25:
        score -= 0.5

    # RBI rate context (lower is more supportive)
    if rbi_rate and rbi_rate < 6.0:
        score += 0.5

    return max(0, min(2, score))  # Cap 0-2
