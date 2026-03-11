"""
LangGraph-based orchestrator: coordinates all agents in the trading pipeline.
Uses StateGraph for state machine-style agent orchestration.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config.settings import get_settings
from config.universes import NIFTY50, NIFTY_MIDCAP_150_SAMPLE, NIFTY_SMALLCAP_250_SAMPLE

logger = logging.getLogger(__name__)


@dataclass
class TradingState:
    """Shared state object passed through LangGraph nodes."""
    symbols: list[str] = field(default_factory=list)
    filtered_symbols: dict[str, list[str]] = field(default_factory=dict)
    sentiment_scores: dict[str, dict] = field(default_factory=dict)
    strategy_weights: dict[str, float] = field(default_factory=dict)
    regime: str = "unknown"
    risk_level: str = "medium"
    session_start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    errors: list[str] = field(default_factory=list)
    is_running: bool = True


class TradingOrchestrator:
    """
    Master orchestrator that coordinates all agents.
    Uses asyncio tasks rather than LangGraph's sync loop for real-time operation.
    LangGraph state graph is used for the morning setup sequence.
    """

    def __init__(
        self,
        market_analyst,
        sentiment_agent,
        strategy_selector,
        risk_agent,
        execution_agent,
        portfolio_agent,
        signal_bus,
        portfolio_state,
        audit_trail,
        alerter=None,
    ):
        self.market_analyst = market_analyst
        self.sentiment_agent = sentiment_agent
        self.strategy_selector = strategy_selector
        self.risk_agent = risk_agent
        self.execution_agent = execution_agent
        self.portfolio_agent = portfolio_agent
        self.bus = signal_bus
        self.portfolio = portfolio_state
        self.audit = audit_trail
        self.alerter = alerter
        self.settings = get_settings()

    async def morning_setup(self) -> TradingState:
        """
        Pre-market morning sequence (8:45am–9:15am IST):
        1. Filter universe by liquidity
        2. Run sentiment scan on watchlist
        3. Select strategies based on market regime
        4. Pre-approve swing signals
        """
        state = TradingState()

        # --- Universe selection ---
        from config.universes import NIFTY50, NIFTY_BANK, NIFTY_IT
        state.symbols = NIFTY50 + NIFTY_BANK + NIFTY_IT  # Start with liquid universe
        logger.info(f"Morning setup: {len(state.symbols)} symbols in universe")

        # --- Sentiment scan ---
        logger.info("Running overnight sentiment scan...")
        try:
            scores = await self.sentiment_agent.scan_and_signal(
                state.symbols[:30],  # Top 30 for sentiment (API cost control)
                lookback_hours=24,
            )
            state.sentiment_scores = scores
            logger.info(f"Sentiment scores for {len(scores)} symbols")
        except Exception as exc:
            logger.error(f"Sentiment scan failed: {exc}")
            state.errors.append(f"Sentiment scan: {exc}")

        # --- Strategy selection ---
        logger.info("Selecting strategy weights for today...")
        try:
            from data.market_data import fetch_latest_bars
            from config.instruments import get_token
            from signals.indicators import compute_all_indicators

            nifty_token = get_token("NIFTY 50")
            nifty_df = None
            if nifty_token:
                nifty_df = fetch_latest_bars(nifty_token, "day", 60)
                if not nifty_df.empty:
                    nifty_df = compute_all_indicators(nifty_df)

            selection = await self.strategy_selector.select_strategies(nifty_df=nifty_df)
            state.strategy_weights = selection.get("strategy_weights", {})
            state.regime = selection.get("regime", "unknown")
            state.risk_level = selection.get("risk_level", "medium")
            logger.info(f"Regime: {state.regime} | Weights: {state.strategy_weights}")
        except Exception as exc:
            logger.error(f"Strategy selection failed: {exc}")
            state.errors.append(f"Strategy selection: {exc}")

        self.audit.log_agent_decision(
            "Orchestrator",
            f"Morning setup complete. Regime={state.regime}, risk={state.risk_level}",
            {"symbols_count": len(state.symbols), "regime": state.regime},
        )
        return state

    async def run_intraday_cycle(self, symbols: list[str]) -> None:
        """
        5-minute intraday analysis cycle.
        Runs market_analyst on all symbols → signals flow through bus automatically.
        """
        from signals.signal_model import TradingMode
        logger.debug(f"Intraday cycle: scanning {len(symbols)} symbols")
        await self.market_analyst.scan_universe(symbols, TradingMode.INTRADAY)

    async def run_swing_cycle(self, symbols: list[str]) -> None:
        """Daily swing analysis cycle."""
        from signals.signal_model import TradingMode
        logger.info(f"Swing cycle: scanning {len(symbols)} symbols")
        await self.market_analyst.scan_universe(symbols, TradingMode.SWING)

    async def run(self, symbols: list[str]) -> None:
        """
        Main event loop: launch all agent tasks concurrently.
        Agents communicate via signal_bus.
        """
        logger.info("Orchestrator: starting all agent tasks")

        tasks = [
            asyncio.create_task(self.risk_agent.run(), name="risk_agent"),
            asyncio.create_task(self.execution_agent.run(), name="execution_agent"),
            asyncio.create_task(self.portfolio_agent.run(), name="portfolio_agent"),
        ]

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            logger.error(f"Orchestrator task error: {exc}")
            for task in tasks:
                task.cancel()
            raise

    async def shutdown(self) -> None:
        """Graceful shutdown: close all positions, save state."""
        logger.info("Orchestrator: initiating shutdown")
        await self.portfolio_agent.close_all_mis()
        self.audit.log_pnl(
            realized=self.portfolio.daily_realized_pnl,
            unrealized=0.0,
            total_capital=self.portfolio.total_capital,
        )
        logger.info("Orchestrator: shutdown complete")
