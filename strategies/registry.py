"""
Strategy registry: manages active strategies and routes symbols to appropriate strategies.
"""
from __future__ import annotations

import logging
from typing import Optional

from config.risk_params_loader import load_strategy_params
from strategies.base import BaseStrategy
from strategies.breakout import BreakoutStrategy
from strategies.llm_strategy import LLMStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumStrategy
from strategies.overbought_short import OverboughtShortStrategy
from strategies.oversold_bounce import OversoldBounceStrategy
from strategies.sentiment_driven import SentimentDrivenStrategy

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """Manages all active strategies. Add/remove strategies without code changes."""

    def __init__(self, enabled: list[str] | None = None):
        params = load_strategy_params()
        self._strategies: dict[str, BaseStrategy] = {}
        self._enabled = enabled or [
            "momentum", "mean_reversion", "breakout",
            "oversold_bounce", "overbought_short",
            "sentiment_driven", "llm_strategy",
        ]

        all_strategies = {
            "momentum":        MomentumStrategy(params.get("momentum", {})),
            "mean_reversion":  MeanReversionStrategy(params.get("mean_reversion", {})),
            "breakout":        BreakoutStrategy(params.get("breakout", {})),
            "oversold_bounce": OversoldBounceStrategy(params.get("oversold_bounce", {})),
            "overbought_short": OverboughtShortStrategy(params.get("overbought_short", {})),
            "sentiment_driven": SentimentDrivenStrategy(params.get("sentiment_driven", {})),
            "llm_strategy":    LLMStrategy(params.get("llm_strategy", {})),
        }

        for name in self._enabled:
            if name in all_strategies:
                self._strategies[name] = all_strategies[name]
                logger.info(f"Registered strategy: {name}")
            else:
                logger.warning(f"Unknown strategy: {name}")

    def get(self, name: str) -> Optional[BaseStrategy]:
        return self._strategies.get(name)

    def all(self) -> list[BaseStrategy]:
        return list(self._strategies.values())

    def names(self) -> list[str]:
        return list(self._strategies.keys())

    def enable(self, name: str) -> None:
        """Enable a strategy by name (reloads with current params)."""
        params = load_strategy_params()
        strategy_map = {
            "momentum":         lambda: MomentumStrategy(params.get("momentum", {})),
            "mean_reversion":   lambda: MeanReversionStrategy(params.get("mean_reversion", {})),
            "breakout":         lambda: BreakoutStrategy(params.get("breakout", {})),
            "oversold_bounce":  lambda: OversoldBounceStrategy(params.get("oversold_bounce", {})),
            "overbought_short": lambda: OverboughtShortStrategy(params.get("overbought_short", {})),
            "sentiment_driven": lambda: SentimentDrivenStrategy(params.get("sentiment_driven", {})),
            "llm_strategy":     lambda: LLMStrategy(params.get("llm_strategy", {})),
        }
        if name in strategy_map:
            self._strategies[name] = strategy_map[name]()
            logger.info(f"Enabled strategy: {name}")

    def disable(self, name: str) -> None:
        """Disable a strategy."""
        if name in self._strategies:
            del self._strategies[name]
            logger.info(f"Disabled strategy: {name}")
