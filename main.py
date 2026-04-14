"""
Algo Trading Agent — CLI Entry Point
Usage:
  python main.py --mode paper --trading both
  python main.py --mode backtest --start 2023-01-01 --end 2024-12-31
  python main.py --mode live --trading intraday
"""
from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime
from pathlib import Path

import click

from config.settings import get_settings
from monitoring.logger import get_logger, setup_logging

logger = get_logger(__name__)


@click.group()
def cli():
    """AI Algorithmic Trading Agent for Indian Markets (NSE/BSE) via Zerodha."""
    pass


@cli.command()
@click.option(
    "--mode",
    type=click.Choice(["paper", "live", "backtest"]),
    default="paper",
    help="Trading mode: paper (virtual), live (real orders), backtest",
)
@click.option(
    "--trading",
    type=click.Choice(["intraday", "swing", "both"]),
    default="both",
    help="Trading type: intraday (MIS), swing (CNC), or both",
)
@click.option("--headless/--no-headless", default=True, help="Playwright headless mode for auth")
def run(mode: str, trading: str, headless: bool):
    """Run the trading agent in paper or live mode."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir)

    if mode == "live" and settings.is_paper:
        logger.warning("PAPER_TRADING=true in .env but mode=live requested. Overriding to paper mode for safety.")
        mode = "paper"

    click.echo(f"\n{'='*55}")
    click.echo(f"  Algo Trading Agent")
    click.echo(f"  Mode: {mode.upper()} | Trading: {trading.upper()}")
    click.echo(f"  Paper: {settings.is_paper}")
    click.echo(f"  Capital: ₹{settings.paper_trading_capital:,.0f}")
    click.echo(f"{'='*55}\n")

    asyncio.run(_run_trading(mode, trading, headless))


@cli.command()
@click.option("--start", type=str, required=True, help="Start date YYYY-MM-DD")
@click.option("--end", type=str, required=True, help="End date YYYY-MM-DD")
@click.option(
    "--strategy",
    type=click.Choice(["momentum", "mean_reversion", "breakout", "all"]),
    default="all",
)
@click.option(
    "--symbol",
    type=str,
    default="RELIANCE",
    help="Single symbol for backtest (or 'nifty50' for all)",
)
@click.option("--walk-forward/--no-walk-forward", default=False)
def backtest(start: str, end: str, strategy: str, symbol: str, walk_forward: bool):
    """Run backtests for strategies."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir)
    asyncio.run(_run_backtest(start, end, strategy, symbol, walk_forward))


