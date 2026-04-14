"""
Unit tests for signals/signal_combiner.py

Coverage:
  - LONG signal: confirm (≥3/5 checks pass), downgrade (2/5), reject (<2/5)
  - SHORT signal: same paths
  - HOLD signal: always confirms unchanged
  - Combined confidence formula: 0.6×sent + 0.4×tech
  - Strength downgrade to WEAK on downgrade path
  - Signal updated with new confidence; original unchanged
  - Missing indicator columns handled gracefully (NaN = fail except ADX)
  - ADX NaN treated as pass (data missing → skip check)
"""
import math
from dataclasses import replace

import pandas as pd
import pytest

from signals.signal_combiner import combine, TECH_CONFIRM_THRESHOLD, TECH_DOWNGRADE_THRESHOLD
from signals.signal_model import Signal, SignalAction, SignalStrength, TradingMode, Product


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    action: SignalAction = SignalAction.BUY,
    confidence: float = 0.80,
    strength: SignalStrength = SignalStrength.STRONG,
    symbol: str = "RELIANCE",
) -> Signal:
    return Signal(
        symbol=symbol,
        action=action,
        strategy="sentiment_driven",
        mode=TradingMode.SWING if action == SignalAction.BUY else TradingMode.INTRADAY,
        product=Product.CNC if action == SignalAction.BUY else Product.MIS,
        entry_price=2500.0,
        stop_loss=2450.0 if action == SignalAction.BUY else 2550.0,
        target=2600.0 if action == SignalAction.BUY else 2400.0,
        confidence=confidence,
        strength=strength,
        reasoning="test signal",
    )


def _make_df(
    close: float = 2500.0,
    rsi: float = 50.0,
    macd_hist: float = 5.0,
    macd_hist_prev: float = 3.0,
    ema_20: float = 2480.0,
    volume_ratio: float = 1.2,
    adx: float = 25.0,
    rows: int = 5,
) -> pd.DataFrame:
    """Build a minimal DataFrame with required indicator columns."""
    data = {
        "close":        [close] * rows,
        "rsi":          [rsi] * rows,
        "macd_hist":    [macd_hist_prev] * (rows - 1) + [macd_hist],
        "ema_20":       [ema_20] * rows,
        "volume_ratio": [volume_ratio] * rows,
        "adx":          [adx] * rows,
    }
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# LONG (BUY) signal tests
# ---------------------------------------------------------------------------

