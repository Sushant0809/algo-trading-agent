"""
KiteConnect singleton client wrapper.
Initialized once per session with a valid access token.
"""
from __future__ import annotations

import logging
from typing import Optional

from kiteconnect import KiteConnect, KiteTicker

logger = logging.getLogger(__name__)

_kite_instance: Optional[KiteConnect] = None
_ticker_instance: Optional[KiteTicker] = None


def init_kite(api_key: str, access_token: str) -> KiteConnect:
    """Initialize and return the KiteConnect client."""
    global _kite_instance
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    _kite_instance = kite
    logger.info("KiteConnect client initialized.")
    return kite


def get_kite() -> KiteConnect:
    """Return the initialized KiteConnect client."""
    if _kite_instance is None:
        raise RuntimeError("KiteConnect not initialized. Call init_kite() first.")
    return _kite_instance


def init_ticker(api_key: str, access_token: str) -> KiteTicker:
    """Initialize and return the KiteTicker WebSocket client."""
    global _ticker_instance
    ticker = KiteTicker(api_key=api_key, access_token=access_token)
    _ticker_instance = ticker
    logger.info("KiteTicker WebSocket client initialized.")
    return ticker


def get_ticker() -> KiteTicker:
    """Return the initialized KiteTicker client."""
    if _ticker_instance is None:
        raise RuntimeError("KiteTicker not initialized. Call init_ticker() first.")
    return _ticker_instance
