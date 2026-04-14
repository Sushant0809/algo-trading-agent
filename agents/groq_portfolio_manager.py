"""
Groq-backed portfolio manager using Llama/Mixtral via Groq API.
Cost-efficient alternative: ~10x cheaper than Claude Haiku.
"""
import httpx
import logging
import os

import pandas as pd

from agents.llm_base import AllocationDecision, BaseLLMPortfolioManager

logger = logging.getLogger(__name__)


class GroqPortfolioManager(BaseLLMPortfolioManager):
    """Portfolio manager using Groq API (ultra-fast open-source models)."""

    # Available models on Groq (updated list)
    AVAILABLE_MODELS = {
        "llama-3.3-70b": "llama-3.3-70b-versatile",  # Best quality, closest to Claude Haiku
        "llama-3.1-70b": "llama-3.1-70b-specdec",
        "llama-3.1-8b": "llama-3.1-8b-instant",
        "mixtral-8x7b": "mixtral-8x7b-32768",
    }

    def __init__(
        self,
        model: str = "llama-3.3-70b-versatile",
        max_tokens: int = 2000,
        temperature: float = 0.2,
        cash_reserve_pct: float = 0.10,
    ):
        super().__init__(cash_reserve_pct=cash_reserve_pct)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_key = os.getenv("GROQ_API_KEY")

        if not self.api_key:
            raise ValueError("GROQ_API_KEY not set in environment")

        logger.info(f"Initialized Groq Portfolio Manager with model: {self.model}")

    async def decide(
        self,
        symbol_data: dict[str, pd.DataFrame],
        portfolio_cash: float,
        portfolio_value: float,
        holdings: dict[str, float],
        avg_costs: dict[str, float],
        daily_pnl: float = 0.0,
        drawdown_pct: float = 0.0,
        nifty_df: pd.DataFrame | None = None,
        entry_dates: dict[str, object] | None = None,
        today: object | None = None,
        trade_journal: list[dict] | None = None,
    ) -> AllocationDecision:
        """
        Ask Groq LLM for allocation decisions.
        Ultra-fast inference, very affordable.
        """
        prompt = self._build_prompt(
            symbol_data, portfolio_cash, portfolio_value,
            holdings, avg_costs, daily_pnl, drawdown_pct, nifty_df,
            entry_dates=entry_dates or {},
            today=today,
            trade_journal=trade_journal,
        )

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": self.SYSTEM_PROMPT,
                            },
                            {
                                "role": "user",
                                "content": prompt,
                            }
                        ],
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                        "top_p": 0.95,
                    },
                    timeout=60.0,
                )

                if response.status_code != 200:
                    logger.error(f"Groq API error {response.status_code}: {response.text}")
                    raw_text = ""
                else:
                    data = response.json()
                    raw_text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    logger.debug(f"GroqPortfolioManager response ({len(raw_text)} chars)")

        except Exception as exc:
            logger.error(f"GroqPortfolioManager API call failed: {exc}")
            raw_text = ""

        allocations = self._parse_response(raw_text, list(symbol_data.keys()))
        decision = AllocationDecision(
            allocations=allocations,
            model_reasoning=raw_text[:500],
        )
        logger.info(
            f"GroqPortfolioManager ({self.model}): {len(decision.buys())} buys, {len(decision.sells())} sells "
            f"| {decision.summary()}"
        )
        return decision
