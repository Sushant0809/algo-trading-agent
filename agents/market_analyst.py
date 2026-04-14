"""
Market Analyst Agent: runs TA indicators → generates signals (no LLM).
Processes all symbols in the active universe.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import pandas as pd

from config.instruments import get_token
from data.cache import load_bars, save_bars
from data.market_data import fetch_latest_bars
from signals.indicators import add_vwap, compute_all_indicators
from signals.signal_bus import SignalBus
from signals.signal_model import Signal, TradingMode
from strategies.registry import StrategyRegistry

logger = logging.getLogger(__name__)


class MarketAnalyst:
    """
    Runs all registered strategies on each symbol and publishes signals to the bus.
    No LLM involved — pure technical analysis.
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        signal_bus: SignalBus,
        intraday_interval: str = "5minute",
        swing_interval: str = "day",
        intraday_bars: int = 200,
        swing_bars: int = 400,
    ):
        self.registry = registry
        self.bus = signal_bus
        self.intraday_interval = intraday_interval
        self.swing_interval = swing_interval
        self.intraday_bars = intraday_bars
        self.swing_bars = swing_bars

    async def analyze_symbol(
        self,
        symbol: str,
        mode: TradingMode,
    ) -> list[Signal]:
        """Run all strategies on a symbol and return generated signals."""
        token = get_token(symbol)
        if not token:
            logger.debug(f"No instrument token for {symbol}, skipping")
            return []

        interval = self.intraday_interval if mode == TradingMode.INTRADAY else self.swing_interval
        n_bars = self.intraday_bars if mode == TradingMode.INTRADAY else self.swing_bars

        # Fetch data (cached)
        try:
            # fetch_latest_bars is async; cache wrapper handles it
            cached = load_bars(token, interval)
            if cached is not None:
                df = cached
            else:
                df = await fetch_latest_bars(token, interval, n_bars)
                save_bars(token, interval, df)
        except Exception as exc:
            logger.warning(f"Data fetch failed for {symbol}: {exc}")
            return []

        if df.empty or len(df) < 30:
            return []

        # Add indicators
        try:
            df = compute_all_indicators(df)
            if mode == TradingMode.INTRADAY:
                try:
                    df["vwap"] = add_vwap(df)
                except Exception:
                    pass  # VWAP may fail on daily data
        except Exception as exc:
            logger.warning(f"Indicator computation failed for {symbol}: {exc}")
            return []

        # Run all strategies
        signals: list[Signal] = []
        for strategy in self.registry.all():
            if strategy.name in ("sentiment_driven", "llm_strategy") and mode == TradingMode.INTRADAY:
                continue  # Sentiment and LLM strategies are swing-only
            try:
                # LLMStrategy uses async path to avoid blocking event loop
                if hasattr(strategy, "async_generate_signal"):
                    signal = await strategy.async_generate_signal(symbol, df, mode)
                else:
                    signal = strategy.generate_signal(symbol, df, mode)
                if signal:
                    signals.append(signal)
                    logger.info(f"Signal: {symbol} {signal.action.value} [{strategy.name}] mode={mode.value}")
            except Exception as exc:
                logger.warning(f"Strategy {strategy.name} failed on {symbol}: {exc}")

        return signals

    async def scan_universe(
        self,
        symbols: list[str],
        mode: TradingMode,
        max_concurrent: int = 5,
    ) -> None:
        """
        Analyze all symbols concurrently (rate-limited) and publish signals.
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def analyze_and_publish(symbol: str) -> None:
            async with semaphore:
                signals = await self.analyze_symbol(symbol, mode)
                for sig in signals:
                    await self.bus.publish_signal(sig)
                await asyncio.sleep(0.1)  # Gentle rate limiting

        tasks = [analyze_and_publish(sym) for sym in symbols]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"Universe scan complete: {len(symbols)} symbols, mode={mode.value}")
