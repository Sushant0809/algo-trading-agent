"""
KiteConnect daily authentication.
Handles token refresh using Playwright automation or manual request_token input.
Access tokens expire daily at 6am IST — this runs at 8:30am IST.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)


def load_cached_token(cache_path: Path) -> str | None:
    """Load today's access token from cache if it exists."""
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text())
        if data.get("date") == date.today().isoformat():
            token = data.get("access_token", "")
            if token:
                logger.info("Loaded today's access token from cache.")
                return token
    except Exception as exc:
        logger.warning(f"Could not read token cache: {exc}")
    return None


def save_token_cache(cache_path: Path, access_token: str) -> None:
    """Save access token with today's date."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date.today().isoformat(),
        "access_token": access_token,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_path.write_text(json.dumps(payload))
    logger.info(f"Access token saved to {cache_path}")


def exchange_request_token(api_key: str, api_secret: str, request_token: str) -> str:
    """Exchange a request_token for an access_token."""
    kite = KiteConnect(api_key=api_key)
    session = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session["access_token"]
    logger.info("Successfully exchanged request_token for access_token.")
    return access_token


def update_env_token(access_token: str, env_path: Path = Path(".env")) -> None:
    """Update KITE_ACCESS_TOKEN in .env file."""
    if not env_path.exists():
        logger.warning(f".env not found at {env_path}, skipping env update.")
        return
    lines = env_path.read_text().splitlines()
    updated = []
    found = False
    for line in lines:
        if line.startswith("KITE_ACCESS_TOKEN="):
            updated.append(f"KITE_ACCESS_TOKEN={access_token}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"KITE_ACCESS_TOKEN={access_token}")
    env_path.write_text("\n".join(updated) + "\n")
    logger.info("Updated KITE_ACCESS_TOKEN in .env")


async def run_daily_auth(
    api_key: str,
    api_secret: str,
    user_id: str,
    password: str,
    totp_secret: str,
    cache_path: Path,
    env_path: Path = Path(".env"),
    headless: bool = True,
) -> str:
    """
    Full daily auth flow:
    1. Check cache for today's token
    2. If missing/stale, run Playwright auto-login to get request_token
    3. Exchange for access_token
    4. Save to cache + update .env
    Returns the access_token.
    """
    # Try cache first
    cached = load_cached_token(cache_path)
    if cached:
        return cached

    # Run browser automation
    logger.info("No valid cached token — running Playwright auto-login...")
    from auth.auto_login import get_request_token_via_playwright

    request_token = await get_request_token_via_playwright(
        api_key=api_key,
        user_id=user_id,
        password=password,
        totp_secret=totp_secret,
        headless=headless,
    )

    access_token = exchange_request_token(api_key, api_secret, request_token)
    save_token_cache(cache_path, access_token)
    update_env_token(access_token, env_path)
    return access_token
