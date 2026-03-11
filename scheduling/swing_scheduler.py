"""
Swing trading scheduler: EOD and pre-market daily cycles.
"""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from scheduling.market_calendar import is_trading_day

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class SwingScheduler:
    def __init__(self, orchestrator, symbols: list[str]):
        self.orchestrator = orchestrator
        self.symbols = symbols
        self.scheduler = AsyncIOScheduler(timezone=IST)
        self._setup_jobs()

    def _setup_jobs(self) -> None:
        # Pre-market swing scan at 9:00am IST (before market opens)
        self.scheduler.add_job(
            self._pre_market_swing_scan,
            CronTrigger(
                day_of_week="mon-fri",
                hour=9,
                minute=0,
                timezone=IST,
            ),
            id="swing_premarket",
            name="Pre-Market Swing Scan",
        )

        # EOD swing review at 4:00pm IST
        self.scheduler.add_job(
            self._eod_swing_review,
            CronTrigger(
                day_of_week="mon-fri",
                hour=16,
                minute=0,
                timezone=IST,
            ),
            id="swing_eod",
            name="EOD Swing Review",
        )

        # Daily auth refresh at 8:30am IST
        self.scheduler.add_job(
            self._daily_auth_refresh,
            CronTrigger(
                day_of_week="mon-fri",
                hour=8,
                minute=30,
                timezone=IST,
            ),
            id="daily_auth",
            name="Daily Auth Token Refresh",
        )

    async def _pre_market_swing_scan(self) -> None:
        """9:00am: Fetch daily bars and run swing strategy analysis."""
        if not is_trading_day():
            return
        logger.info("Pre-market: running swing strategy scan")
        try:
            await self.orchestrator.run_swing_cycle(self.symbols)
        except Exception as exc:
            logger.error(f"Pre-market swing scan error: {exc}")

    async def _eod_swing_review(self) -> None:
        """4:00pm: Review swing positions, update trailing stops for overnight."""
        if not is_trading_day():
            return
        logger.info("EOD: reviewing swing positions")
        portfolio = self.orchestrator.portfolio
        positions = portfolio.get_swing_positions()
        logger.info(f"Active swing positions: {len(positions)}")
        for pos in positions:
            logger.info(f"  {pos.symbol}: qty={pos.qty} avg={pos.avg_price:.2f} stop={pos.stop_loss:.2f}")

    async def _daily_auth_refresh(self) -> None:
        """8:30am: Refresh KiteConnect access token."""
        from config.settings import get_settings
        settings = get_settings()
        if not settings.kite_api_key:
            logger.warning("No Kite API key configured, skipping auth refresh")
            return
        try:
            from auth.kite_auth import run_daily_auth
            from pathlib import Path
            token = await run_daily_auth(
                api_key=settings.kite_api_key,
                api_secret=settings.kite_api_secret,
                user_id=settings.zerodha_user_id,
                password=settings.zerodha_password,
                totp_secret=settings.zerodha_totp_secret,
                cache_path=settings.token_cache_path,
            )
            from data.kite_client import init_kite
            init_kite(settings.kite_api_key, token)
            logger.info("Daily auth refresh complete, KiteConnect re-initialized")
        except Exception as exc:
            logger.error(f"Daily auth refresh failed: {exc}")
            if self.orchestrator.alerter:
                await self.orchestrator.alerter.alert_error("Auth", str(exc))

    def start(self) -> None:
        self.scheduler.start()
        logger.info("SwingScheduler started")

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        logger.info("SwingScheduler stopped")
