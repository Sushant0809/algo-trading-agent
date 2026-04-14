"""
Risk Agent: Hard rules first, then Claude review for borderline signals.
LLM is ONLY consulted when a signal passes hard rules but has unusual characteristics.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import anthropic

from config.settings import get_settings
from monitoring.audit_trail import AuditTrail
from risk.portfolio_allocator import PortfolioAllocator
from risk.portfolio_state import PortfolioState
from risk.risk_manager import KillSwitchError, RiskManager
from signals.signal_bus import SignalBus
from signals.signal_model import ApprovedSignal, Signal

logger = logging.getLogger(__name__)

RISK_REVIEW_PROMPT = """You are a risk manager reviewing a trading signal for Indian equity markets (NSE/BSE).

The signal has PASSED all hard rules. Your job is to flag any UNUSUAL RISKS that the rule engine may have missed.

Signal details:
{signal_data}

Portfolio state:
{portfolio_summary}

Look for:
1. Is the entry/stop/target ratio reasonable (minimum 1.5:1 reward:risk)?
2. Are there any red flags in the indicators that suggest a trap or false signal?
3. Is this signal going against a strong macro trend?
4. Any other unusual risk factors?

Respond in JSON ONLY:
{
  "approve": <true|false>,
  "concern_level": "<low|medium|high>",
  "flags": ["flag1", "flag2"],
  "reasoning": "<1-2 sentences>",
  "suggested_size_adjustment": <0.5-1.0 (1.0 = full size, 0.5 = half size)>
}

If concern_level is "low", always approve=true.
Only set approve=false for HIGH concern with clear justification."""


class RiskAgent:
    """
    Consumes raw signals from the bus, applies risk rules, publishes approved signals.
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        portfolio: PortfolioState,
        signal_bus: SignalBus,
        audit: AuditTrail,
        use_llm_review: bool = True,
    ):
        self.risk_mgr = risk_manager
        self.portfolio = portfolio
        self.bus = signal_bus
        self.audit = audit
        self.use_llm_review = use_llm_review
        self.allocator = PortfolioAllocator(portfolio)
        self.strategy_weights: dict[str, float] = {}   # updated by orchestrator each morning
        settings = get_settings()
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def run(self) -> None:
        """
        Continuous loop: drain raw signals in batches → allocator → evaluate → publish.
        Batching allows the allocator to rank all pending signals together before
        committing capital to any of them.
        """
        logger.info("RiskAgent started, listening for signals...")
        while True:
            try:
                # Block until at least one signal arrives
                first = await asyncio.wait_for(self.bus.consume_signal(), timeout=1.0)
                self.bus.signal_done()
                batch = [first]

                # Drain any additional signals already queued (non-blocking)
                while not self.bus._raw_queue.empty():
                    sig = self.bus._raw_queue.get_nowait()
                    self.bus.signal_done()
                    batch.append(sig)

                # Portfolio allocator: rank and filter by capital availability
                shortlist = self.allocator.allocate(batch, self.strategy_weights)

                for signal in shortlist:
                    try:
                        approved = await self._evaluate_signal(signal)
                        if approved:
                            await self.bus.publish_approved(approved)
                    except Exception as exc:
                        logger.error(f"RiskAgent signal eval error [{signal.symbol}]: {exc}")

            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.error(f"RiskAgent error: {exc}")
                await asyncio.sleep(0.5)

    async def _evaluate_signal(self, signal: Signal) -> Optional[ApprovedSignal]:
        """Full evaluation pipeline: hard rules → optional LLM review."""
        # Hard rules (risk_manager)
        approved = await self.risk_mgr.evaluate(signal)
        if not approved:
            return None  # Already logged by risk_manager

        # Optional LLM review for borderline signals
        if self.use_llm_review and approved.signal.confidence < 0.7:
            adjusted = await self._llm_review(approved)
            return adjusted

        return approved

    async def _llm_review(self, approved: ApprovedSignal) -> Optional[ApprovedSignal]:
        """Use Claude to review signals with moderate confidence."""
        signal = approved.signal
        portfolio_summary = self.portfolio.summary()

        prompt = RISK_REVIEW_PROMPT.format(
            signal_data=json.dumps(signal.to_dict(), indent=2),
            portfolio_summary=json.dumps(portfolio_summary, indent=2),
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()

            review = json.loads(raw)
            approve = review.get("approve", True)
            concern = review.get("concern_level", "low")
            flags = review.get("flags", [])
            reasoning = review.get("reasoning", "")
            size_adj = float(review.get("suggested_size_adjustment", 1.0))

            logger.info(
                f"LLM risk review [{signal.symbol}]: approve={approve} "
                f"concern={concern} flags={flags}"
            )
            self.audit.log_agent_decision(
                "RiskAgent",
                reasoning,
                {"symbol": signal.symbol, "concern": concern, "flags": flags, "approve": approve},
            )

            if not approve:
                self.audit.log_risk_decision(signal, False, f"LLM rejected: {reasoning}")
                return None

            # Adjust size if recommended
            if size_adj < 1.0:
                approved.approved_qty = max(1, int(approved.approved_qty * size_adj))
                approved.approved_capital = approved.approved_qty * signal.entry_price
                approved.risk_note = f"LLM size-adjusted to {size_adj*100:.0f}%: {reasoning}"

            return approved

        except Exception as exc:
            logger.warning(f"LLM risk review failed (approving anyway): {exc}")
            return approved  # Fail open — hard rules already passed
