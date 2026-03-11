"""
Append-only audit trail for all agent decisions and order events.
Written as newline-delimited JSON to logs/audit/audit_YYYYMMDD.jsonl
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditTrail:
    def __init__(self, audit_dir: Path = Path("./logs/audit")):
        self.audit_dir = audit_dir
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def _today_file(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        return self.audit_dir / f"audit_{today}.jsonl"

    def log(self, event_type: str, data: dict[str, Any]) -> None:
        """Append a structured event to today's audit log."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **data,
        }
        try:
            with self._today_file().open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            logger.error(f"Audit trail write failed: {exc}")

    def log_signal(self, signal: Any) -> None:
        self.log("SIGNAL_GENERATED", {"signal": signal if isinstance(signal, dict) else vars(signal)})

    def log_risk_decision(self, signal: Any, approved: bool, reason: str) -> None:
        self.log("RISK_DECISION", {
            "approved": approved,
            "reason": reason,
            "signal": signal if isinstance(signal, dict) else vars(signal),
        })

    def log_order(self, order_id: str, symbol: str, action: str, qty: int, price: float, product: str) -> None:
        self.log("ORDER_PLACED", {
            "order_id": order_id,
            "symbol": symbol,
            "action": action,
            "qty": qty,
            "price": price,
            "product": product,
        })

    def log_fill(self, order_id: str, fill_price: float, fill_qty: int) -> None:
        self.log("ORDER_FILLED", {
            "order_id": order_id,
            "fill_price": fill_price,
            "fill_qty": fill_qty,
        })

    def log_agent_decision(self, agent: str, reasoning: str, data: dict | None = None) -> None:
        self.log("AGENT_DECISION", {
            "agent": agent,
            "reasoning": reasoning,
            **(data or {}),
        })

    def log_kill_switch(self, reason: str) -> None:
        self.log("KILL_SWITCH_TRIGGERED", {"reason": reason})

    def log_pnl(self, realized: float, unrealized: float, total_capital: float) -> None:
        self.log("DAILY_PNL", {
            "realized": realized,
            "unrealized": unrealized,
            "total_capital": total_capital,
        })
