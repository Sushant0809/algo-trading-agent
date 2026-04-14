"""
Sentiment Agent: Uses Claude to rate NSE/BSE news sentiment (-10 to +10).

Improvements over v1:
  - Accepts staleness_weight and source_credibility from NewsItem; passes them
    to the prompt so Claude knows which articles are fresh vs stale.
  - Processes up to 10 articles per symbol (was 5).
  - Body text increased to 1000 chars (matches new NewsItem).
  - Weighted score: raw Claude score × (staleness_weight × source_credibility)
    averaged across all articles gives a final adjusted_score.
  - Negative sentiment gate: score ≤ -7 sets signal_type="short_candidate".
  - score ≥ +7 sets signal_type="long_candidate".
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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
LONG_THRESHOLD  = 7.0   # adjusted_score ≥ this → long_candidate
SHORT_THRESHOLD = -7.0  # adjusted_score ≤ this → short_candidate

SENTIMENT_SYSTEM_PROMPT = """You are a financial sentiment analyst specialising in Indian equity markets (NSE/BSE).

You will be given news headlines and corporate announcements for a specific stock.
Each article includes:
  - [AGE]    how old the article is (staleness weight 0.0–1.0; 1.0 = very fresh)
  - [SOURCE] the publication and its credibility weight (0.0–1.0)

Rate the MARKET SENTIMENT for this stock from -10 to +10:
  -10 = Extremely bearish (company default, major fraud, catastrophic miss)
   -7 = Very bearish (large earnings miss, serious regulatory issues)
   -4 = Moderately bearish (weak guidance, minor bad news)
    0 = Neutral (no significant impact)
   +4 = Moderately bullish (beat estimates, positive guidance)
   +7 = Very bullish (major contract win, strong earnings beat, sector tailwind)
  +10 = Extremely bullish (acquisition at premium, major positive surprise)

Guidelines:
1. Fresh articles (staleness ≥ 0.8) and credible sources (credibility ≥ 0.85) carry
   more weight — reflect this in your score.
2. Stale articles (staleness < 0.5) that are already public knowledge should score
   closer to 0 even if the news was originally significant.
3. Consider near-term price impact (1–5 days) and Indian equity market context.
4. If the same story appears multiple times from different sources, do NOT amplify —
   treat it as a single data point.

Respond in JSON ONLY (no markdown):
{
  "score": <float -10 to +10>,
  "confidence": <float 0 to 1>,
  "key_factors": ["factor1", "factor2"],
  "reasoning": "<1-2 sentence explanation>",
  "already_priced_in": <bool>
}"""


def _format_news_for_prompt(items: list[NewsItem]) -> str:
    """Format up to 10 NewsItems into a structured text block for the prompt."""
    lines = []
    for i, item in enumerate(items[:10], 1):
        age_label = f"staleness={item.staleness_weight:.2f}"
        cred_label = f"credibility={item.source_credibility:.2f}"
        ts = item.published_at.strftime("%Y-%m-%d %H:%M UTC")
        body_snippet = item.body[:1000].strip() if item.body else ""
        entry = (
            f"[{i}] [{ts}] [AGE {age_label}] [SOURCE: {item.source} | {cred_label}]\n"
            f"Headline: {item.headline}"
        )
        if body_snippet:
            entry += f"\n{body_snippet}"
        lines.append(entry)
    return "\n\n".join(lines)


def _weighted_score(raw_score: float, items: list[NewsItem]) -> float:
    """
    Adjust raw Claude score by the average weight of the news items provided.
    weight_i = staleness_weight_i × source_credibility_i
    adjusted = raw_score × mean(weights)
    This shrinks the score when all news is stale/low-credibility.
    """
    if not items:
        return raw_score
    weights = [item.staleness_weight * item.source_credibility for item in items[:10]]
    avg_weight = sum(weights) / len(weights)
    return raw_score * avg_weight


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
        Returns enriched dict with score, adjusted_score, signal_type, confidence, reasoning.
        """
        if not news_items:
            return None

        news_text = _format_news_for_prompt(news_items)
        user_msg = f"Stock: {symbol} (NSE/BSE)\n\nRecent news ({len(news_items[:10])} articles):\n\n{news_text}"

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=512,
                system=SENTIMENT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )

            raw = response.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()

            result = json.loads(raw)
            result["symbol"] = symbol
            result["news_count"] = len(news_items)

            # Adjust score for freshness/credibility
            raw_score = float(result.get("score", 0.0))
            adjusted = _weighted_score(raw_score, news_items[:10])
            result["adjusted_score"] = round(adjusted, 3)

            # Determine signal type
            if adjusted >= LONG_THRESHOLD:
                result["signal_type"] = "long_candidate"
            elif adjusted <= SHORT_THRESHOLD:
                result["signal_type"] = "short_candidate"
            else:
                result["signal_type"] = "neutral"

            logger.info(
                f"Sentiment [{symbol}]: raw={raw_score:+.1f} adjusted={adjusted:+.3f} "
                f"signal={result['signal_type']} confidence={result.get('confidence')} | "
                f"{result.get('reasoning', '')[:80]}"
            )
            self.audit.log_agent_decision(
                "SentimentAgent",
                result.get("reasoning", ""),
                {
                    "symbol": symbol,
                    "raw_score": raw_score,
                    "adjusted_score": adjusted,
                    "signal_type": result["signal_type"],
                },
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
        Fetch news for all symbols, score them, return results keyed by symbol.
        Callers can filter by signal_type == 'long_candidate' or 'short_candidate'.
        """
        results: dict[str, dict] = {}

        async with NewsFetcher(lookback_hours=lookback_hours) as fetcher:
            news_map = await fetcher.fetch_news_for_watchlist(symbols)

        logger.info(f"News found for {len(news_map)}/{len(symbols)} symbols")

        for symbol, news_items in news_map.items():
            score_data = await self.score_news(symbol, news_items)
            if score_data:
                results[symbol] = score_data

        # Summary log
        longs  = [s for s, d in results.items() if d.get("signal_type") == "long_candidate"]
        shorts = [s for s, d in results.items() if d.get("signal_type") == "short_candidate"]
        if longs:
            logger.info(f"Long candidates:  {longs}")
        if shorts:
            logger.info(f"Short candidates: {shorts}")

        return results
