"""
Intraday scheduler: runs 5-minute analysis cycles from 9:15am to 3:15pm IST.
Uses APScheduler with asyncio.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from scheduling.market_calendar import is_intraday_entry_allowed, is_trading_day, should_close_mis

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class IntradayScheduler:
    def __init__(self, orchestrator, symbols: list[str]):
        self.orchestrator = orchestrator
        self.symbols = symbols
        self.scheduler = AsyncIOScheduler(timezone=IST)
        self._setup_jobs()

    def _setup_jobs(self) -> None:
        # 5-minute intraday scan cycle: 9:15am to 3:15pm IST on weekdays
        self.scheduler.add_job(
            self._intraday_cycle,
            CronTrigger(
                day_of_week="mon-fri",
                hour="9-15",
                minute="*/5",
                timezone=IST,
            ),
            id="intraday_5min",
            name="5min Intraday Scan",
            max_instances=1,  # Don't overlap
            coalesce=True,    # Skip missed runs
        )

        # EOD MIS close at 3:15pm IST
        self.scheduler.add_job(
            self._eod_close,
            CronTrigger(
                day_of_week="mon-fri",
                hour=15,
                minute=15,
                timezone=IST,
            ),
            id="eod_mis_close",
            name="EOD MIS Position Close",
        )

        # Daily P&L report at 3:35pm IST
        self.scheduler.add_job(
            self._daily_report,
            CronTrigger(
                day_of_week="mon-fri",
                hour=15,
                minute=35,
                timezone=IST,
            ),
            id="daily_report",
            name="Daily P&L Report",
        )

    async def _intraday_cycle(self) -> None:
        """Run 5-minute intraday scan cycle."""
        if not is_trading_day():
            return
        if should_close_mis():
            return  # Past cutoff

        now = datetime.now(IST).strftime("%H:%M")
        logger.info(f"[{now} IST] Running intraday 5-min cycle ({len(self.symbols)} symbols)")

        try:
            await self.orchestrator.run_intraday_cycle(self.symbols)
        except Exception as exc:
            logger.error(f"Intraday cycle error: {exc}")

    async def _eod_close(self) -> None:
        """Force-close all MIS positions at 3:15pm IST."""
        if not is_trading_day():
            return
        logger.info("EOD: triggering MIS position close")
        try:
            await self.orchestrator.portfolio_agent.close_all_mis()
        except Exception as exc:
            logger.error(f"EOD close error: {exc}")

    async def _daily_report(self) -> None:
        """Generate daily P&L report with benchmark comparison."""
        if not is_trading_day():
            return
        try:
            portfolio  = self.orchestrator.portfolio
            alerter    = self.orchestrator.alerter
            audit      = self.orchestrator.audit
            benchmark  = self.orchestrator.benchmark

            summary = portfolio.summary()
            logger.info(f"Daily summary: {summary}")
            audit.log_pnl(
                realized=portfolio.daily_realized_pnl,
                unrealized=0.0,
                total_capital=portfolio.total_capital,
            )

            # Record benchmark metrics
            bm_metrics = await benchmark.record_daily(portfolio.total_capital)
            audit.log_agent_decision("BenchmarkTracker", benchmark.summary_text(), bm_metrics)

            if alerter:
                msg = (
                    f"Daily P&L: ₹{portfolio.daily_realized_pnl:+,.0f} | "
                    f"{benchmark.summary_text()}"
                )
                await alerter.alert_daily_pnl(
                    portfolio.daily_realized_pnl, 0.0, portfolio.total_capital
                )
        except Exception as exc:
            logger.error(f"Daily report error: {exc}")

    def start(self) -> None:
        self.scheduler.start()
        logger.info("IntradayScheduler started (5min cycles, IST)")

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        logger.info("IntradayScheduler stopped")
