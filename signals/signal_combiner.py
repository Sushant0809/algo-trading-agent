"""
Signal Combiner: cross-validates a sentiment-driven signal against technical indicators.

Position in pipeline:
    SentimentAgent → SentimentDrivenStrategy → SignalCombiner → SignalBus → RiskAgent

Why this exists:
    Sentiment and technical signals are NOT independent — they share price as input.
    This combiner checks that the technical picture *agrees* with the sentiment direction
    before the signal reaches the risk agent.  A high sentiment score alone is not enough
    if price is already overbought, MACD is diverging, or volume is absent.

Scoring:
    Each technical check returns True/False.
    tech_score = passed_checks / total_checks  (0.0–1.0)
    combined_confidence = 0.6 × sentiment_confidence + 0.4 × tech_score

Decision:
    tech_score ≥ 0.6  → CONFIRM  (≥3/5 checks pass) — signal passes through
    tech_score ≥ 0.4  → DOWNGRADE signal strength to WEAK, lower confidence
    tech_score < 0.4  → REJECT   (majority of technicals disagree)

Checks for LONG signals (BUY):
    1. RSI in [30, 70]    — not overbought, not deeply oversold bounce
    2. MACD histogram > 0 or turning up (hist > prev hist)
    3. Price ≥ EMA(20)    — short-term trend positive
    4. Volume ratio > 1.0 — above-average volume confirms move
    5. ADX > 20           — some trend strength (skip if data missing)

Checks for SHORT signals (SELL):
    1. RSI in [30, 70]    — not oversold (avoid short into oversold)
    2. MACD histogram < 0 or turning down (hist < prev hist)
    3. Price ≤ EMA(20)    — short-term trend negative
    4. Volume ratio > 1.0 — above-average volume confirms move
    5. ADX > 20           — some trend strength
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from signals.signal_model import Signal, SignalAction, SignalStrength

logger = logging.getLogger(__name__)

# Tunable thresholds
RSI_OB = 70.0        # overbought — avoid long entry
RSI_OS = 30.0        # oversold   — avoid short entry
MIN_VOLUME_RATIO = 1.0
MIN_ADX = 20.0
TECH_CONFIRM_THRESHOLD  = 0.6   # ≥60% checks pass → confirm
TECH_DOWNGRADE_THRESHOLD = 0.4  # 40–60% → downgrade to WEAK
# below 40% → reject


@dataclass
class CombineResult:
    decision: str          # "confirm" | "downgrade" | "reject"
    tech_score: float      # 0.0–1.0
    combined_confidence: float
    checks: dict[str, bool]
    signal: Optional[Signal]  # updated signal (None if rejected)
    reason: str


def combine(signal: Signal, df: pd.DataFrame) -> CombineResult:
    """
    Cross-validate *signal* against technical indicators in *df*.

    df must have been processed with compute_all_indicators() already.
    Returns a CombineResult; caller should use result.signal (None = rejected).
    """
    is_long  = signal.action == SignalAction.BUY
    is_short = signal.action == SignalAction.SELL

    # Read indicator values safely
    def last(col: str) -> float:
        if col not in df.columns:
            return float("nan")
        v = df[col].iloc[-1]
        return float(v) if pd.notna(v) else float("nan")

    def prev(col: str) -> float:
        if col not in df.columns or len(df) < 2:
            return float("nan")
        v = df[col].iloc[-2]
        return float(v) if pd.notna(v) else float("nan")

    close        = last("close")
    rsi          = last("rsi")
    macd_hist    = last("macd_hist")
    macd_hist_p  = prev("macd_hist")
    ema_20       = last("ema_20")
    volume_ratio = last("volume_ratio")
    adx          = last("adx")

    # ----------------------------------------------------------------
    # Run checks
    # ----------------------------------------------------------------
    checks: dict[str, bool] = {}

    if is_long:
        checks["rsi_not_overbought"] = (
            not math.isnan(rsi) and RSI_OS <= rsi <= RSI_OB
        )
        checks["macd_positive_or_turning"] = (
            not math.isnan(macd_hist) and (
                macd_hist > 0 or
                (not math.isnan(macd_hist_p) and macd_hist > macd_hist_p)
            )
        )
        checks["price_above_ema20"] = (
            not math.isnan(ema_20) and not math.isnan(close) and close >= ema_20
        )
        checks["volume_above_avg"] = (
            not math.isnan(volume_ratio) and volume_ratio >= MIN_VOLUME_RATIO
        )
        checks["adx_trend"] = (
            math.isnan(adx) or adx >= MIN_ADX   # pass if data missing
        )

    elif is_short:
        checks["rsi_not_oversold"] = (
            not math.isnan(rsi) and RSI_OS <= rsi <= RSI_OB
        )
        checks["macd_negative_or_turning"] = (
            not math.isnan(macd_hist) and (
                macd_hist < 0 or
                (not math.isnan(macd_hist_p) and macd_hist < macd_hist_p)
            )
        )
        checks["price_below_ema20"] = (
            not math.isnan(ema_20) and not math.isnan(close) and close <= ema_20
        )
        checks["volume_above_avg"] = (
            not math.isnan(volume_ratio) and volume_ratio >= MIN_VOLUME_RATIO
        )
        checks["adx_trend"] = (
            math.isnan(adx) or adx >= MIN_ADX
        )

    else:
        # HOLD — pass through unchanged
        return CombineResult(
            decision="confirm",
            tech_score=1.0,
            combined_confidence=signal.confidence,
            checks={},
            signal=signal,
            reason="HOLD signal — no technical validation needed",
        )

    # ----------------------------------------------------------------
    # Score
    # ----------------------------------------------------------------
    passed = sum(1 for v in checks.values() if v)
    tech_score = passed / len(checks) if checks else 0.0

    sent_conf = signal.confidence or 0.5
    combined_conf = round(0.6 * sent_conf + 0.4 * tech_score, 3)

    failed = [k for k, v in checks.items() if not v]
    passed_names = [k for k, v in checks.items() if v]

    # ----------------------------------------------------------------
    # Decision
    # ----------------------------------------------------------------
    direction = "LONG" if is_long else "SHORT"

    if tech_score >= TECH_CONFIRM_THRESHOLD:
        decision = "confirm"
        updated = _update_signal(signal, combined_conf, signal.strength)
        reason = (
            f"{direction} confirmed: tech_score={tech_score:.2f} "
            f"({passed}/{len(checks)} checks: {passed_names})"
        )

    elif tech_score >= TECH_DOWNGRADE_THRESHOLD:
        decision = "downgrade"
        updated = _update_signal(signal, combined_conf, SignalStrength.WEAK)
        reason = (
            f"{direction} downgraded to WEAK: tech_score={tech_score:.2f} "
            f"({passed}/{len(checks)} checks). Failed: {failed}"
        )

    else:
        decision = "reject"
        updated = None
        reason = (
            f"{direction} REJECTED: tech_score={tech_score:.2f} "
            f"({passed}/{len(checks)} checks). Failed: {failed}"
        )

    logger.info(
        f"SignalCombiner [{signal.symbol}] {decision.upper()} | "
        f"sent_conf={sent_conf:.2f} tech={tech_score:.2f} "
        f"combined={combined_conf:.2f} | {reason}"
    )

    return CombineResult(
        decision=decision,
        tech_score=tech_score,
        combined_confidence=combined_conf,
        checks=checks,
        signal=updated,
        reason=reason,
    )


def _update_signal(
    signal: Signal,
    new_confidence: float,
    new_strength: SignalStrength,
) -> Signal:
    """Return a copy of signal with updated confidence, strength, and appended reasoning."""
    from dataclasses import replace
    updated_reasoning = (
        f"{signal.reasoning} | "
        f"[Combiner] conf={new_confidence:.2f} strength={new_strength.value}"
    )
    return replace(
        signal,
        confidence=new_confidence,
        strength=new_strength,
        reasoning=updated_reasoning,
    )
