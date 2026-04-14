"""
LLM Portfolio Manager base class supporting multiple backends (Claude, Groq, etc).
"""
import json
import logging
import math
import os
import textwrap
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ─── Allocation decision model ────────────────────────────────────────────────

@dataclass
class SymbolAllocation:
    symbol: str
    action: str          # "buy" | "sell" | "hold"
    quantity: float      # fraction: buy=% of cash, sell=% of holdings held
    reasoning: str = ""

    def __post_init__(self):
        self.action = self.action.lower()
        if self.action not in ("buy", "sell", "hold"):
            self.action = "hold"
        self.quantity = max(0.0, min(1.0, float(self.quantity)))


@dataclass
class AllocationDecision:
    allocations: list[SymbolAllocation]
    model_reasoning: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def buys(self) -> list[SymbolAllocation]:
        return [a for a in self.allocations if a.action == "buy" and a.quantity > 0]

    def sells(self) -> list[SymbolAllocation]:
        return [a for a in self.allocations if a.action == "sell" and a.quantity > 0]

    def summary(self) -> str:
        buys  = "+".join(f"buy({a.symbol},{a.quantity:.2f})" for a in self.buys())
        sells = "+".join(f"sell({a.symbol},{a.quantity:.2f})" for a in self.sells())
        parts = [p for p in [buys, sells] if p]
        return " | ".join(parts) or "hold(all)"


# ─── Base LLM Manager ────────────────────────────────────────────────────────

