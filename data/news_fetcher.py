"""
News and corporate announcement fetcher for sentiment analysis.

Sources (all free, no paid APIs):
  - NSE corporate announcements  (official, highest credibility)
  - BSE corporate announcements  (official, highest credibility)
  - Economic Times RSS           (financial news)
  - Moneycontrol RSS             (financial news)
  - LiveMint RSS                 (financial news)
  - Business Standard RSS        (financial news)
  - Reddit JSON API              (retail sentiment — r/IndiaInvestments, r/IndianStreetBets)
  - X/Twitter via Nitter RSS     (social sentiment — no API key required)

Features:
  - Staleness weighting:  0-1hr=1.0, 1-6hr=0.8, 6-12hr=0.6, 12-24hr=0.4
  - Source credibility:   NSE/BSE=1.0, ET/MC/BS/Mint=0.85, Reddit=0.55, X=0.50
  - Deduplication:        headline fingerprint (normalise → hash)
  - Concurrent fetching:  all sources fetched in parallel via asyncio
  - Body length:          up to 1000 chars (was 300)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source credibility weights
# ---------------------------------------------------------------------------
SOURCE_CREDIBILITY: dict[str, float] = {
    "NSE":              1.0,
    "BSE":              1.0,
    "Economic Times":   0.85,
    "Moneycontrol":     0.85,
    "Business Standard":0.85,
    "LiveMint":         0.80,
    "Reddit":           0.55,
    "X/Twitter":        0.50,
}

# ---------------------------------------------------------------------------
# RSS feed URLs
# ---------------------------------------------------------------------------
RSS_FEEDS = {
    "Economic Times":    "https://economictimes.indiatimes.com/markets/rss.cms",
    "Moneycontrol":      "https://www.moneycontrol.com/rss/latestnews.xml",
    "LiveMint":          "https://www.livemint.com/rss/markets",
    "Business Standard": "https://www.business-standard.com/rss/markets-106.rss",
}

# Nitter instances to try in order (no API key needed)
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.mint.lgbt",
]

# Key financial X/Twitter accounts for Indian markets
X_ACCOUNTS = [
    "NSEIndia",
    "BSEIndia",
    "moneycontrolcom",
    "economictimes",
    "LiveMint",
    "CNBCTV18News",
    "ZerodhaOnline",
]

# Reddit subreddits for Indian market sentiment
REDDIT_SUBREDDITS = [
    "IndiaInvestments",
    "IndianStreetBets",
]

# NSE / BSE API endpoints
NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
BSE_ANNOUNCEMENTS_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

REDDIT_HEADERS = {
    "User-Agent": "algo-trading-bot/1.0 (research purposes)",
}

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AlgoTradingBot/1.0)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


# ---------------------------------------------------------------------------
# NewsItem dataclass
# ---------------------------------------------------------------------------

@dataclass
class NewsItem:
    symbol:             str
    headline:           str
    body:               str        = ""
    source:             str        = ""
    published_at:       datetime   = field(default_factory=lambda: datetime.now(timezone.utc))
    url:                str        = ""
    staleness_weight:   float      = 1.0   # 0.0–1.0 based on age
    source_credibility: float      = 0.85  # 0.0–1.0 based on source
    relevance_score:    float      = 0.0   # staleness_weight × source_credibility
    fingerprint:        str        = ""    # dedup key

    def __post_init__(self) -> None:
        self.staleness_weight   = _staleness_weight(self.published_at)
        self.source_credibility = SOURCE_CREDIBILITY.get(self.source, 0.7)
        self.relevance_score    = round(self.staleness_weight * self.source_credibility, 4)
        self.fingerprint        = _headline_fingerprint(self.headline)

    def to_dict(self) -> dict:
        return {
            "symbol":             self.symbol,
            "headline":           self.headline,
            "body":               self.body[:1000],
            "source":             self.source,
            "published_at":       self.published_at.isoformat(),
            "url":                self.url,
            "staleness_weight":   self.staleness_weight,
            "source_credibility": self.source_credibility,
            "relevance_score":    self.relevance_score,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _staleness_weight(published_at: datetime) -> float:
    """Return staleness weight based on how old the news is."""
    now  = datetime.now(timezone.utc)
    age  = (now - published_at).total_seconds() / 3600  # hours
    if age <= 1:
        return 1.0
    elif age <= 6:
        return 0.8
    elif age <= 12:
        return 0.6
    elif age <= 24:
        return 0.4
    return 0.2


def _headline_fingerprint(headline: str) -> str:
    """Normalise headline and return MD5 fingerprint for deduplication."""
    normalised = re.sub(r"[^a-z0-9\s]", "", headline.lower())
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return hashlib.md5(normalised.encode()).hexdigest()


def _deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    """Remove duplicate news items by headline fingerprint. Keep highest relevance."""
    seen:   dict[str, NewsItem] = {}
    for item in items:
        fp = item.fingerprint
        if fp not in seen or item.relevance_score > seen[fp].relevance_score:
            seen[fp] = item
    return list(seen.values())


def _parse_rss_date(date_str: str) -> datetime:
    """Parse RFC 2822 or ISO date strings from RSS feeds."""
    if not date_str:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str[:19], fmt[:len(date_str)])
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _symbol_in_text(symbol: str, text: str) -> bool:
    """Check if symbol or company name fragment appears in text."""
    clean = symbol.replace(".NS", "").replace(".BO", "").upper()
    return clean in text.upper()


# ---------------------------------------------------------------------------
# Main fetcher class
# ---------------------------------------------------------------------------

class NewsFetcher:
    """
    Async news fetcher aggregating all free sources.
    Use as an async context manager.

    Example:
        async with NewsFetcher(lookback_hours=12) as fetcher:
            news_map = await fetcher.fetch_news_for_watchlist(symbols)
    """

    def __init__(self, lookback_hours: int = 24):
        self.lookback_hours = lookback_hours
        self.cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        self._nse_session:    httpx.AsyncClient | None = None
        self._general_session: httpx.AsyncClient | None = None
        self._reddit_session:  httpx.AsyncClient | None = None

    async def __aenter__(self) -> "NewsFetcher":
        self._nse_session = httpx.AsyncClient(
            headers=NSE_HEADERS, timeout=15, follow_redirects=True
        )
        self._general_session = httpx.AsyncClient(
            headers=RSS_HEADERS, timeout=15, follow_redirects=True
        )
        self._reddit_session = httpx.AsyncClient(
            headers=REDDIT_HEADERS, timeout=15, follow_redirects=True
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        for session in [self._nse_session, self._general_session, self._reddit_session]:
            if session:
                await session.aclose()

    # ------------------------------------------------------------------
    # Public: fetch for entire watchlist
    # ------------------------------------------------------------------

    async def fetch_news_for_watchlist(
        self, symbols: list[str]
    ) -> dict[str, list[NewsItem]]:
        """
        Fetch all sources concurrently, deduplicate, and map to symbols.
        Returns dict of symbol → list[NewsItem] sorted by relevance_score desc.
        """
        # Fetch all sources in parallel
        nse_task     = self._fetch_nse_all()
        bse_task     = self._fetch_bse_all()
        rss_task     = self._fetch_all_rss()
        reddit_task  = self._fetch_reddit(symbols)
        twitter_task = self._fetch_nitter(symbols)

        nse_items, bse_items, rss_items, reddit_items, twitter_items = await asyncio.gather(
            nse_task, bse_task, rss_task, reddit_task, twitter_task,
            return_exceptions=True,
        )

        # Flatten, handle exceptions gracefully
        all_items: list[NewsItem] = []
        for result in [nse_items, bse_items, rss_items, reddit_items, twitter_items]:
            if isinstance(result, list):
                all_items.extend(result)
            elif isinstance(result, Exception):
                logger.warning(f"Source fetch error: {result}")

        # Global dedup across all sources
        all_items = _deduplicate(all_items)

        logger.info(
            f"Total news items after dedup: {len(all_items)} "
            f"from {len(symbols)} symbols"
        )

        # Map to symbols
        result_map: dict[str, list[NewsItem]] = {}
        for symbol in symbols:
            matched = [
                item for item in all_items
                if item.symbol == symbol
                or _symbol_in_text(symbol, item.headline)
                or _symbol_in_text(symbol, item.body)
            ]
            # Assign symbol and sort by relevance
            for item in matched:
                item.symbol = symbol
            matched.sort(key=lambda x: x.relevance_score, reverse=True)
            if matched:
                result_map[symbol] = matched[:10]  # Top 10 per symbol

        return result_map

    # ------------------------------------------------------------------
    # NSE announcements
    # ------------------------------------------------------------------

    async def _fetch_nse_all(self) -> list[NewsItem]:
        """Fetch all NSE corporate announcements (equities)."""
        if not self._nse_session:
            return []
        items: list[NewsItem] = []
        try:
            await self._nse_session.get("https://www.nseindia.com", timeout=10)
            resp = await self._nse_session.get(
                NSE_ANNOUNCEMENTS_URL, params={"index": "equities"}
            )
            resp.raise_for_status()
            data = resp.json()
            announcements = data if isinstance(data, list) else data.get("data", [])

            for ann in announcements:
                try:
                    ts_str = ann.get("exchdisstime", "") or ann.get("bcastdttm", "")
                    try:
                        ts = datetime.strptime(ts_str[:19], "%d-%b-%Y %H:%M:%S")
                        ts = ts.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        ts = datetime.now(timezone.utc)

                    if ts < self.cutoff:
                        continue

                    items.append(NewsItem(
                        symbol=ann.get("symbol", ""),
                        headline=ann.get("subject", ""),
                        body=ann.get("desc", "")[:1000],
                        source="NSE",
                        published_at=ts,
                        url=ann.get("attchmntFile", ""),
                    ))
                except Exception:
                    continue

            logger.info(f"NSE: fetched {len(items)} announcements")
        except Exception as exc:
            logger.warning(f"NSE fetch failed: {exc}")
        return items

    # ------------------------------------------------------------------
    # BSE announcements
    # ------------------------------------------------------------------

    async def _fetch_bse_all(self) -> list[NewsItem]:
        """Fetch BSE corporate announcements."""
        if not self._general_session:
            return []
        items: list[NewsItem] = []
        try:
            params = {
                "pageno":    "1",
                "strCat":    "-1",
                "strPrevDate": (datetime.now() - timedelta(hours=self.lookback_hours)).strftime("%Y%m%d"),
                "strScrip":  "",
                "strSearch": "P",
                "strToDate": datetime.now().strftime("%Y%m%d"),
                "strType":   "C",
                "subcategory": "-1",
            }
            resp = await self._general_session.get(BSE_ANNOUNCEMENTS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            announcements = data.get("Table", [])

            for ann in announcements:
                try:
                    ts_str = ann.get("NEWS_DT", "") or ann.get("DissemDT", "")
                    try:
                        ts = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
                        ts = ts.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        ts = datetime.now(timezone.utc)

                    if ts < self.cutoff:
                        continue

                    items.append(NewsItem(
                        symbol=ann.get("SCRIP_CD", ""),
                        headline=ann.get("HEADLINE", "") or ann.get("NEWSSUB", ""),
                        body=ann.get("ATTACHMENTNAME", "")[:1000],
                        source="BSE",
                        published_at=ts,
                        url=f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{ann.get('ATTACHMENTNAME', '')}",
                    ))
                except Exception:
                    continue

            logger.info(f"BSE: fetched {len(items)} announcements")
        except Exception as exc:
            logger.warning(f"BSE fetch failed: {exc}")
        return items

    # ------------------------------------------------------------------
    # RSS feeds (ET, Moneycontrol, LiveMint, Business Standard)
    # ------------------------------------------------------------------

    async def _fetch_all_rss(self) -> list[NewsItem]:
        """Fetch all RSS feeds concurrently."""
        tasks = [
            self._fetch_rss(name, url)
            for name, url in RSS_FEEDS.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[NewsItem] = []
        for r in results:
            if isinstance(r, list):
                items.extend(r)
        return items

    async def _fetch_rss(self, source_name: str, url: str) -> list[NewsItem]:
        """Fetch and parse a single RSS feed."""
        if not self._general_session:
            return []
        items: list[NewsItem] = []
        try:
            resp = await self._general_session.get(url, timeout=10)
            resp.raise_for_status()

            root = ET.fromstring(resp.text)
            ns   = {"atom": "http://www.w3.org/2005/Atom"}

            # Handle both RSS 2.0 and Atom
            entries = root.findall(".//item") or root.findall(".//atom:entry", ns)

            for entry in entries:
                try:
                    title = (
                        (entry.findtext("title") or entry.findtext("atom:title", namespaces=ns) or "")
                        .strip()
                    )
                    desc = (
                        (entry.findtext("description") or
                         entry.findtext("atom:summary", namespaces=ns) or
                         entry.findtext("atom:content", namespaces=ns) or "")
                        .strip()
                    )
                    # Strip HTML tags from description
                    desc = re.sub(r"<[^>]+>", " ", desc)
                    desc = re.sub(r"\s+", " ", desc).strip()

                    link = (
                        entry.findtext("link") or
                        entry.findtext("atom:link", namespaces=ns) or ""
                    )
                    pub_date = (
                        entry.findtext("pubDate") or
                        entry.findtext("atom:published", namespaces=ns) or ""
                    )
                    ts = _parse_rss_date(pub_date)

                    if ts < self.cutoff or not title:
                        continue

                    items.append(NewsItem(
                        symbol="",   # Assigned later during symbol matching
                        headline=title,
                        body=desc[:1000],
                        source=source_name,
                        published_at=ts,
                        url=link,
                    ))
                except Exception:
                    continue

            logger.info(f"{source_name} RSS: fetched {len(items)} articles")
        except Exception as exc:
            logger.warning(f"{source_name} RSS fetch failed: {exc}")
        return items

    # ------------------------------------------------------------------
    # Reddit JSON API (no auth required for public subreddits)
    # ------------------------------------------------------------------

    async def _fetch_reddit(self, symbols: list[str]) -> list[NewsItem]:
        """
        Fetch posts from Indian finance subreddits mentioning watchlist symbols.
        Uses Reddit's public JSON API — no API key required.
        """
        if not self._reddit_session:
            return []
        items: list[NewsItem] = []

        for subreddit in REDDIT_SUBREDDITS:
            for symbol in symbols:
                clean_symbol = symbol.replace(".NS", "").replace(".BO", "")
                try:
                    url = (
                        f"https://www.reddit.com/r/{subreddit}/search.json"
                        f"?q={quote(clean_symbol)}&sort=new&limit=5"
                        f"&t=day&restrict_sr=1"
                    )
                    resp = await self._reddit_session.get(url, timeout=10)
                    resp.raise_for_status()
                    data = resp.json()

                    posts = data.get("data", {}).get("children", [])
                    for post in posts:
                        pd_ = post.get("data", {})
                        created_utc = pd_.get("created_utc", 0)
                        ts = datetime.fromtimestamp(created_utc, tz=timezone.utc)

                        if ts < self.cutoff:
                            continue

                        title    = pd_.get("title", "")
                        selftext = pd_.get("selftext", "")[:1000]
                        score    = pd_.get("score", 0)
                        comments = pd_.get("num_comments", 0)

                        # Boost body with engagement context
                        body = (
                            f"{selftext} "
                            f"[upvotes={score}, comments={comments}, "
                            f"subreddit=r/{subreddit}]"
                        ).strip()

                        items.append(NewsItem(
                            symbol=symbol,
                            headline=title,
                            body=body,
                            source="Reddit",
                            published_at=ts,
                            url=f"https://reddit.com{pd_.get('permalink', '')}",
                        ))

                    await asyncio.sleep(0.5)  # Rate limit: be gentle with Reddit

                except Exception as exc:
                    logger.debug(f"Reddit fetch failed for {clean_symbol} in r/{subreddit}: {exc}")
                    continue

        logger.info(f"Reddit: fetched {len(items)} posts")
        return items

    # ------------------------------------------------------------------
    # X/Twitter via Nitter RSS (no API key required)
    # ------------------------------------------------------------------

    async def _fetch_nitter(self, symbols: list[str]) -> list[NewsItem]:
        """
        Fetch tweets from key financial accounts via Nitter RSS.
        Nitter is an open-source Twitter frontend that provides RSS feeds.
        Tries multiple Nitter instances — skips gracefully if all fail.
        """
        if not self._general_session:
            return []

        # Find a working Nitter instance
        working_instance = await self._find_nitter_instance()
        if not working_instance:
            logger.warning("X/Twitter: No working Nitter instance found, skipping")
            return []

        items: list[NewsItem] = []
        symbol_set = {s.replace(".NS", "").replace(".BO", "").upper() for s in symbols}

        for account in X_ACCOUNTS:
            try:
                rss_url = f"{working_instance}/{account}/rss"
                resp = await self._general_session.get(rss_url, timeout=10)
                resp.raise_for_status()

                root    = ET.fromstring(resp.text)
                entries = root.findall(".//item")

                for entry in entries:
                    try:
                        title    = (entry.findtext("title") or "").strip()
                        desc     = re.sub(r"<[^>]+>", " ", entry.findtext("description") or "")
                        desc     = re.sub(r"\s+", " ", desc).strip()
                        pub_date = entry.findtext("pubDate") or ""
                        link     = entry.findtext("link") or ""
                        ts       = _parse_rss_date(pub_date)

                        if ts < self.cutoff or not title:
                            continue

                        full_text = f"{title} {desc}".upper()

                        # Only keep tweets mentioning watchlist symbols
                        matched_symbols = [
                            s for s in symbols
                            if s.replace(".NS", "").replace(".BO", "").upper() in full_text
                        ]
                        if not matched_symbols and account not in ("NSEIndia", "BSEIndia"):
                            continue

                        for sym in (matched_symbols or [""]):
                            items.append(NewsItem(
                                symbol=sym,
                                headline=title[:200],
                                body=desc[:1000],
                                source="X/Twitter",
                                published_at=ts,
                                url=link,
                            ))
                    except Exception:
                        continue

                await asyncio.sleep(0.3)

            except Exception as exc:
                logger.debug(f"Nitter fetch failed for @{account}: {exc}")
                continue

        logger.info(f"X/Twitter (Nitter): fetched {len(items)} tweets")
        return items

    async def _find_nitter_instance(self) -> str | None:
        """Try Nitter instances in order, return first working one."""
        if not self._general_session:
            return None
        for instance in NITTER_INSTANCES:
            try:
                resp = await self._general_session.get(
                    f"{instance}/NSEIndia/rss", timeout=8
                )
                if resp.status_code == 200:
                    logger.info(f"Using Nitter instance: {instance}")
                    return instance
            except Exception:
                continue
        return None
