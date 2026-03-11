"""
Performance report generation using quantstats.
Generates HTML tearsheets for strategy evaluation.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def generate_quantstats_report(
    returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    strategy_name: str = "Strategy",
    output_dir: Path = Path("./logs/reports"),
    risk_free_rate: float = 0.07,  # 7% — approx India 10yr bond yield
) -> Path:
    """
    Generate a full quantstats HTML tearsheet.

    Args:
        returns: Daily returns series (as fractions, e.g. 0.01 = 1%)
        benchmark_returns: Benchmark returns (e.g. Nifty 50)
        strategy_name: Name used in report title
        output_dir: Directory to save HTML report
        risk_free_rate: Annual risk-free rate for Sharpe calculation

    Returns:
        Path to generated HTML file.
    """
    try:
        import quantstats as qs

        output_dir.mkdir(parents=True, exist_ok=True)

        # Ensure timezone-naive index for quantstats
        if returns.index.tz is not None:
            returns = returns.copy()
            returns.index = returns.index.tz_localize(None)

        if benchmark_returns is not None and benchmark_returns.index.tz is not None:
            benchmark_returns = benchmark_returns.copy()
            benchmark_returns.index = benchmark_returns.index.tz_localize(None)

        filename = output_dir / f"{strategy_name.lower().replace(' ', '_')}_report.html"

        qs.reports.html(
            returns,
            benchmark=benchmark_returns,
            rf=risk_free_rate,
            output=str(filename),
            title=f"{strategy_name} — Performance Report",
        )

        logger.info(f"Report generated: {filename}")
        return filename

    except ImportError:
        logger.error("quantstats not installed.")
        return Path("error_quantstats_not_installed.html")
    except Exception as exc:
        logger.error(f"Report generation failed: {exc}")
        raise


def trades_to_returns(trades: list[dict], initial_capital: float) -> pd.Series:
    """Convert a list of trade dicts to a daily returns series."""
    if not trades:
        return pd.Series(dtype=float)

    trade_df = pd.DataFrame(trades)
    if "exited_at" not in trade_df.columns:
        return pd.Series(dtype=float)

    trade_df["date"] = pd.to_datetime(trade_df["exited_at"]).dt.date
    daily_pnl = trade_df.groupby("date")["realized_pnl"].sum()
    daily_pnl.index = pd.to_datetime(daily_pnl.index)

    # Normalize to returns
    daily_returns = daily_pnl / initial_capital
    return daily_returns.sort_index()


def print_backtest_summary(results: dict, strategy: str = "") -> None:
    """Print a formatted backtest summary to console."""
    print(f"\n{'='*55}")
    print(f"  BACKTEST RESULTS: {strategy}")
    print(f"{'='*55}")
    for k, v in results.items():
        if k != "windows":
            print(f"  {k:30s}: {v}")
    print(f"{'='*55}\n")
