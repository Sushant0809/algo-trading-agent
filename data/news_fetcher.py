"""
News and corporate announcement fetcher for sentiment analysis.
Sources: NSE corporate announcements + financial news sites.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# NSE announcements API
NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
NSE_CORP_ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions"

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


class NewsItem:
    __slots__ = ("symbol", "headline", "body", "source", "published_at", "url")

    def __init__(
        self,
        symbol: str,
        headline: str,
        body: str = "",
        source: str = "",
        published_at: datetime | None = None,
        url: str = "",
    ):
        self.symbol = symbol
        self.headline = headline
        self.body = body
        self.source = source
        self.published_at = published_at or datetime.now(timezone.utc)
        self.url = url

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "headline": self.headline,
            "body": self.body[:500],
            "source": self.source,
            "published_at": self.published_at.isoformat(),
            "url": self.url,
        }


class NewsFetcher:
    def __init__(self, lookback_hours: int = 24):
        self.lookback_hours = lookback_hours
        self._session: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "NewsFetcher":
        self._session = httpx.AsyncClient(
            headers=NSE_HEADERS, timeout=15, follow_redirects=True
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session:
            await self._session.aclose()

    async def fetch_nse_announcements(self, symbol: str | None = None) -> list[NewsItem]:
        """Fetch NSE corporate announcements for a symbol or all symbols."""
        if self._session is None:
            raise RuntimeError("Use as async context manager.")

        params: dict[str, Any] = {"index": "equities"}
        if symbol:
            params["symbol"] = symbol

        items: list[NewsItem] = []
        try:
            # First hit the main page to get cookies
            await self._session.get("https://www.nseindia.com", timeout=10)
            resp = await self._session.get(NSE_ANNOUNCEMENTS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

            cutoff = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
            announcements = data if isinstance(data, list) else data.get("data", [])

            for ann in announcements:
                try:
                    ts_str = ann.get("exchdisstime", "") or ann.get("bcastdttm", "")
                    try:
                        ts = datetime.strptime(ts_str[:19], "%d-%b-%Y %H:%M:%S")
                        ts = ts.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        ts = datetime.now(timezone.utc)

                    if ts < cutoff:
                        continue

                    items.append(
                        NewsItem(
                            symbol=ann.get("symbol", symbol or ""),
                            headline=ann.get("subject", ""),
                            body=ann.get("desc", ""),
                            source="NSE",
                            published_at=ts,
                        )
                    )
                except Exception:
                    continue

        except Exception as exc:
            logger.warning(f"NSE announcements fetch failed: {exc}")

        return items

    async def fetch_news_for_watchlist(self, symbols: list[str]) -> dict[str, list[NewsItem]]:
        """Fetch news for all symbols in watchlist concurrently."""
        tasks = {sym: self.fetch_nse_announcements(sym) for sym in symbols}
        results: dict[str, list[NewsItem]] = {}

        for sym, coro in tasks.items():
            try:
                items = await coro
                if items:
                    results[sym] = items
                await asyncio.sleep(0.3)  # Be gentle with NSE API
            except Exception as exc:
                logger.warning(f"News fetch failed for {sym}: {exc}")

        return results
