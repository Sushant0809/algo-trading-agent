"""
LLM Portfolio Manager: uses Claude to allocate capital across all tracked symbols daily.

This is the financial_market_env inference.py pattern adapted for production:
  - Every trading cycle, Claude receives the full portfolio state + all symbols' indicators
  - Claude returns a JSON array with buy/sell/hold + fraction for every symbol
  - Fraction semantics: buy=fraction of available cash, sell=fraction of holdings
  - This keeps capital deployed (no idle cash from missed signals)
  - Claude also makes exit decisions (when to reduce/exit positions)

Why this is better than the signal pipeline for allocation:
  - Signal pipeline: waits for specific conditions → fires 0-8% of bars → 60-80% cash idle
  - LLM allocator: reviews all stocks every day → always allocates available cash → deployed 80-90%

Prompt includes:
  - Full indicator suite: RSI, MACD, EMA stack, BB, ATR, ADX, volume ratio
  - Current holdings with unrealised P&L
  - Portfolio-level metrics (cash, drawdown, daily P&L)
  - NIFTY50 trend context (bull/bear/sideways)
  - Risk rules: 10% cash reserve, no individual position > 8% of capital
  - Indian market rules: short only MIS, no overnight shorts

This is called from:
  - TradingOrchestrator.run_intraday_cycle() every 5 minutes (intraday mode)
  - IntradayScheduler morning_setup for daily swing allocation
  - StrategyBacktester when allocation_mode="llm"
"""
from __future__ import annotations

import json
import logging
import math
import textwrap
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ─── Allocation decision model ────────────────────────────────────────────────

from dataclasses import dataclass, field


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


# ─── LLM Portfolio Manager ────────────────────────────────────────────────────

class LLMPortfolioManager:
    """
    Daily LLM-based portfolio allocator.

    Calls Claude once per trading cycle with all available symbol data.
    Returns allocation decisions (buy/sell/hold + fraction) for every symbol.
    """

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

        ALLOCATION RULES:
          - Keep at least 10% cash reserve at all times
          - No single stock > 8% of total portfolio value
          - Diversify across 5-10 stocks
          - In bull regime: stay 80%+ deployed in momentum stocks
          - Spread buys across multiple candidates (quantity 0.15-0.30 each)

        REGIME-SPECIFIC BEHAVIOR:
          - STRONG BULL: Hold everything. Only sell on hard stop (-7%). Buy dips (RSI<40).
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
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 2000,
        temperature: float = 0.2,
        cash_reserve_pct: float = 0.10,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.cash_reserve_pct = cash_reserve_pct
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def cleanup(self):
        """Properly close the async client."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def decide(
        self,
        symbol_data: dict[str, pd.DataFrame],
        portfolio_cash: float,
        portfolio_value: float,
        holdings: dict[str, float],         # symbol → shares held
        avg_costs: dict[str, float],         # symbol → avg cost per share
        daily_pnl: float = 0.0,
        drawdown_pct: float = 0.0,
        nifty_df: pd.DataFrame | None = None,
        entry_dates: dict[str, date] | None = None,   # symbol → entry date
        today: date | None = None,                     # current bar date
        trade_journal: list[dict] | None = None,       # last N trades for LLM memory
    ) -> AllocationDecision:
        """
        Ask Claude for allocation decisions across all symbols.

        Args:
            symbol_data:    {symbol: DataFrame with indicators computed}
            portfolio_cash: Current cash balance
            portfolio_value: Total portfolio value (cash + holdings)
            holdings:       Current shares held per symbol
            avg_costs:      Average entry cost per share per symbol
            daily_pnl:      Today's realised P&L so far
            drawdown_pct:   Current drawdown from peak
            nifty_df:       NIFTY50 DataFrame for regime context
            entry_dates:    Entry date per held symbol — used to show days held
            today:          Today's date — used to compute days held

        Returns:
            AllocationDecision with per-symbol buy/sell/hold + quantities.
        """
        prompt = self._build_prompt(
            symbol_data, portfolio_cash, portfolio_value,
            holdings, avg_costs, daily_pnl, drawdown_pct, nifty_df,
            entry_dates=entry_dates or {},
            today=today,
            trade_journal=trade_journal,
        )

        try:
            client = self._get_client()
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text.strip()
            logger.debug(f"LLMPortfolioManager raw response ({len(raw_text)} chars)")
        except Exception as exc:
            logger.error(f"LLMPortfolioManager API call failed: {exc}")
            raw_text = ""

        allocations = self._parse_response(raw_text, list(symbol_data.keys()))
        decision = AllocationDecision(
            allocations=allocations,
            model_reasoning=raw_text[:500],
        )
        logger.info(
            f"LLMPortfolioManager: {len(decision.buys())} buys, {len(decision.sells())} sells "
            f"| {decision.summary()}"
        )
        return decision

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
        entry_dates: dict[str, date] | None = None,
        today: date | None = None,
        trade_journal: list[dict] | None = None,
    ) -> str:
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
            lines.append(f"MARKET REGIME: {regime} ({score}/7 factors)")
            lines.append(f"  NIFTY50: ₹{n_close:,.0f}  RSI={n_rsi:.1f}  EMA20={n_ema20:,.0f}  EMA50={n_ema50:,.0f}  EMA200={n_ema200:,.0f}")
            lines.append("")

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