class TestLongSignalCombiner:

    def test_all_checks_pass_confirms(self):
        sig = _make_signal(SignalAction.BUY, confidence=0.80)
        df = _make_df(
            close=2500, rsi=55, macd_hist=5.0, macd_hist_prev=3.0,
            ema_20=2480, volume_ratio=1.3, adx=28,
        )
        result = combine(sig, df)

        assert result.decision == "confirm"
        assert result.tech_score >= TECH_CONFIRM_THRESHOLD
        assert result.signal is not None
        assert result.signal.strength == SignalStrength.STRONG  # unchanged

    def test_three_of_five_pass_confirms(self):
        """Exactly 3/5 (0.60) = confirm threshold met."""
        sig = _make_signal(SignalAction.BUY, confidence=0.75)
        df = _make_df(
            close=2500, rsi=55,       # pass: not overbought
            macd_hist=5.0, macd_hist_prev=3.0,  # pass: positive
            ema_20=2480,              # pass: price ≥ ema20
            volume_ratio=0.7,         # FAIL: below 1.0
            adx=15,                   # FAIL: below 20
        )
        result = combine(sig, df)
        assert result.decision == "confirm"
        assert result.tech_score == pytest.approx(3 / 5)

    def test_two_of_five_downgrades_to_weak(self):
        """2/5 (0.40) = downgrade threshold, signal becomes WEAK."""
        sig = _make_signal(SignalAction.BUY, confidence=0.75, strength=SignalStrength.STRONG)
        df = _make_df(
            close=2500, rsi=55,       # pass
            macd_hist=5.0, macd_hist_prev=3.0,  # pass
            ema_20=2600,              # FAIL: price < ema20
            volume_ratio=0.7,         # FAIL
            adx=15,                   # FAIL
        )
        result = combine(sig, df)
        assert result.decision == "downgrade"
        assert result.signal is not None
        assert result.signal.strength == SignalStrength.WEAK
        assert result.tech_score == pytest.approx(2 / 5)

    def test_one_of_five_rejects(self):
        """1/5 (0.20) < reject threshold → signal dropped."""
        sig = _make_signal(SignalAction.BUY)
        df = _make_df(
            close=2500, rsi=55,       # pass
            macd_hist=-3.0, macd_hist_prev=-1.0,  # FAIL: negative and turning down
            ema_20=2600,              # FAIL
            volume_ratio=0.7,         # FAIL
            adx=15,                   # FAIL
        )
        result = combine(sig, df)
        assert result.decision == "reject"
        assert result.signal is None

    def test_rsi_overbought_fails_check(self):
        """RSI > 70 should fail the rsi_not_overbought check."""
        sig = _make_signal(SignalAction.BUY)
        df = _make_df(rsi=75, macd_hist=-2, macd_hist_prev=-1, ema_20=2600, volume_ratio=0.5, adx=10)
        result = combine(sig, df)
        assert not result.checks.get("rsi_not_overbought", True)

    def test_rsi_oversold_fails_long_check(self):
        """RSI < 30 should also fail (we want RSI in [30,70] for clean momentum)."""
        sig = _make_signal(SignalAction.BUY)
        df = _make_df(rsi=25, macd_hist=-2, macd_hist_prev=-1, ema_20=2600, volume_ratio=0.5, adx=10)
        result = combine(sig, df)
        assert not result.checks.get("rsi_not_overbought", True)

    def test_combined_confidence_formula(self):
        """combined_conf = 0.6×sent_conf + 0.4×tech_score."""
        sent_conf = 0.80
        sig = _make_signal(SignalAction.BUY, confidence=sent_conf)
        df = _make_df(
            close=2500, rsi=55, macd_hist=5, macd_hist_prev=3,
            ema_20=2480, volume_ratio=1.2, adx=25,
        )
        result = combine(sig, df)
        expected = round(0.6 * sent_conf + 0.4 * result.tech_score, 3)
        assert result.combined_confidence == pytest.approx(expected, abs=1e-3)

    def test_adx_nan_treated_as_pass(self):
        """If ADX column is missing, that check should pass (data unavailable = skip)."""
        sig = _make_signal(SignalAction.BUY)
        df = _make_df(close=2500, rsi=55, macd_hist=5, macd_hist_prev=3, ema_20=2480, volume_ratio=1.2)
        df = df.drop(columns=["adx"])  # Remove ADX
        result = combine(sig, df)
        # ADX check should pass (NaN = pass), so 5/5 if everything else passes
        assert result.checks.get("adx_trend") is True

    def test_macd_turning_up_passes_even_if_negative(self):
        """MACD hist < 0 but turning up (hist > prev) should pass."""
        sig = _make_signal(SignalAction.BUY)
        df = _make_df(
            close=2500, rsi=55,
            macd_hist=-1.0, macd_hist_prev=-3.0,  # negative but turning up
            ema_20=2480, volume_ratio=1.2, adx=25,
        )
        result = combine(sig, df)
        assert result.checks.get("macd_positive_or_turning") is True

    def test_original_signal_not_mutated(self):
        """combine() must not modify the input signal."""
        sig = _make_signal(SignalAction.BUY, confidence=0.80, strength=SignalStrength.STRONG)
        df = _make_df()
        combine(sig, df)
        assert sig.confidence == 0.80
        assert sig.strength == SignalStrength.STRONG


# ---------------------------------------------------------------------------
# SHORT (SELL) signal tests
# ---------------------------------------------------------------------------

