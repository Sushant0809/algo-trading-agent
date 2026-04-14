"""
LLM Strategy: Claude Sonnet as a swing trade decision maker.

Role in pipeline:
    Sits alongside technical strategies in the registry.
    Called from MarketAnalyst (or Orchestrator's swing cycle).
    Returns a Signal just like any other strategy.

Design principles:
    1. SUPPLEMENT, not replace, technical strategies.
       LLMStrategy only runs in SWING mode. Intraday latency is too high.
    2. Structured output enforced — prompt demands JSON; fallback = no signal.
    3. Only trades when Claude confidence ≥ threshold (default 0.65).
    4. Uses the same indicators already computed by compute_all_indicators().
    5. Supports BUY (long CNC), SELL (short MIS intraday forced), or HOLD.
    6. Caches one call per symbol per day — no repeated API calls in scan loops.

Prompt design:
    - Provides last 5 bars of OHLCV + key indicators as a compact table
    - Asks for: action, confidence, stop_loss, target, reasoning
    - Temperature=0.1 for deterministic, conservative output
"""
from __future__ import annotations

import json
import logging
import math
import textwrap
from datetime import date, datetime, timezone
from typing import Optional

import anthropic
import pandas as pd

from config.settings import get_settings
from signals.signal_model import Product, Signal, SignalAction, SignalStrength, TradingMode
from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MIN_CONFIDENCE    = 0.65   # reject LLM signals below this
MAX_TOKENS        = 400
TEMPERATURE       = 0.1
MIN_BARS_REQUIRED = 60

LLM_SYSTEM_PROMPT = textwrap.dedent("""
    You are a quantitative swing trader specialising in Indian equities (NSE/BSE).
    You will receive OHLCV data and technical indicators for a single stock.
    Your job: decide whether to BUY (enter long CNC swing), SELL (enter short MIS intraday),
    or HOLD (no action) based on the data provided.

    Rules:
    - SWING trades only (1–10 day hold); NOT for intraday scalping.
    - SELL means initiating a short. Only short if the trend is clearly bearish.
      Remember: equity short selling in India is intraday-only (MIS product).
    - Base entry on CURRENT price (last close). Stop and target are price levels.
    - Risk:reward must be at least 1.5:1 (target distance ≥ 1.5 × stop distance).
    - Do not trade if the chart is choppy or unclear — output HOLD.

    Respond ONLY with valid JSON (no markdown, no extra text):
    {
      "action":     "BUY" | "SELL" | "HOLD",
      "confidence": <float 0.0–1.0>,
      "stop_loss":  <price float — required if action != HOLD>,
      "target":     <price float — required if action != HOLD>,
      "reasoning":  "<max 2 sentences>"
    }
""").strip()


def _build_prompt(symbol: str, df: pd.DataFrame) -> str:
    """Compact prompt: last 5 bars table + latest indicator snapshot."""
    # Last 5 daily bars
    tail = df.tail(5)[["open", "high", "low", "close", "volume"]].copy()
    if isinstance(tail.index[0], (datetime, date, pd.Timestamp)):
        tail.index = tail.index.strftime("%Y-%m-%d")

    bar_lines = []
    for dt, row in tail.iterrows():
        bar_lines.append(
            f"  {dt}  O={row['open']:.1f}  H={row['high']:.1f}  "
            f"L={row['low']:.1f}  C={row['close']:.1f}  V={int(row['volume']):,}"
        )

    def g(col: str) -> str:
        """Get last value of a column as a formatted string."""
        if col not in df.columns:
            return "N/A"
        v = df[col].iloc[-1]
        if pd.isna(v):
            return "N/A"
        return f"{float(v):.2f}"

    indicators = textwrap.dedent(f"""
        EMA(9)={g('ema_9')}  EMA(20)={g('ema_20')}  EMA(50)={g('ema_50')}  EMA(200)={g('ema_200')}
        RSI(14)={g('rsi')}
        MACD={g('macd')}  Signal={g('macd_signal')}  Hist={g('macd_hist')}
        BB_Upper={g('bb_upper')}  BB_Mid={g('bb_mid')}  BB_Lower={g('bb_lower')}
        ATR(14)={g('atr')}  ADX(14)={g('adx')}
        Vol_Ratio={g('volume_ratio')}  (current vol / 20-day avg)
        20-day High={g('roll_high')}  20-day Low={g('roll_low')}
    """).strip()

    return textwrap.dedent(f"""
        Symbol: {symbol} (NSE/BSE)

        === Last 5 daily bars ===
        {chr(10).join(bar_lines)}

        === Technical indicators (latest bar) ===
        {indicators}

        Should I BUY, SELL, or HOLD {symbol}? Respond with JSON only.
    """).strip()


