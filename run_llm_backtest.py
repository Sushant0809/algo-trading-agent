"""
Run LLM portfolio manager backtest across different universes and time periods.
Supports large cap (Nifty50), mid cap, small cap, and combined universes.

Usage:
    # Default: Nifty50 only (15 symbols, 3 periods)
    python3 run_llm_backtest.py                          # ~$0.36

    # Test with mid + large cap combined (~65 stocks)
    python3 run_llm_backtest.py --short --mid-large      # ~$1.56
    python3 run_llm_backtest.py --medium --mid-large     # ~$2.30
    python3 run_llm_backtest.py --full --mid-large       # ~$25.20

    # Test with all market caps combined (~165 stocks)
    python3 run_llm_backtest.py --short --all-cap        # ~$2.88
    python3 run_llm_backtest.py --medium --all-cap       # ~$4.23
    python3 run_llm_backtest.py --full --all-cap         # ~$45.00

    # Custom number of stocks
    python3 run_llm_backtest.py --short --all-cap --num 100

Universe options:
    --nifty50     Large cap only (50 stocks) [default]
    --mid-large   Large + Mid cap combined (65 stocks)
    --all-cap     Large + Mid + Small cap combined (165 stocks)
    --midcap      Mid cap only (up to 50 stocks)
    --smallcap    Small cap only (up to 50 stocks)

Period options:
    --short       3 periods: COVID crash, 2022 bear, 2023 bull [default]
    --medium      5 key periods covering different market regimes
    --full        17 predefined periods (6+ years of market history)
"""
import logging
import sys
from datetime import date
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet noisy libs
logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("peewee").setLevel(logging.WARNING)

from backtesting.benchmark_comparison import (
    BacktestPeriod, run_llm_comparison, run_full_comparison, PREDEFINED_PERIODS,
)
from backtesting.strategy_backtester import BacktestConfig
from config.universes import NIFTY50, NIFTY_MIDCAP_150_SAMPLE, NIFTY_ALL_CAP, NIFTY_MID_LARGE_CAP
from signals.signal_model import TradingMode

# Parse command line arguments
args = sys.argv[1:]
mode = "--short"
universe = "nifty50"
symbols = NIFTY50[:15]
num_stocks = None

i = 0
while i < len(args):
    arg = args[i]
    if arg in ("--short", "--medium", "--full"):
        mode = arg
    elif arg == "--all-cap":
        universe = "all_cap"
        # Use larger pool: up to 80 stocks from combined universe
        symbols = NIFTY_ALL_CAP[:80]
    elif arg == "--mid-large":
        universe = "mid_large_cap"
        # Combined large + mid: ~65 stocks
        symbols = NIFTY_MID_LARGE_CAP[:65]
    elif arg == "--midcap":
        universe = "midcap"
        symbols = NIFTY_MIDCAP_150_SAMPLE[:50]
    elif arg == "--smallcap":
        universe = "smallcap"
        symbols = NIFTY_SMALLCAP_250_SAMPLE[:50]
    elif arg == "--nifty50":
        universe = "nifty50"
        symbols = NIFTY50[:15]
    elif arg == "--num" and i + 1 < len(args):
        # Allow custom number of stocks: --num 100
        try:
            num_stocks = int(args[i + 1])
            # Adjust based on universe
            if universe == "all_cap":
                symbols = NIFTY_ALL_CAP[:num_stocks]
            elif universe == "mid_large_cap":
                symbols = NIFTY_MID_LARGE_CAP[:num_stocks]
            elif universe == "midcap":
                symbols = NIFTY_MIDCAP_150_SAMPLE[:num_stocks]
            elif universe == "smallcap":
                symbols = NIFTY_SMALLCAP_250_SAMPLE[:num_stocks]
            elif universe == "nifty50":
                symbols = NIFTY50[:num_stocks]
        except ValueError:
            pass
        i += 1
    i += 1

if mode == "--full":
    # All 17 predefined + random periods
    # Cost scales with ~$0.002/symbol/day
    stock_cost = len(symbols) / 15  # baseline 15 symbols = $0.36 for 3 periods
    cost_est = f"${stock_cost * 10.65:.2f}"  # 17 periods ≈ 10.65x of 3 periods
    print(f"\nRunning FULL comparison (17 periods, {len(symbols)} {universe} symbols) ≈ {cost_est}")
    result = run_full_comparison(
        symbols=symbols,
        config=BacktestConfig(
            allocation_mode="llm",
            max_concurrent_positions=min(20, len(symbols)),
            cash_reserve_pct=0.02,
            max_hold_days_swing=30,
            mode=TradingMode.SWING,
            strategies=[],
        ),
        periods=PREDEFINED_PERIODS,
        include_random_short=3,
        include_random_medium=3,
        include_random_long=2,
    )

elif mode == "--medium":
    # 5 key periods covering different regimes
    periods = [
        BacktestPeriod("COVID Crash",    date(2020,1,1),  date(2020,3,31),  "extreme bear"),
        BacktestPeriod("COVID Recovery", date(2020,4,1),  date(2020,12,31), "strong bull"),
        BacktestPeriod("2022 Bear",      date(2022,1,1),  date(2022,6,30),  "bear/correction"),
        BacktestPeriod("2023 Bull",      date(2023,1,1),  date(2023,12,31), "slow bull"),
        BacktestPeriod("2024 Full",      date(2024,1,1),  date(2024,12,31), "election + rally"),
    ]
    stock_cost = len(symbols) / 15  # baseline 15 symbols
    cost_est = f"${stock_cost * 1.47:.2f}"  # 5 periods ≈ 1.47x of 3 periods
    print(f"\nRunning MEDIUM comparison (5 key periods, {len(symbols)} {universe} symbols) ≈ {cost_est}")
    result = run_full_comparison(
        symbols=symbols,
        config=BacktestConfig(
            allocation_mode="llm",
            max_concurrent_positions=min(20, len(symbols)),
            cash_reserve_pct=0.02,
            max_hold_days_swing=30,
            mode=TradingMode.SWING,
            strategies=[],
        ),
        periods=periods,
        include_random_short=0,
        include_random_medium=0,
        include_random_long=0,
    )

else:
    # Default: 3 short periods
    stock_cost = len(symbols) / 15  # baseline 15 symbols = $0.36
    cost_est = f"${stock_cost * 0.36:.2f}"
    print(f"\nRunning SHORT comparison (3 periods, {len(symbols)} {universe} symbols) ≈ {cost_est}")
    result = run_llm_comparison(symbols=symbols)

result.print_summary()