class BaseLLMPortfolioManager(ABC):
    """Abstract base for LLM-based portfolio managers."""

    SYSTEM_PROMPT = textwrap.dedent("""
        You are an expert Indian equity portfolio manager trading NIFTY50 stocks.
        You manage a real money portfolio and are called INFREQUENTLY — every 3-5 days
        in bull markets, only at regime transitions otherwise.

        YOUR ROLE: Stock selection and portfolio construction, NOT daily trading.
        Default action for every held position is HOLD. Only deviate with a compelling reason.

        At each step you receive:
          - Market data with full technical indicators for every tracked symbol
          - Your current holdings with unrealised P&L per position
          - Portfolio metrics: cash, total capital, daily P&L, drawdown
          - NIFTY50 trend context and regime classification
          - Recent trade history (so you have memory of past decisions)

        YOU MUST respond with a JSON array — one entry per available symbol:
        [
          {"symbol": "RELIANCE", "action_type": "buy",  "quantity": 0.30},
          {"symbol": "TCS",      "action_type": "hold", "quantity": 0.00},
          {"symbol": "HDFCBANK", "action_type": "sell", "quantity": 0.50}
        ]

        FIELD RULES (STRICT):
          - "symbol": exact symbol as given
          - "action_type": exactly "buy", "sell", or "hold"
          - "quantity": fraction 0.0–1.0
              buy:  fraction of available_cash to spend on this stock
              sell: fraction of current holdings to sell
              hold: do nothing (quantity = 0)

        STRATEGY PHILOSOPHY:
          - In bull markets: select strong momentum stocks and HOLD them. Do NOT rotate.
          - A stock dipping from RSI 65 to 55 is normal fluctuation, NOT a sell signal.
          - Only sell held positions for compelling reasons:
              * P&L below -7% (stop loss)
              * RSI > 80 sustained (extreme overbought)
              * Fundamental trend reversal (MACD hist deeply negative AND price < EMA50)
          - Aim for 2-4 new position entries per call, not wholesale portfolio changes.
          - Consistency beats daily optimization. Let winners compound.

        STOCK SELECTION CRITERIA (for new buys):
          - RSI 40-65 + EMA20 > EMA50 > EMA200 = strong momentum candidate
          - MACD hist positive and rising = uptrend confirmation
          - ADX > 25 = strong trend, trade with trend
          - volume_ratio > 1.2 = above-average participation
          - Price > BB mid = not overextended but in uptrend
          - RSI < 30 = deep oversold for contrarian entry (smaller position)

        SENTIMENT AS CONFIRMATION (when available):
          - Use sentiment ONLY as a tiebreaker between equally-strong technical setups
          - Technical signal BULLISH + sentiment POSITIVE → boost position size 10-15%
          - Technical signal BULLISH + sentiment NEGATIVE → reduce allocation 20% or skip
          - Sentiment alone → NEVER trade (too noisy without confirmation)
          - Example: If RSI 45 + MACD turning up + EMA stack, check sentiment:
            * Positive sentiment → increase position to 0.30
            * Negative sentiment → reduce to 0.15 or skip

        ALLOCATION RULES:
          - NORMAL regimes: Keep at least 10% cash reserve at all times
          - STRONG BULL: Reduce cash reserve to 5% minimum (deploy 95% of capital)
          - No single stock > 8% of total portfolio value (BULL), > 15% in STRONG_BULL
          - Diversify across 5-10 stocks
          - In bull regime: stay 80%+ deployed in momentum stocks
          - In STRONG_BULL: deploy 95%, use aggressive position sizes (0.25-0.35 each)
          - Spread buys across multiple candidates (quantity 0.15-0.30 each, or 0.25-0.35 in STRONG_BULL)

        REGIME-SPECIFIC BEHAVIOR:
          - STRONG BULL: Bias toward holding. Sell only on hard stop (-7%) or very strong profit (+20%+).
            → Your job is to stay invested and compound. +15% can become +30% if held.
            → Deploy aggressively (use 0.25-0.35 position sizes). Keep 5% cash buffer.
            → Let winners run: block exit at +15%, but allow at +20%+ to capture extended rallies.
          - BULL: Hold most positions. Trim only extreme overbought (RSI>80). Buy pullbacks.
          - NEUTRAL: Be selective. Hold winners, cut persistent laggards slowly.
          - RECOVERY (called after BEAR/CRASH bounce): Portfolio is mostly cash. Your job is
            to identify the strongest momentum leaders and start re-entering with 3-5 positions.
            Prefer stocks that have bounced the most from recent lows with rising volume.
            Use position sizes of 0.25-0.40 per stock. Be decisive — don't hold all cash.
          - Note: During BEAR/CRASH, the system auto-liquidates and shorts. You are not called
            until a recovery bounce is detected (NIFTY 10-day return > +8%).

        TRADE HISTORY RULES:
          - Review the RECENT TRADE HISTORY section before making decisions.
          - Do NOT re-buy a stock you recently sold at a loss.
          - A stopped-out stock needs a clear regime change before re-entry.
          - Avoid churning — if you sold something last week, leave it alone.

        Indian market rules:
          - Short selling is only for INTRADAY (MIS). Do not short for overnight holds.

        Respond ONLY with the JSON array. No markdown, no explanation text.
    """).strip()

    def __init__(
        self,
        cash_reserve_pct: float = 0.10,
    ):
        self.cash_reserve_pct = cash_reserve_pct

    @abstractmethod
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
        """Make allocation decisions. Implemented by subclasses."""
        pass

    def _build_prompt(
        self,
        symbol_data: dict[str, pd.DataFrame],
        cash: float,
        total_value: float,
        holdings: dict[str, float],
        avg_costs: dict[str, float],
        daily_pnl: float,
        drawdown_pct: float,
        nifty_df: pd.DataFrame | None,
        entry_dates: dict[str, object] | None = None,
        today: object | None = None,
        trade_journal: list[dict] | None = None,
    ) -> str:
        """Build the prompt for LLM. Shared across all backends."""
        lines = []

        # ── Portfolio header ──────────────────────────────────────────────
        lines.append(f"PORTFOLIO STATUS")
        lines.append(f"  Cash:         ₹{cash:,.0f}  ({cash/total_value*100:.1f}% of portfolio)")
        lines.append(f"  Total value:  ₹{total_value:,.0f}")
        lines.append(f"  Daily P&L:    ₹{daily_pnl:+,.0f}  ({daily_pnl/total_value*100:+.2f}%)")
        lines.append(f"  Drawdown:     {drawdown_pct*100:.1f}%")
        lines.append(f"  Cash reserve: Keep at least ₹{total_value*self.cash_reserve_pct:,.0f} as buffer")
        lines.append("")

        # ── NIFTY50 regime context (7-factor score) ──────────────────────
        if nifty_df is not None and not nifty_df.empty:
            n_close  = _last(nifty_df, "close")
            n_ema20  = _last(nifty_df, "ema_20")
            n_ema50  = _last(nifty_df, "ema_50")
            n_ema200 = _last(nifty_df, "ema_200")
            n_rsi    = _last(nifty_df, "rsi")
            n_macd   = _last(nifty_df, "macd_hist")
            # Count bullish factors
            score = 0
            def _ok(a, b): return not math.isnan(a) and not math.isnan(b)
            if _ok(n_close, n_ema200) and n_close > n_ema200: score += 1
            if _ok(n_close, n_ema50)  and n_close > n_ema50:  score += 1
            if _ok(n_close, n_ema20)  and n_close > n_ema20:  score += 1
            if _ok(n_ema20, n_ema50)  and n_ema20 > n_ema50:  score += 1
            if _ok(n_ema50, n_ema200) and n_ema50 > n_ema200: score += 1
            if not math.isnan(n_rsi)  and n_rsi  > 50: score += 1
            if not math.isnan(n_macd) and n_macd > 0:  score += 1
            regime = (
                "STRONG BULL" if score >= 5 else
                "BULL"        if score >= 3 else
                "NEUTRAL"     if score >= 2 else
                "BEAR"
            )
            lines.append(f"MARKET REGIME: {regime} ({score}/7 technical factors)")
            lines.append(f"  NIFTY50: ₹{n_close:,.0f}  RSI={n_rsi:.1f}  EMA20={n_ema20:,.0f}  EMA50={n_ema50:,.0f}  EMA200={n_ema200:,.0f}")
            lines.append("")

        # ── Macroeconomic signals (FII/DII, VIX, RBI rate) ──────────────
        try:
            from data.macro_fetcher import fetch_fii_dii_flows, fetch_india_vix, get_rbi_rate
            macro_data = fetch_fii_dii_flows()
            india_vix = fetch_india_vix()
            rbi_rate = get_rbi_rate()

            fii_trend = macro_data.get("fii_trend", "neutral")
            fii_5d = macro_data.get("fii_net_5d", 0)
            lines.append("MACRO SIGNALS:")
            lines.append(f"  FII 5-day: ₹{fii_5d:,.0f} crores ({fii_trend})")
            lines.append(f"  India VIX: {india_vix:.1f} {'(calm)' if india_vix < 15 else '(elevated)' if india_vix > 25 else '(normal)'}")
            lines.append(f"  RBI Repo Rate: {rbi_rate:.2f}%")
            lines.append("")
        except Exception as exc:
            # Graceful degradation if macro fetch fails
            logger.debug(f"Macro signals unavailable: {exc}")

        # ── Current holdings ──────────────────────────────────────────────
        if holdings:
            lines.append("CURRENT HOLDINGS:")
            for sym, shares in holdings.items():
                if shares <= 0:
                    continue
                df = symbol_data.get(sym)
                curr_price = _last(df, "close") if df is not None else 0.0
                avg_cost   = avg_costs.get(sym, curr_price)
                mkt_value  = curr_price * shares
                pnl_pct    = (curr_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0.0
                # Days held — key for minimum-hold rule enforcement
                days_held = 0
                if entry_dates and today and sym in entry_dates:
                    days_held = (today - entry_dates[sym]).days
                hold_tag = f"  held {days_held}d" if days_held > 0 else ""
                lines.append(
                    f"  {sym:<16}: {shares:.0f} shares @ avg ₹{avg_cost:.2f}"
                    f"  curr ₹{curr_price:.2f}  val ₹{mkt_value:,.0f}  P&L {pnl_pct:+.1f}%{hold_tag}"
                )
            lines.append("")

        # ── Recent trade history (LLM memory) ────────────────────────────
        if trade_journal:
            lines.append("RECENT TRADE HISTORY (last 10 trades):")
            for t in trade_journal[-10:]:
                pnl_str = f"P&L {t['pnl_pct']:+.1f}%" if t.get('pnl_pct') else ""
                lines.append(
                    f"  {t['date']}  {t['action']:<4} {t['symbol']:<12} "
                    f"{t.get('qty', 0):.0f}@{t.get('price', 0):.2f}  "
                    f"{t.get('exit_reason', ''):<16} {pnl_str}"
                )
            lines.append("")
            lines.append("IMPORTANT: Do NOT re-buy stocks you recently sold. "
                         "Let stopped-out stocks recover before re-entry.")
            lines.append("")

        # ── Symbol-by-symbol indicator table ─────────────────────────────
        lines.append("MARKET DATA (all tracked symbols):")
        lines.append(
            f"  {'Symbol':<16} {'Price':>8} {'RSI':>6} {'MACD_h':>8} "
            f"{'EMA20':>8} {'EMA200':>8} {'ADX':>6} {'VolRatio':>9} {'BB_pos':>7}"
        )
        lines.append("  " + "-" * 82)

        for sym, df in symbol_data.items():
            if df.empty:
                continue
            close   = _last(df, "close")
            rsi     = _last(df, "rsi")
            macd_h  = _last(df, "macd_hist")
            ema20   = _last(df, "ema_20")
            ema200  = _last(df, "ema_200")
            adx     = _last(df, "adx")
            vol_r   = _last(df, "volume_ratio")
            bb_low  = _last(df, "bb_lower")
            bb_up   = _last(df, "bb_upper")

            # BB position: 0=at lower, 1=at upper
            bb_pos = (
                (close - bb_low) / (bb_up - bb_low)
                if not math.isnan(bb_low) and not math.isnan(bb_up) and (bb_up - bb_low) > 0
                else float("nan")
            )
            held_marker = "*" if holdings.get(sym, 0) > 0 else " "
            lines.append(
                f"  {held_marker}{sym:<15} {close:>8.2f} {rsi:>6.1f} {macd_h:>8.4f} "
                f"{ema20:>8.2f} {ema200:>8.2f} {adx:>6.1f} {vol_r:>9.2f} {bb_pos:>7.2f}"
            )

        lines.append("")
        lines.append("(* = currently held position)")
        lines.append("")
        lines.append(f"Available symbols: {', '.join(symbol_data.keys())}")
        lines.append("")
        lines.append("Respond with JSON array — one entry per symbol above.")

        return "\n".join(lines)

    def _parse_response(self, text: str, valid_symbols: list[str]) -> list[SymbolAllocation]:
        """Parse LLM JSON response into SymbolAllocation list."""
        text = text.strip()

        # Strip markdown code fences
        if text.startswith("```"):
            lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        def _hold_all() -> list[SymbolAllocation]:
            return [SymbolAllocation(sym, "hold", 0.0) for sym in valid_symbols]

        try:
            data = json.loads(text)
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                logger.warning("LLMPortfolioManager: response is not a JSON array — holding all")
                return _hold_all()

            result: list[SymbolAllocation] = []
            seen: set[str] = set()

            for item in data:
                sym    = item.get("symbol", "")
                action = item.get("action_type") or item.get("action", "hold")
                qty    = float(item.get("quantity", 0.0))

                if sym not in valid_symbols:
                    continue
                seen.add(sym)
                result.append(SymbolAllocation(sym, action, qty))

            # Fill missing symbols with hold
            for sym in valid_symbols:
                if sym not in seen:
                    result.append(SymbolAllocation(sym, "hold", 0.0))

            return result if result else _hold_all()

        except Exception as exc:
            logger.warning(f"LLMPortfolioManager: JSON parse failed ({exc}) — holding all")
            return _hold_all()


# ─── Utility ──────────────────────────────────────────────────────────────────

def _last(df: pd.DataFrame, col: str) -> float:
    if df is None or df.empty or col not in df.columns:
        return float("nan")
    v = df[col].iloc[-1]
    return float(v) if pd.notna(v) else float("nan")


# ─── Factory ──────────────────────────────────────────────────────────────────

def create_llm_manager(provider: str = "claude", **kwargs) -> BaseLLMPortfolioManager:
    """
    Factory function to create LLM manager.

    Args:
        provider: "claude" (default) or "groq"
                  Can also be set via LLM_PROVIDER env variable
        **kwargs: passed to the manager constructor

    Returns:
        Initialized LLM manager instance

    Examples:
        create_llm_manager()                      # Uses Claude (default)
        create_llm_manager(provider="groq")       # Uses Groq (cheaper)
    """
    provider = (os.getenv("LLM_PROVIDER") or provider).lower()

    if provider == "groq":
        from agents.groq_portfolio_manager import GroqPortfolioManager
        return GroqPortfolioManager(**kwargs)
    elif provider == "claude":
        from agents.llm_portfolio_manager import LLMPortfolioManager
        return LLMPortfolioManager(**kwargs)
    else:
        raise ValueError(
            f"Unknown LLM provider: {provider}. "
            f"Use 'claude' (default) or 'groq'."
        )
