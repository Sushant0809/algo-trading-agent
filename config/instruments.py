"""
NSE instrument token cache.
Fetches the full instrument list from KiteConnect and caches it locally.
Refreshed daily at startup (8:31am IST).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)

# In-memory cache: symbol -> instrument_token
_TOKEN_MAP: dict[str, int] = {}
_INSTRUMENT_MAP: dict[int, dict] = {}  # token -> full record


def refresh_instruments(kite: "KiteConnect", cache_path: Path) -> None:
    """Download full NSE instrument list and save to cache."""
    logger.info("Refreshing NSE instrument token cache...")
    try:
        instruments = kite.instruments("NSE")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_date": date.today().isoformat(),
            "instruments": instruments,
        }
        cache_path.write_text(json.dumps(payload, default=str))
        logger.info(f"Cached {len(instruments)} NSE instruments to {cache_path}")
        _build_index(instruments)
    except Exception as exc:
        logger.error(f"Failed to refresh instruments: {exc}")
        raise


def load_instruments(cache_path: Path) -> bool:
    """Load instruments from cache file. Returns True if fresh (today's date)."""
    if not cache_path.exists():
        return False
    try:
        data = json.loads(cache_path.read_text())
        fetched_date = data.get("fetched_date", "")
        instruments = data.get("instruments", [])
        _build_index(instruments)
        is_fresh = fetched_date == date.today().isoformat()
        logger.info(
            f"Loaded {len(instruments)} instruments from cache "
            f"(date={fetched_date}, fresh={is_fresh})"
        )
        return is_fresh
    except Exception as exc:
        logger.warning(f"Could not load instrument cache: {exc}")
        return False


def _build_index(instruments: list[dict]) -> None:
    """Build in-memory lookup maps."""
    global _TOKEN_MAP, _INSTRUMENT_MAP
    _TOKEN_MAP = {}
    _INSTRUMENT_MAP = {}
    for inst in instruments:
        symbol = inst.get("tradingsymbol", "")
        token = inst.get("instrument_token", 0)
        if symbol and token:
            _TOKEN_MAP[symbol] = token
            _INSTRUMENT_MAP[token] = inst


def get_token(symbol: str) -> int | None:
    """Get instrument token for a symbol (NSE equity)."""
    return _TOKEN_MAP.get(symbol)


def get_instrument(token: int) -> dict | None:
    """Get full instrument record by token."""
    return _INSTRUMENT_MAP.get(token)


def get_tokens(symbols: list[str]) -> dict[str, int]:
    """Return {symbol: token} for all known symbols."""
    return {s: _TOKEN_MAP[s] for s in symbols if s in _TOKEN_MAP}


def get_all_tokens() -> dict[str, int]:
    return dict(_TOKEN_MAP)
