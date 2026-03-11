"""
Fast backtesting using vectorbt.
Generates signals from strategy code and runs vectorized simulation.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def run_momentum_backtest(
    df: pd.DataFrame,
    ema_fast: int = 20,
    ema_mid: int = 50,
    ema_slow: int = 200,
    rsi_min: float = 50,
    rsi_max: float = 70,
    atr_stop_mult: float = 1.5,
    sl_stop_mult: float = 1.5,
    init_cash: float = 1_000_000,
) -> dict:
    """
    Run a vectorbt momentum backtest.
    df must have columns: open, high, low, close, volume.
    Returns dict with performance metrics.
    """
    try:
        import vectorbt as vbt
        import numpy as np
        from signals.indicators import compute_all_indicators

        df = compute_all_indicators(df.copy())
        df = df.dropna()

        if len(df) < 250:
            return {"error": "Insufficient data for backtest (need 250+ bars)"}

        # Generate signals
        ema_f = df[f"ema_{ema_fast}"]
        ema_m = df[f"ema_{ema_mid}"]
        ema_s = df[f"ema_{ema_slow}"]
        rsi = df["rsi"]
        macd_hist = df["macd_hist"]

        entries = (
            (ema_f > ema_m) &
            (ema_m > ema_s) &
            (rsi >= rsi_min) &
            (rsi <= rsi_max) &
            (macd_hist > 0) &
            (macd_hist > macd_hist.shift(1))
        )

        # Simple SL exits based on ATR
        stops = df["close"] - sl_stop_mult * df["atr"]
        exits = df["close"] < stops.shift(1)

        # Run portfolio simulation
        pf = vbt.Portfolio.from_signals(
            close=df["close"],
            entries=entries,
            exits=exits,
            init_cash=init_cash,
            fees=0.001,     # 0.1% round-trip brokerage
            slippage=0.0005,
        )

        stats = pf.stats()
        return {
            "total_return_pct": round(float(stats.get("Total Return [%]", 0)), 2),
            "sharpe_ratio": round(float(stats.get("Sharpe Ratio", 0)), 3),
            "max_drawdown_pct": round(float(stats.get("Max Drawdown [%]", 0)), 2),
            "win_rate_pct": round(float(stats.get("Win Rate [%]", 0)), 2),
            "total_trades": int(stats.get("Total Trades", 0)),
            "calmar_ratio": round(float(stats.get("Calmar Ratio", 0)), 3),
        }

    except ImportError:
        logger.error("vectorbt not installed. Run: pip install vectorbt")
        return {"error": "vectorbt not installed"}
    except Exception as exc:
        logger.error(f"Vectorbt backtest failed: {exc}")
        return {"error": str(exc)}


def run_mean_reversion_backtest(
    df: pd.DataFrame,
    bb_period: int = 20,
    rsi_oversold: float = 30,
    vol_spike: float = 1.5,
    init_cash: float = 1_000_000,
) -> dict:
    """Run vectorbt mean reversion backtest."""
    try:
        import vectorbt as vbt
        from signals.indicators import compute_all_indicators

        df = compute_all_indicators(df.copy())
        df = df.dropna()

        entries = (
            (df["close"] <= df["bb_lower"] * 1.005) &
            (df["rsi"] < rsi_oversold) &
            (df["volume_ratio"] >= vol_spike)
        )
        exits = (df["rsi"] > 50) | (df["close"] >= df["bb_mid"])

        pf = vbt.Portfolio.from_signals(
            close=df["close"],
            entries=entries,
            exits=exits,
            init_cash=init_cash,
            fees=0.001,
            slippage=0.0005,
        )
        stats = pf.stats()
        return {
            "total_return_pct": round(float(stats.get("Total Return [%]", 0)), 2),
            "sharpe_ratio": round(float(stats.get("Sharpe Ratio", 0)), 3),
            "max_drawdown_pct": round(float(stats.get("Max Drawdown [%]", 0)), 2),
            "win_rate_pct": round(float(stats.get("Win Rate [%]", 0)), 2),
            "total_trades": int(stats.get("Total Trades", 0)),
        }

    except ImportError:
        return {"error": "vectorbt not installed"}
    except Exception as exc:
        return {"error": str(exc)}


def passes_promotion_gate(results: dict, gate: dict | None = None) -> tuple[bool, list[str]]:
    """
    Check if backtest results pass the promotion checklist.
    Returns (passes, list_of_failures).
    """
    if gate is None:
        from config.risk_params_loader import load_risk_params
        gate = load_risk_params().get("backtest_gate", {})

    failures = []
    sharpe = results.get("sharpe_ratio", 0)
    drawdown = abs(results.get("max_drawdown_pct", 100))
    win_rate = results.get("win_rate_pct", 0) / 100
    trades = results.get("total_trades", 0)

    if sharpe < gate.get("min_sharpe", 1.0):
        failures.append(f"Sharpe {sharpe:.2f} < {gate['min_sharpe']} (required)")
    if drawdown > gate.get("max_drawdown", 0.15) * 100:
        failures.append(f"MaxDD {drawdown:.1f}% > {gate['max_drawdown']*100}% (required)")
    if win_rate < gate.get("min_win_rate", 0.45):
        failures.append(f"Win rate {win_rate*100:.1f}% < {gate['min_win_rate']*100}% (required)")
    if trades < gate.get("min_trades", 200):
        failures.append(f"Trades {trades} < {gate['min_trades']} (required)")

    return len(failures) == 0, failures