class TestShortSignalCombiner:

    def _bearish_df(self) -> pd.DataFrame:
        """DataFrame with bearish conditions: price below EMA, neg MACD, high RSI."""
        return _make_df(
            close=2500, rsi=60,
            macd_hist=-4.0, macd_hist_prev=-2.0,  # negative and worsening
            ema_20=2530,  # price < ema20 (bearish)
            volume_ratio=1.3, adx=28,
        )

    def test_all_checks_pass_confirms_short(self):
        sig = _make_signal(SignalAction.SELL, confidence=0.75)
        result = combine(sig, self._bearish_df())
        assert result.decision == "confirm"
        assert result.signal is not None

    def test_rsi_oversold_fails_short(self):
        """RSI < 30 = oversold, should not short into oversold."""
        sig = _make_signal(SignalAction.SELL)
        df = _make_df(rsi=25, macd_hist=-3, macd_hist_prev=-1, ema_20=2530, volume_ratio=1.2, adx=25)
        result = combine(sig, df)
        assert not result.checks.get("rsi_not_oversold", True)

    def test_macd_positive_fails_short(self):
        """MACD positive (bullish) → fail short's macd_negative_or_turning check."""
        sig = _make_signal(SignalAction.SELL)
        df = _make_df(rsi=60, macd_hist=3.0, macd_hist_prev=1.0, ema_20=2530, volume_ratio=1.2, adx=25)
        result = combine(sig, df)
        assert not result.checks.get("macd_negative_or_turning", True)

    def test_price_above_ema20_fails_short(self):
        """Price > EMA20 = bullish trend, should fail the short's trend check."""
        sig = _make_signal(SignalAction.SELL)
        df = _make_df(close=2550, rsi=60, macd_hist=-2, macd_hist_prev=-1, ema_20=2480, volume_ratio=1.2, adx=25)
        result = combine(sig, df)
        assert not result.checks.get("price_below_ema20", True)

    def test_macd_turning_down_passes_short(self):
        """MACD hist positive but turning down (hist < prev) should pass."""
        sig = _make_signal(SignalAction.SELL)
        df = _make_df(
            close=2500, rsi=60,
            macd_hist=1.0, macd_hist_prev=3.0,  # positive but worsening
            ema_20=2530, volume_ratio=1.2, adx=25,
        )
        result = combine(sig, df)
        assert result.checks.get("macd_negative_or_turning") is True

    def test_short_downgrade_path(self):
        """2/5 = 0.40 hits downgrade threshold exactly."""
        sig = _make_signal(SignalAction.SELL, strength=SignalStrength.MODERATE)
        df = _make_df(
            close=2500, rsi=60,            # pass: rsi in [30,70]
            macd_hist=2.0, macd_hist_prev=1.0,  # FAIL: positive and worsening
            ema_20=2530,                   # pass: close(2500) < ema20(2530)
            volume_ratio=0.7,              # FAIL: below 1.0
            adx=10,                        # FAIL: below 20
        )
        result = combine(sig, df)
        # 2/5 = 0.40 which equals TECH_DOWNGRADE_THRESHOLD → downgrade
        assert result.decision == "downgrade"
        assert result.signal.strength == SignalStrength.WEAK

    def test_short_reject_path(self):
        sig = _make_signal(SignalAction.SELL)
        df = _make_df(
            close=2550, rsi=25,         # FAIL: oversold
            macd_hist=2.0, macd_hist_prev=1.0,  # FAIL: positive + worsening
            ema_20=2480,                # FAIL: price above ema20
            volume_ratio=0.5,           # FAIL
            adx=10,                     # FAIL
        )
        result = combine(sig, df)
        assert result.decision == "reject"
        assert result.signal is None


# ---------------------------------------------------------------------------
# HOLD signal test
# ---------------------------------------------------------------------------

class TestHoldSignalCombiner:
    def test_hold_always_confirms(self):
        sig = _make_signal(SignalAction.HOLD, confidence=0.60)
        df = _make_df()  # Any df
        result = combine(sig, df)
        assert result.decision == "confirm"
        assert result.signal is sig  # Same object returned
        assert result.tech_score == 1.0

    def test_hold_confidence_unchanged(self):
        sig = _make_signal(SignalAction.HOLD, confidence=0.45)
        result = combine(sig, _make_df())
        assert result.combined_confidence == 0.45


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestSignalCombinerEdgeCases:
    def test_missing_all_indicator_columns(self):
        """Empty DataFrame with only close — should not crash."""
        sig = _make_signal(SignalAction.BUY)
        df = pd.DataFrame({"close": [2500.0, 2510.0]})
        result = combine(sig, df)
        # All numeric checks will be NaN → fail, except ADX (NaN = pass)
        # Expected: 1/5 = 0.20 < 0.40 → reject
        assert result.decision == "reject"

    def test_single_row_df_no_prev(self):
        """Single row: prev() returns NaN for macd_hist_prev."""
        sig = _make_signal(SignalAction.BUY)
        df = pd.DataFrame({
            "close": [2500.0], "rsi": [50.0], "macd_hist": [3.0],
            "ema_20": [2480.0], "volume_ratio": [1.5], "adx": [30.0],
        })
        result = combine(sig, df)
        # macd_hist > 0 is True even without prev — should still pass
        assert result.checks.get("macd_positive_or_turning") is True

    def test_updated_signal_reasoning_contains_combiner_tag(self):
        """Confirmed signal reasoning should include [Combiner] annotation."""
        sig = _make_signal(SignalAction.BUY, confidence=0.80)
        df = _make_df()
        result = combine(sig, df)
        if result.signal:
            assert "[Combiner]" in result.signal.reasoning