def _parse_response(text: str) -> Optional[dict]:
    """Extract and validate the JSON from Claude's response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object within the text
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        try:
            data = json.loads(text[start:end])
        except json.JSONDecodeError:
            return None

    action = str(data.get("action", "HOLD")).upper()
    if action not in ("BUY", "SELL", "HOLD"):
        return None

    confidence = float(data.get("confidence", 0.0))
    result = {
        "action":     action,
        "confidence": max(0.0, min(1.0, confidence)),
        "stop_loss":  data.get("stop_loss"),
        "target":     data.get("target"),
        "reasoning":  str(data.get("reasoning", ""))[:300],
    }
    return result


class LLMStrategy(BaseStrategy):
    """
    Swing-only strategy that uses Claude Sonnet to decide BUY / SELL / HOLD.
    Not a BaseStrategy subclass in the strict sense — it needs an API client,
    so it takes one in __init__. The registry handles this via a factory.
    """
    name = "llm_strategy"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        settings = get_settings()
        self.client       = anthropic.Anthropic(api_key=settings.anthropic_api_key)       # sync (unused now)
        self.async_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)  # async
        self.model        = settings.anthropic_model
        self.min_confidence = self.params.get("min_confidence", MIN_CONFIDENCE)
        self.atr_mult = self.params.get("atr_stop_multiplier", 1.5)
        # Per-day cache: {symbol: (date, Signal|None)}
        self._cache: dict[str, tuple[date, Optional[Signal]]] = {}

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    def generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
    ) -> Optional[Signal]:
        """
        Sync stub — returns None so MarketAnalyst skips it.
        Callers should use async_generate_signal() instead.
        """
        return None  # Always use async path via MarketAnalyst special-case

    async def async_generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
    ) -> Optional[Signal]:
        """Async entry point used by MarketAnalyst in swing cycle."""
        if mode == TradingMode.INTRADAY:
            return None

        if not self.has_min_bars(df, MIN_BARS_REQUIRED):
            return None

        today = datetime.now(timezone.utc).date()
        if symbol in self._cache:
            cached_date, cached_signal = self._cache[symbol]
            if cached_date == today:
                return cached_signal

        signal = await self._call_llm_async(symbol, df, mode)
        self._cache[symbol] = (today, signal)
        return signal

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _call_llm_async(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
    ) -> Optional[Signal]:
        prompt = _build_prompt(symbol, df)

        try:
            response = await self.async_client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=LLM_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = (response.content[0].text or "").strip()
        except Exception as exc:
            logger.error(f"LLMStrategy API call failed [{symbol}]: {exc}")
            return None

        parsed = _parse_response(raw)
        if parsed is None:
            logger.warning(f"LLMStrategy [{symbol}]: unparseable response: {raw[:120]}")
            return None

        if parsed["action"] == "HOLD":
            logger.debug(f"LLMStrategy [{symbol}]: HOLD")
            return None

        if parsed["confidence"] < self.min_confidence:
            logger.debug(
                f"LLMStrategy [{symbol}]: {parsed['action']} below confidence "
                f"threshold ({parsed['confidence']:.2f} < {self.min_confidence})"
            )
            return None

        return self._build_signal(symbol, df, mode, parsed)

    def _build_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        mode: TradingMode,
        parsed: dict,
    ) -> Optional[Signal]:
        close = float(df["close"].iloc[-1])
        atr   = self._last(df, "atr")
        action_str = parsed["action"]

        # Validate and apply stop/target from LLM
        stop   = parsed.get("stop_loss")
        target = parsed.get("target")

        if stop is None or target is None or math.isnan(atr):
            # Fall back to ATR-based levels if LLM omitted them
            if action_str == "BUY":
                stop   = round(close - self.atr_mult * atr, 2)
                target = round(close + 2 * (close - stop), 2)
            else:
                stop   = round(close + self.atr_mult * atr, 2)
                target = round(close - 2 * (stop - close), 2)
        else:
            stop   = round(float(stop), 2)
            target = round(float(target), 2)

        # Enforce minimum R:R of 1.5:1
        if action_str == "BUY":
            risk   = close - stop
            reward = target - close
        else:
            risk   = stop - close
            reward = close - target

        if risk <= 0 or reward / risk < 1.5:
            logger.debug(
                f"LLMStrategy [{symbol}]: {action_str} rejected — R:R "
                f"{reward:.1f}/{risk:.1f} < 1.5"
            )
            return None

        confidence = parsed["confidence"]
        strength = (
            SignalStrength.STRONG if confidence >= 0.80
            else SignalStrength.MODERATE if confidence >= 0.65
            else SignalStrength.WEAK
        )

        if action_str == "BUY":
            action  = SignalAction.BUY
            product = Product.CNC   # swing long, overnight hold
        else:
            action  = SignalAction.SELL
            product = Product.MIS   # equity short = intraday only
            mode    = TradingMode.INTRADAY

        return Signal(
            symbol=symbol,
            action=action,
            strategy=self.name,
            mode=mode,
            product=product,
            entry_price=close,
            stop_loss=stop,
            target=target,
            position_size_pct=self._size(confidence),
            confidence=confidence,
            strength=strength,
            reasoning=(
                f"[LLM:{self.model}] {action_str} | conf={confidence:.2f} | "
                f"{parsed['reasoning']} | Stop={stop:.2f} Target={target:.2f}"
            ),
            indicators={
                "llm_confidence": confidence,
                "close": close,
                "atr": atr,
            },
        )

    def _size(self, confidence: float) -> float:
        """
        Position size scales with confidence:
          0.65 → 1.3% of capital, 0.80 → 1.6%, 1.0 → 2.0%
        """
        base = 0.02
        return round(base * confidence, 4)
