"""
Telegram alerting for fills, P&L summaries, and kill switch events.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)

    async def send(self, message: str) -> None:
        if not self._enabled:
            logger.debug(f"[Telegram disabled] {message}")
            return
        try:
            import httpx
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"})
        except Exception as exc:
            logger.warning(f"Telegram send failed: {exc}")

    async def alert_fill(self, symbol: str, action: str, qty: int, price: float, product: str) -> None:
        msg = (
            f"<b>{'BUY' if action == 'BUY' else 'SELL'} FILLED</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Qty: {qty} @ ₹{price:.2f}\n"
            f"Product: {product}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S IST')}"
        )
        await self.send(msg)

    async def alert_kill_switch(self, reason: str) -> None:
        msg = f"<b>KILL SWITCH TRIGGERED</b>\nReason: {reason}"
        await self.send(msg)

    async def alert_daily_pnl(self, realized: float, unrealized: float, total: float) -> None:
        emoji = "📈" if realized >= 0 else "📉"
        msg = (
            f"{emoji} <b>Daily P&L Summary</b>\n"
            f"Realized: ₹{realized:,.2f}\n"
            f"Unrealized: ₹{unrealized:,.2f}\n"
            f"Portfolio: ₹{total:,.2f}"
        )
        await self.send(msg)

    async def alert_error(self, component: str, error: str) -> None:
        msg = f"<b>ERROR [{component}]</b>\n{error[:500]}"
        await self.send(msg)
