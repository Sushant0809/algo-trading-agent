"""
Walk-forward optimization and out-of-sample testing.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)


def walk_forward_test(
    df: pd.DataFrame,
    backtest_fn: Callable[[pd.DataFrame], dict],
    train_months: int = 12,
    test_months: int = 3,
    min_test_trades: int = 10,
) -> dict[str, Any]:
    """
    Walk-forward test: train on in-sample, test on out-of-sample, slide forward.

    Args:
        df: Full OHLCV DataFrame (daily bars recommended)
        backtest_fn: Function that accepts df and returns metrics dict
        train_months: Training window in months
        test_months: Out-of-sample test window in months
        min_test_trades: Minimum trades in test period to consider valid

    Returns:
        Dict with aggregated OOS metrics and per-window results.
    """
    if df.index.tz is not None:
        df = df.copy()
        df.index = df.index.tz_convert("Asia/Kolkata")

    start_date = df.index[0].date()
    end_date = df.index[-1].date()

    windows = []
    train_start = start_date

    while True:
        train_end = train_start + timedelta(days=train_months * 30)
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_months * 30)

        if test_end > end_date:
            break

        train_df = df[df.index.date >= train_start]
        train_df = train_df[train_df.index.date <= train_end]
        test_df = df[df.index.date >= test_start]
        test_df = test_df[test_df.index.date <= test_end]

        if len(train_df) < 100 or len(test_df) < 20:
            train_start = test_start
            continue

        logger.info(
            f"WFT window: train={train_start} to {train_end}, "
            f"test={test_start} to {test_end}"
        )

        train_result = backtest_fn(train_df)
        test_result = backtest_fn(test_df)

        windows.append({
            "train_start": str(train_start),
            "train_end": str(train_end),
            "test_start": str(test_start),
            "test_end": str(test_end),
            "train_sharpe": train_result.get("sharpe_ratio", 0),
            "test_sharpe": test_result.get("sharpe_ratio", 0),
            "test_return_pct": test_result.get("total_return_pct", 0),
            "test_drawdown_pct": test_result.get("max_drawdown_pct", 0),
            "test_trades": test_result.get("total_trades", 0),
            "test_win_rate": test_result.get("win_rate_pct", 0),
        })

        train_start = test_start

    if not windows:
        return {"error": "No valid walk-forward windows found"}

    valid = [w for w in windows if w["test_trades"] >= min_test_trades]

    if not valid:
        return {"windows": windows, "error": "No windows with sufficient trades"}

    avg_oos_sharpe = sum(w["test_sharpe"] for w in valid) / len(valid)
    avg_oos_return = sum(w["test_return_pct"] for w in valid) / len(valid)
    avg_oos_drawdown = sum(w["test_drawdown_pct"] for w in valid) / len(valid)
    avg_oos_winrate = sum(w["test_win_rate"] for w in valid) / len(valid)
    total_oos_trades = sum(w["test_trades"] for w in valid)

    return {
        "windows": windows,
        "valid_windows": len(valid),
        "avg_oos_sharpe": round(avg_oos_sharpe, 3),
        "avg_oos_return_pct": round(avg_oos_return, 2),
        "avg_oos_drawdown_pct": round(avg_oos_drawdown, 2),
        "avg_oos_win_rate_pct": round(avg_oos_winrate, 2),
        "total_oos_trades": total_oos_trades,
    }
