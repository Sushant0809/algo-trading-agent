"""
Sentiment Agent: Uses Claude to rate NSE news sentiment (-10 to +10).
Publishes sentiment-driven entry signals when score ≥ threshold.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic

from config.settings import get_settings
from data.news_fetcher import NewsItem, NewsFetcher
from monitoring.audit_trail import AuditTrail
from signals.signal_bus import SignalBus
from signals.signal_model import TradingMode

logger = logging.getLogger(__name__)

SENTIMENT_SYSTEM_PROMPT = """You are a financial sentiment analyst specializing in Indian equity markets (NSE/BSE).

You will be given news headlines and corporate announcements for a specific stock.
Rate the MARKET SENTIMENT for this stock from -10 to +10:
  -10 = Extremely bearish (company default, major fraud, catastrophic earnings miss)
  -7  = Very bearish (large earnings miss, serious regulatory issues)
  -4  = Moderately bearish (weak guidance, minor bad news)
   0  = Neutral (no significant impact)
  +4  = Moderately bullish (beat estimates, positive management guidance)
  +7  = Very bullish (major contract win, strong earnings beat, positive sector tailwind)
  +10 = Extremely bullish (acquisition at premium, major positive surprise)

Consider:
1. Impact on near-term price (1-5 days)
2. Whether news is already priced in (stale or old news scores lower)
3. Market context for Indian equities
4. Credibility of news source

Respond in JSON format ONLY:
{
  "score": <float -10 to +10>,
  "confidence": <float 0 to 1>,
  "key_factors": ["factor1", "factor2"],
  "reasoning": "<1-2 sentence explanation>",
  "already_priced_in": <bool>
}"""


class SentimentAgent:
    def __init__(self, signal_bus: SignalBus, audit: AuditTrail):
        self.bus = signal_bus
        self.audit = audit
        settings = get_settings()
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def score_news(self, symbol: str, news_items: list[NewsItem]) -> Optional[dict]:
        """
        Use Claude to score sentiment for a symbol's news.
        Returns dict with score, confidence, reasoning.
        """
        if not news_items:
            return None

        # Prepare news text
        news_text = "\n\n".join([
            f"[{item.published_at.strftime('%Y-%m-%d %H:%M')}] {item.headline}\n{item.body[:300]}"
            for item in news_items[:5]  # Limit to 5 most recent
        ])

        user_msg = f"Stock: {symbol} (NSE)\n\nRecent news:\n{news_text}"

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=512,
                system=SENTIMENT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )

            raw = response.content[0].text.strip()
            # Extract JSON
            if "```" in raw:
                raw = raw.split("```")[1].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()

            result = json.loads(raw)
            result["symbol"] = symbol
            result["news_count"] = len(news_items)

            logger.info(
                f"Sentiment [{symbol}]: score={result.get('score')}/10 "
                f"confidence={result.get('confidence')} | {result.get('reasoning', '')[:80]}"
            )
            self.audit.log_agent_decision(
                "SentimentAgent",
                result.get("reasoning", ""),
                {"symbol": symbol, "score": result.get("score")},
            )
            return result

        except Exception as exc:
            logger.error(f"Sentiment scoring failed for {symbol}: {exc}")
            return None

    async def scan_and_signal(
        self,
        symbols: list[str],
        lookback_hours: int = 24,
    ) -> dict[str, dict]:
        """
        Fetch news for all symbols, score them, return results.
        High-scoring symbols should be passed to SentimentDrivenStrategy.
        """
        results: dict[str, dict] = {}

        async with NewsFetcher(lookback_hours=lookback_hours) as fetcher:
            news_map = await fetcher.fetch_news_for_watchlist(symbols)

        logger.info(f"News found for {len(news_map)}/{len(symbols)} symbols")

        for symbol, news_items in news_map.items():
            score_data = await self.score_news(symbol, news_items)
            if score_data:
                results[symbol] = score_data

        return results
