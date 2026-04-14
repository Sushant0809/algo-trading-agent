"""
Benchmark comparison: strategy performance vs NIFTY50 across multiple time periods.

Three test types:
  1. Short  (1–3 months)  — tests behaviour in specific market events
  2. Medium (6 months)    — tests a full market cycle leg
  3. Long   (1–2 years)   — tests through bull + bear + sideways

Pre-defined historically meaningful periods (2019–2025):
  - COVID Crash:         Jan 2020 – Mar 2020  (short, extreme bear)
  - COVID Recovery:      Apr 2020 – Dec 2020  (medium, strong bull)
  - Bull Peak:           Jan 2021 – Oct 2021  (medium, bull)
  - 2022 Bear:           Jan 2022 – Jun 2022  (short, bear/correction)
  - Sideways/Recovery:   Jul 2022 – Dec 2022  (medium, choppy)
  - 2023 Slow Grind:     Jan 2023 – Dec 2023  (long, slow bull)
  - 2024 Election+Rally: Jan 2024 – Dec 2024  (long, volatile then bull)
  - Full cycle:          Jan 2019 – Jan 2025  (full 6-year)

Random period sampler:
  Draws N non-overlapping random windows from 2019–2025 for each duration type.
  Used to test strategy robustness without cherry-picking.

Output:
  - Console summary table: period | strategy_return | NIFTY50_return | alpha | sharpe
  - quantstats HTML report per strategy (via report_generator.py)
  - CSV of all trades
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from backtesting.strategy_backtester import BacktestConfig, BacktestResult, StrategyBacktester
from data.yfinance_historical import fetch_multi_symbol, nifty50_daily_returns
from signals.signal_model import TradingMode

logger = logging.getLogger(__name__)

# ─── Predefined benchmark periods ────────────────────────────────────────────

@dataclass
class BacktestPeriod:
    name: str
    start: date
    end: date
    market_context: str   # What was happening in this period

    @property
    def duration_days(self) -> int:
        return (self.end - self.start).days

    @property
    def duration_label(self) -> str:
        d = self.duration_days
        if d <= 100:
            return "short"
        elif d <= 220:
            return "medium"
        else:
            return "long"


PREDEFINED_PERIODS: list[BacktestPeriod] = [
    BacktestPeriod("COVID Crash",       date(2020, 1,  1), date(2020, 3, 31), "extreme bear, NIFTY -38%"),
    BacktestPeriod("COVID Recovery",    date(2020, 4,  1), date(2020, 12,31), "strong bull, NIFTY +78%"),
    BacktestPeriod("2021 Bull Peak",    date(2021, 1,  1), date(2021, 10,31), "bull market peak"),
    BacktestPeriod("2022 Correction",   date(2022, 1,  1), date(2022, 6, 30), "Russia/inflation bear"),
    BacktestPeriod("2022 Recovery",     date(2022, 7,  1), date(2022, 12,31), "sideways/choppy recovery"),
    BacktestPeriod("2023 Slow Grind",   date(2023, 1,  1), date(2023, 12,31), "slow steady bull"),
    BacktestPeriod("2024 Full Year",    date(2024, 1,  1), date(2024, 12,31), "election + rate cut cycle"),
    BacktestPeriod("Pre-COVID Base",    date(2019, 1,  1), date(2019, 12,31), "pre-COVID base year"),
    BacktestPeriod("Full 6-Year",       date(2019, 1,  1), date(2025, 1,  1), "complete backtest period"),
]


def random_periods(
    n: int,
    duration_months: int,
    start_year: int = 2019,
    end_year: int = 2025,
    seed: int | None = None,
) -> list[BacktestPeriod]:
    """
    Generate N non-overlapping random periods of a given duration.

    Args:
        n:               Number of periods
        duration_months: Length of each period in months
        start_year:      Earliest possible start year
        end_year:        Latest possible end year
        seed:            Random seed for reproducibility

    Returns:
        List of BacktestPeriod objects
    """
    if seed is not None:
        random.seed(seed)

    universe_start = date(start_year, 1, 1)
    universe_end   = date(end_year, 1, 1)
    duration_days  = duration_months * 30

    periods: list[BacktestPeriod] = []
    used_ranges: list[tuple[date, date]] = []
    attempts = 0

    while len(periods) < n and attempts < n * 20:
        attempts += 1
        max_start = universe_end - timedelta(days=duration_days)
        if max_start <= universe_start:
            break

        total_days = (max_start - universe_start).days
        offset = random.randint(0, total_days)
        start = universe_start + timedelta(days=offset)
        end   = start + timedelta(days=duration_days)

        # Check for overlap with existing periods
        overlap = any(
            not (end <= used[0] or start >= used[1])
            for used in used_ranges
        )
        if overlap:
            continue

        used_ranges.append((start, end))
        duration_label = "short" if duration_months <= 3 else ("medium" if duration_months <= 6 else "long")
        periods.append(BacktestPeriod(
            name=f"Random {duration_label} {len(periods)+1} ({start.strftime('%b %Y')})",
            start=start,
            end=end,
            market_context=f"random {duration_months}-month window",
        ))

    return periods


# ─── Per-period comparison ────────────────────────────────────────────────────

@dataclass
class PeriodResult:
    period: BacktestPeriod
    strategy_return_pct: float
    nifty50_return_pct: float
    alpha_pct: float
    sharpe: float
    max_drawdown_pct: float
    total_trades: int
    win_rate_pct: float
    beat_benchmark: bool
    metrics: dict
    bt_result: Optional[BacktestResult] = None


def run_period(
    period: BacktestPeriod,
    symbols: list[str],
    config: BacktestConfig,
    warmup_days: int = 250,
) -> PeriodResult:
    """
    Run backtest for one period and compare against NIFTY50.

    Args:
        period:      The period to test
        symbols:     NSE symbols to trade
        config:      BacktestConfig (capital, strategies, etc.)
        warmup_days: Extra days before period start for indicator warmup

    Returns:
        PeriodResult with full metrics and benchmark comparison.
    """
    from data.yfinance_historical import fetch_multi_symbol, nifty50_daily_returns

    warmup_start = period.start - timedelta(days=warmup_days)

    logger.info(f"Running period: {period.name} ({period.start} to {period.end})")

    # Fetch data (includes warmup)
    symbol_data = fetch_multi_symbol(symbols, warmup_start, period.end)
    if not symbol_data:
        logger.error(f"No data fetched for {period.name}")
        return _empty_period_result(period)

    # Run backtest
    bt = StrategyBacktester(config)
    result = bt.run(symbol_data, period.start, period.end)

    # Fetch NIFTY50 benchmark returns for same period
    nifty_returns = nifty50_daily_returns(period.start, period.end)

    # Calculate metrics
    m = result.metrics()
    strat_return = m.get("total_return_pct", 0.0)

    # NIFTY50 total return for same period
    nifty_total = float((1 + nifty_returns).prod() - 1) * 100 if not nifty_returns.empty else 0.0
    alpha = strat_return - nifty_total

    return PeriodResult(
        period=period,
        strategy_return_pct=round(strat_return, 2),
        nifty50_return_pct=round(nifty_total, 2),
        alpha_pct=round(alpha, 2),
        sharpe=m.get("sharpe_ratio", 0.0),
        max_drawdown_pct=m.get("max_drawdown_pct", 0.0),
        total_trades=m.get("total_trades", 0),
        win_rate_pct=m.get("win_rate_pct", 0.0),
        beat_benchmark=alpha > 0,
        metrics=m,
        bt_result=result,
    )


def _empty_period_result(period: BacktestPeriod) -> PeriodResult:
    return PeriodResult(
        period=period,
        strategy_return_pct=0.0, nifty50_return_pct=0.0,
        alpha_pct=0.0, sharpe=0.0, max_drawdown_pct=0.0,
        total_trades=0, win_rate_pct=0.0, beat_benchmark=False,
        metrics={},
    )


# ─── Full comparison across multiple periods ──────────────────────────────────

@dataclass
class FullComparisonResult:
    period_results: list[PeriodResult]
    periods_beating_benchmark: int
    avg_alpha_pct: float
    avg_sharpe: float
    avg_max_drawdown_pct: float
    total_trades: int

    def summary_table(self) -> pd.DataFrame:
        rows = []
        for r in self.period_results:
            rows.append({
                "Period": r.period.name,
                "Duration": r.period.duration_label,
                "Context": r.period.market_context,
                "Strategy %": f"{r.strategy_return_pct:+.2f}%",
                "NIFTY50 %": f"{r.nifty50_return_pct:+.2f}%",
                "Alpha %": f"{r.alpha_pct:+.2f}%",
                "Sharpe": f"{r.sharpe:.2f}",
                "MaxDD %": f"{r.max_drawdown_pct:.1f}%",
                "Trades": r.total_trades,
                "Win %": f"{r.win_rate_pct:.1f}%",
                "Beat?": "✓" if r.beat_benchmark else "✗",
            })
        return pd.DataFrame(rows)

    def print_summary(self) -> None:
        df = self.summary_table()
        print(f"\n{'═'*100}")
        print(f"  BENCHMARK COMPARISON RESULTS")
        print(f"{'═'*100}")
        print(df.to_string(index=False))
        print(f"{'─'*100}")
        beat = self.periods_beating_benchmark
        total = len(self.period_results)
        print(f"  Beating benchmark: {beat}/{total} periods | "
              f"Avg Alpha: {self.avg_alpha_pct:+.2f}% | "
              f"Avg Sharpe: {self.avg_sharpe:.2f} | "
              f"Avg MaxDD: {self.avg_max_drawdown_pct:.1f}%")
        print(f"{'═'*100}\n")


def run_full_comparison(
    symbols: list[str],
    config: BacktestConfig,
    periods: list[BacktestPeriod] | None = None,
    include_random_short: int = 3,
    include_random_medium: int = 3,
    include_random_long: int = 2,
    output_dir: Path = Path("./logs/reports"),
    save_trades_csv: bool = True,
) -> FullComparisonResult:
    """
    Run strategy vs NIFTY50 across predefined + random periods.

    Args:
        symbols:              List of NSE symbols to trade
        config:               Backtest config (capital, strategies, mode)
        periods:              Custom periods (defaults to PREDEFINED_PERIODS)
        include_random_short: Number of random 3-month periods to add
        include_random_medium: Number of random 6-month periods to add
        include_random_long:  Number of random 12-month periods to add
        output_dir:           Where to save HTML reports and CSV
        save_trades_csv:      If True, save all trades to CSV

    Returns:
        FullComparisonResult with summary and per-period details.
    """
    all_periods = list(periods or PREDEFINED_PERIODS)

    # Add random periods with randomized seeds (not fixed) for true robustness testing
    base_seed = int(time.time())
    if include_random_short > 0:
        all_periods += random_periods(include_random_short, 3, seed=base_seed)
    if include_random_medium > 0:
        all_periods += random_periods(include_random_medium, 6, seed=base_seed + 1)
    if include_random_long > 0:
        all_periods += random_periods(include_random_long, 12, seed=base_seed + 2)

    logger.info(f"Running {len(all_periods)} periods across {len(symbols)} symbols")

    period_results: list[PeriodResult] = []
    all_trades: list[dict] = []

    for period in all_periods:
        try:
            result = run_period(period, symbols, config)
            period_results.append(result)

            if result.bt_result:
                for t in result.bt_result.trades:
                    row = t.to_dict()
                    row["period"] = period.name
                    all_trades.append(row)

        except Exception as exc:
            logger.error(f"Period {period.name} failed: {exc}")

    if not period_results:
        return FullComparisonResult([], 0, 0.0, 0.0, 0.0, 0)

    beat = sum(1 for r in period_results if r.beat_benchmark)
    avg_alpha = sum(r.alpha_pct for r in period_results) / len(period_results)
    avg_sharpe = sum(r.sharpe for r in period_results) / len(period_results)
    avg_dd = sum(r.max_drawdown_pct for r in period_results) / len(period_results)
    total_trades = sum(r.total_trades for r in period_results)

    comparison = FullComparisonResult(
        period_results=period_results,
        periods_beating_benchmark=beat,
        avg_alpha_pct=round(avg_alpha, 2),
        avg_sharpe=round(avg_sharpe, 3),
        avg_max_drawdown_pct=round(avg_dd, 2),
        total_trades=total_trades,
    )

    comparison.print_summary()

    # Save trades CSV
    if save_trades_csv and all_trades:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = output_dir / "backtest_trades.csv"
        pd.DataFrame(all_trades).to_csv(csv_path, index=False)
        logger.info(f"Trades saved: {csv_path}")

    # Generate quantstats reports for the full 6-year period
    full_period = next((r for r in period_results if r.period.name == "Full 6-Year"), None)
    if full_period and full_period.bt_result:
        _generate_report(full_period, config, output_dir)

    return comparison


def _generate_report(
    period_result: PeriodResult,
    config: BacktestConfig,
    output_dir: Path,
) -> None:
    """Generate quantstats HTML tearsheet for a period result."""
    try:
        from backtesting.report_generator import generate_quantstats_report

        bt_result = period_result.bt_result
        if bt_result is None or bt_result.daily_values.empty:
            return

        strategy_returns = bt_result.to_returns_series()
        benchmark_returns = nifty50_daily_returns(
            period_result.period.start, period_result.period.end
        )

        # Align to same dates
        if not benchmark_returns.empty:
            combined = pd.concat([strategy_returns, benchmark_returns], axis=1).dropna()
            strategy_returns = combined.iloc[:, 0]
            benchmark_returns = combined.iloc[:, 1]

        strategy_name = f"AlgoTrader ({period_result.period.name})"
        report_path = generate_quantstats_report(
            returns=strategy_returns,
            benchmark_returns=benchmark_returns if not benchmark_returns.empty else None,
            strategy_name=strategy_name,
            output_dir=Path(output_dir),
        )
        logger.info(f"Quantstats report: {report_path}")

    except Exception as exc:
        logger.warning(f"Report generation failed: {exc}")


# ─── Convenience runner ───────────────────────────────────────────────────────

def run_quick_comparison(
    symbols: list[str] | None = None,
    capital: float = 1_000_000,
    strategies: list[str] | None = None,
    mode: TradingMode = TradingMode.SWING,
) -> FullComparisonResult:
    """
    Quick one-call entry point for running a full benchmark comparison.

    Example:
        from backtesting.benchmark_comparison import run_quick_comparison
        from config.universes import NIFTY50
        result = run_quick_comparison(symbols=NIFTY50[:20])
        result.print_summary()
    """
    from config.universes import NIFTY50

    symbols = symbols or NIFTY50[:20]
    strategies = strategies or [
        "momentum", "mean_reversion", "breakout",
        "oversold_bounce", "overbought_short",
    ]

    config = BacktestConfig(
        initial_capital=capital,
        strategies=strategies,
        mode=mode,
        max_position_pct=0.05,
        cash_reserve_pct=0.10,
        max_concurrent_positions=10,
        max_hold_days_swing=20,
    )

    return run_full_comparison(
        symbols=symbols,
        config=config,
        include_random_short=3,
        include_random_medium=3,
        include_random_long=2,
    )


def run_llm_comparison(
    symbols: list[str] | None = None,
    capital: float = 1_000_000,
    periods: list[BacktestPeriod] | None = None,
) -> FullComparisonResult:
    """
    Run the LLM portfolio manager vs NIFTY50 benchmark.

    Uses claude-haiku-4-5 for daily allocation decisions (~1 API call/day).
    Haiku 4.5 pricing: $0.80/M input, $4.00/M output tokens.
    Default 3 periods (~189 trading days): ~$0.36 for 15 symbols, ~$0.44 for 20 symbols.

    Example:
        from backtesting.benchmark_comparison import run_llm_comparison, PREDEFINED_PERIODS
        result = run_llm_comparison(
            periods=[PREDEFINED_PERIODS[0], PREDEFINED_PERIODS[3]],  # COVID crash + 2022 bear
        )
        result.print_summary()
    """
    from config.universes import NIFTY50

    symbols = symbols or NIFTY50[:15]

    # Default: 3 well-known 3-month periods (cheap to run, cover different regimes)
    if periods is None:
        periods = [
            BacktestPeriod("COVID Crash (LLM)",    date(2020, 1,  1), date(2020, 3, 31), "extreme bear"),
            BacktestPeriod("2022 Correction (LLM)", date(2022, 1,  1), date(2022, 6, 30), "Russia/inflation bear"),
            BacktestPeriod("2023 Slow Grind (LLM)", date(2023, 7,  1), date(2023, 12,31), "slow bull"),
        ]

    config = BacktestConfig(
        initial_capital=capital,
        allocation_mode="llm",
        max_concurrent_positions=15,
        cash_reserve_pct=0.02,
        max_hold_days_swing=30,
        mode=TradingMode.SWING,
        strategies=[],   # not used in LLM mode
    )

    return run_full_comparison(
        symbols=symbols,
        config=config,
        periods=periods,
        include_random_short=0,
        include_random_medium=0,
        include_random_long=0,
    )