@cli.command()
@click.option("--headless/--no-headless", default=True)
def auth_refresh(headless: bool):
    """Manually trigger Zerodha daily auth token refresh."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir)
    asyncio.run(_run_auth_refresh(headless))


# =============================================================================
# Implementation
# =============================================================================

async def _run_auth_refresh(headless: bool) -> None:
    from auth.kite_auth import run_daily_auth
    from data.kite_client import init_kite

    settings = get_settings()
    logger.info("Running manual auth refresh...")

    token = await run_daily_auth(
        api_key=settings.kite_api_key,
        api_secret=settings.kite_api_secret,
        user_id=settings.zerodha_user_id,
        password=settings.zerodha_password,
        totp_secret=settings.zerodha_totp_secret,
        cache_path=settings.token_cache_path,
        headless=headless,
    )
    init_kite(settings.kite_api_key, token)
    click.echo(f"Auth refresh successful. Token: {token[:8]}...")


async def _run_trading(mode: str, trading: str, headless: bool) -> None:
    settings = get_settings()

    # --- Auth & KiteConnect ---
    logger.info("Initializing KiteConnect...")
    from auth.kite_auth import load_cached_token, run_daily_auth
    from data.kite_client import init_kite, init_ticker

    # Try cached token first
    token = load_cached_token(settings.token_cache_path)
    if not token:
        if settings.kite_api_key and settings.zerodha_user_id:
            token = await run_daily_auth(
                api_key=settings.kite_api_key,
                api_secret=settings.kite_api_secret,
                user_id=settings.zerodha_user_id,
                password=settings.zerodha_password,
                totp_secret=settings.zerodha_totp_secret,
                cache_path=settings.token_cache_path,
                headless=headless,
            )
        else:
            token = settings.kite_access_token
            if not token:
                logger.error("No Kite credentials or cached token found. Set .env or run auth-refresh first.")
                sys.exit(1)

    kite = init_kite(settings.kite_api_key, token)

    # --- KiteTicker WebSocket (initialized here, connected after FillTracker setup) ---
    ticker = init_ticker(settings.kite_api_key, token)

    # --- Instrument Cache ---
    logger.info("Loading instrument cache...")
    from config.instruments import load_instruments, refresh_instruments
    fresh = load_instruments(settings.instrument_cache_path)
    if not fresh:
        logger.info("Refreshing instrument cache from KiteConnect...")
        refresh_instruments(kite, settings.instrument_cache_path)

    # --- Build components ---
    logger.info("Initializing trading components...")
    from monitoring.audit_trail import AuditTrail
    from monitoring.alerting import TelegramAlerter
    from risk.portfolio_state import PortfolioState
    from risk.risk_manager import RiskManager
    from signals.signal_bus import SignalBus
    from strategies.registry import StrategyRegistry
    from agents.market_analyst import MarketAnalyst
    from agents.sentiment_agent import SentimentAgent
    from agents.strategy_selector import StrategySelector
    from agents.risk_agent import RiskAgent
    from agents.execution_agent import ExecutionAgent
    from agents.portfolio_agent import PortfolioAgent
    from agents.orchestrator import TradingOrchestrator
    from execution.order_manager import OrderManager
    from execution.fill_tracker import FillTracker

    audit = AuditTrail(settings.log_dir / "audit")
    alerter = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
    portfolio = PortfolioState(initial_capital=settings.paper_trading_capital)

    # --- Wire FillTracker to portfolio ---
    logger.info("Wiring FillTracker to KiteTicker WebSocket...")
    fill_tracker = FillTracker(portfolio, audit)
    fill_tracker.setup_ticker_callbacks(ticker)
    ticker.connect()  # Start WebSocket connection
    signal_bus = SignalBus()
    registry = StrategyRegistry()
    risk_mgr = RiskManager(portfolio, audit)
    order_mgr = OrderManager(portfolio, audit, alerter)

    market_analyst = MarketAnalyst(registry, signal_bus)
    sentiment_agent = SentimentAgent(signal_bus, audit)
    strategy_selector = StrategySelector(registry, audit)
    risk_agent = RiskAgent(risk_mgr, portfolio, signal_bus, audit)
    execution_agent = ExecutionAgent(order_mgr, signal_bus, audit)
    portfolio_agent = PortfolioAgent(portfolio, signal_bus, risk_mgr, audit, alerter)

    orchestrator = TradingOrchestrator(
        market_analyst=market_analyst,
        sentiment_agent=sentiment_agent,
        strategy_selector=strategy_selector,
        risk_agent=risk_agent,
        execution_agent=execution_agent,
        portfolio_agent=portfolio_agent,
        signal_bus=signal_bus,
        portfolio_state=portfolio,
        audit_trail=audit,
        alerter=alerter,
    )

    # --- Universe ---
    from config.universes import NIFTY50, NIFTY_BANK, NIFTY_IT
    symbols = list(set(NIFTY50 + NIFTY_BANK + NIFTY_IT))

    # --- Schedulers ---
    from scheduling.intraday_scheduler import IntradayScheduler
    from scheduling.swing_scheduler import SwingScheduler

    intraday_sched = IntradayScheduler(orchestrator, symbols)
    swing_sched = SwingScheduler(orchestrator, symbols)

    if trading in ("intraday", "both"):
        intraday_sched.start()
    if trading in ("swing", "both"):
        swing_sched.start()

    # --- Morning setup ---
    logger.info("Running morning setup...")
    state = await orchestrator.morning_setup()
    logger.info(f"Morning setup complete: regime={state.regime}, risk={state.risk_level}")

    # --- Graceful shutdown ---
    loop = asyncio.get_event_loop()

    def _shutdown(sig):
        logger.info(f"Received {sig.name}, shutting down...")
        loop.create_task(_graceful_shutdown(orchestrator, intraday_sched, swing_sched))

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, lambda s=s: _shutdown(s))

    click.echo(f"\nAgent running. Press Ctrl+C to stop.\n")

    # Run core agent tasks
    await orchestrator.run(symbols)


async def _graceful_shutdown(orchestrator, intraday_sched, swing_sched) -> None:
    logger.info("Graceful shutdown initiated...")
    intraday_sched.stop()
    swing_sched.stop()
    await orchestrator.shutdown()
    sys.exit(0)


async def _run_backtest(start: str, end: str, strategy: str, symbol: str, walk_forward: bool) -> None:
    from datetime import date as date_cls
    import pandas as pd
    from data.historical import fetch_nsepy
    from backtesting.vectorbt_runner import run_momentum_backtest, run_mean_reversion_backtest, passes_promotion_gate
    from backtesting.report_generator import print_backtest_summary
    from backtesting.walk_forward import walk_forward_test

    start_d = date_cls.fromisoformat(start)
    end_d = date_cls.fromisoformat(end)

    logger.info(f"Fetching history for {symbol} ({start} → {end})")
    click.echo(f"Fetching NSE data for {symbol}...")

    df = fetch_nsepy(symbol, start_d, end_d)
    if df.empty:
        click.echo(f"No data found for {symbol}. Try another symbol.")
        return

    click.echo(f"Fetched {len(df)} daily bars for {symbol}")

    strategies_to_run = ["momentum", "mean_reversion"] if strategy == "all" else [strategy]

    for strat_name in strategies_to_run:
        click.echo(f"\nRunning {strat_name} backtest...")

        if strat_name == "momentum":
            results = run_momentum_backtest(df)
        elif strat_name == "mean_reversion":
            results = run_mean_reversion_backtest(df)
        else:
            results = run_momentum_backtest(df)

        print_backtest_summary(results, f"{strat_name.title()} [{symbol}]")

        if "error" not in results:
            passed, failures = passes_promotion_gate(results)
            if passed:
                click.echo(f"  PROMOTION GATE: PASSED")
            else:
                click.echo(f"  PROMOTION GATE: FAILED")
                for f in failures:
                    click.echo(f"    - {f}")

        if walk_forward and "error" not in results:
            click.echo(f"\nRunning walk-forward analysis...")
            fn = run_momentum_backtest if strat_name == "momentum" else run_mean_reversion_backtest
            wf = walk_forward_test(df, fn)
            print_backtest_summary(wf, f"{strat_name.title()} WFT [{symbol}]")


if __name__ == "__main__":
    cli()
