"""
Strategy Selector Agent: Uses Claude to determine market regime and adjust strategy weights.
Runs at 9:00am IST before market opens.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic
import pandas as pd

from config.settings import get_settings
from monitoring.audit_trail import AuditTrail
from strategies.registry import StrategyRegistry

logger = logging.getLogger(__name__)

STRATEGY_SELECTOR_PROMPT = """You are a quantitative trading strategy selector for Indian equity markets (NSE/BSE).

Based on the current market regime indicators provided, recommend which strategies should be emphasized
and their relative weight (0.0 to 1.0 each, sum to 1.0).

Available strategies:
- momentum: Works best in trending markets with clear directional bias
- mean_reversion: Works best in sideways, range-bound markets with high volatility
- breakout: Works best at key technical levels with volume confirmation
- sentiment_driven: Works when there are strong fundamental catalysts from news

Market regime indicators:
{regime_data}

Respond in JSON ONLY:
{{
  "regime": "<trending|sideways|volatile|uncertain>",
  "regime_reasoning": "<1-2 sentences>",
  "strategy_weights": {{
    "momentum": <0.0-1.0>,
    "mean_reversion": <0.0-1.0>,
    "breakout": <0.0-1.0>,
    "sentiment_driven": <0.0-1.0>
  }},
  "risk_level": "<low|medium|high>",
  "risk_reasoning": "<1 sentence>",
  "market_notes": "<any specific observations about current Indian market conditions>"
}}"""


class StrategySelector:
    def __init__(self, registry: StrategyRegistry, audit: AuditTrail):
        self.registry = registry
        self.audit = audit
        settings = get_settings()
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.anthropic_model

    def _build_regime_data(
        self,
        nifty_df: Optional[pd.DataFrame] = None,
        vix: Optional[float] = None,
        advance_decline: Optional[dict] = None,
    ) -> str:
        """Build a regime summary string from available indicators."""
        lines = []

        if nifty_df is not None and not nifty_df.empty:
            close = nifty_df["close"].iloc[-1]
            close_5d = nifty_df["close"].iloc[-5] if len(nifty_df) >= 5 else close
            close_20d = nifty_df["close"].iloc[-20] if len(nifty_df) >= 20 else close
            lines.append(f"Nifty 50: {close:.1f} (5d chg: {(close/close_5d-1)*100:.1f}%, 20d chg: {(close/close_20d-1)*100:.1f}%)")

            if "rsi" in nifty_df.columns:
                rsi = nifty_df["rsi"].iloc[-1]
                lines.append(f"Nifty RSI(14): {rsi:.1f}")

            if "adx" in nifty_df.columns:
                adx = nifty_df["adx"].iloc[-1]
                lines.append(f"Nifty ADX(14): {adx:.1f} (>25 = trending)")

            if "bb_upper" in nifty_df.columns and "bb_lower" in nifty_df.columns:
                bb_width = (nifty_df["bb_upper"].iloc[-1] - nifty_df["bb_lower"].iloc[-1]) / close * 100
                lines.append(f"BB Width: {bb_width:.1f}% (high = volatile)")

        if vix is not None:
            lines.append(f"India VIX: {vix:.2f} (<15 calm, >25 fearful)")

        if advance_decline:
            lines.append(f"Advance/Decline: {advance_decline.get('advances', 'N/A')}/{advance_decline.get('declines', 'N/A')}")

        if not lines:
            return "Market regime data not available. Use balanced weights."

        return "\n".join(lines)

    async def select_strategies(
        self,
        nifty_df: Optional[pd.DataFrame] = None,
        vix: Optional[float] = None,
        advance_decline: Optional[dict] = None,
    ) -> dict:
        """
        Ask Claude to select strategy weights based on current market regime.
        Returns dict with strategy weights and regime assessment.
        """
        regime_data = self._build_regime_data(nifty_df, vix, advance_decline)
        prompt = STRATEGY_SELECTOR_PROMPT.format(regime_data=regime_data)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )

            raw = response.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()

            result = json.loads(raw)
            weights = result.get("strategy_weights", {})

            logger.info(
                f"Strategy regime: {result.get('regime')} | "
                f"Weights: {weights} | "
                f"Risk: {result.get('risk_level')}"
            )
            self.audit.log_agent_decision(
                "StrategySelector",
                result.get("regime_reasoning", ""),
                {"regime": result.get("regime"), "weights": weights},
            )
            return result

        except Exception as exc:
            logger.error(f"Strategy selection failed: {exc}")
            # Default: equal weights
            return {
                "regime": "uncertain",
                "regime_reasoning": f"LLM unavailable: {exc}",
                "strategy_weights": {
                    "momentum": 0.25,
                    "mean_reversion": 0.25,
                    "breakout": 0.25,
                    "sentiment_driven": 0.25,
                },
                "risk_level": "medium",
            }
