"""
Benchmark tracker: compares portfolio returns against NIFTY50 index.

Metrics computed daily:
  - NIFTY50 daily return % (via yfinance ^NSEI)
  - Portfolio daily return %
  - Alpha = portfolio return - benchmark return (excess return)
  - Cumulative alpha since inception
  - Rolling 20-day Sharpe (portfolio)
  - Rolling 20-day Sharpe (benchmark)
  - Beta = cov(portfolio, benchmark) / var(benchmark)  [rolling 60 days]
  - Information Ratio = mean(daily alpha) / std(daily alpha)

All metrics logged to audit trail + Telegram daily summary.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Max rolling window kept in memory
_ROLLING_WINDOW = 60


class BenchmarkTracker:
    """
    Tracks daily portfolio vs NIFTY50 returns.
    Call record_daily() once at end of each trading session.
    """

    def __init__(self):
        # deques of daily returns (floats, e.g. 0.012 = +1.2%)
        self._portfolio_returns: deque[float] = deque(maxlen=_ROLLING_WINDOW)
        self._benchmark_returns: deque[float] = deque(maxlen=_ROLLING_WINDOW)
        self._dates: deque[date]              = deque(maxlen=_ROLLING_WINDOW)

        self._cumulative_portfolio = 1.0   # multiplier from inception
        self._cumulative_benchmark = 1.0

        self._prev_portfolio_value: Optional[float] = None
        self._prev_benchmark_close: Optional[float] = None

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------

    async def record_daily(
        self,
        portfolio_value: float,
        today: date | None = None,
    ) -> dict:
        """
        Record today's portfolio value.  Fetches NIFTY50 close automatically.
        Returns today's metrics dict.
        """
        today = today or datetime.now(timezone.utc).date()
        nifty_close = await self._fetch_nifty_close()

        # Compute returns
        port_ret = (
            (portfolio_value - self._prev_portfolio_value) / self._prev_portfolio_value
            if self._prev_portfolio_value and self._prev_portfolio_value > 0
            else 0.0
        )
        bench_ret = (
            (nifty_close - self._prev_benchmark_close) / self._prev_benchmark_close
            if self._prev_benchmark_close and self._prev_benchmark_close > 0
            else 0.0
        )

        self._portfolio_returns.append(port_ret)
        self._benchmark_returns.append(bench_ret)
        self._dates.append(today)

        self._cumulative_portfolio *= (1 + port_ret)
        self._cumulative_benchmark *= (1 + bench_ret)

        self._prev_portfolio_value = portfolio_value
        self._prev_benchmark_close = nifty_close

        metrics = self._compute_metrics(port_ret, bench_ret)
        self._log_metrics(metrics, today)
        return metrics

    def set_initial_values(self, portfolio_value: float, nifty_close: float) -> None:
        """Call once at startup with yesterday's closing values as baseline."""
        self._prev_portfolio_value = portfolio_value
        self._prev_benchmark_close = nifty_close

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _compute_metrics(self, port_ret: float, bench_ret: float) -> dict:
        alpha_today = port_ret - bench_ret
        cum_alpha   = self._cumulative_portfolio - self._cumulative_benchmark

        port_list  = list(self._portfolio_returns)
        bench_list = list(self._benchmark_returns)
        alpha_list = [p - b for p, b in zip(port_list, bench_list)]

        sharpe_port  = _sharpe(port_list)
        sharpe_bench = _sharpe(bench_list)
        beta         = _beta(port_list, bench_list)
        info_ratio   = _information_ratio(alpha_list)

        return {
            "date":                    str(datetime.now(timezone.utc).date()),
            "portfolio_daily_pct":     round(port_ret * 100, 3),
            "nifty50_daily_pct":       round(bench_ret * 100, 3),
            "alpha_today_pct":         round(alpha_today * 100, 3),
            "cumulative_portfolio_pct": round((self._cumulative_portfolio - 1) * 100, 2),
            "cumulative_nifty50_pct":  round((self._cumulative_benchmark - 1) * 100, 2),
            "cumulative_alpha_pct":    round(cum_alpha * 100, 2),
            "sharpe_20d_portfolio":    round(sharpe_port, 3),
            "sharpe_20d_nifty50":      round(sharpe_bench, 3),
            "beta_60d":                round(beta, 3),
            "information_ratio":       round(info_ratio, 3),
            "beating_benchmark":       self._cumulative_portfolio > self._cumulative_benchmark,
        }

    def latest_metrics(self) -> Optional[dict]:
        """Return the most recently computed metrics, or None if no data yet."""
        if not self._portfolio_returns:
            return None
        port_ret  = self._portfolio_returns[-1]
        bench_ret = self._benchmark_returns[-1]
        return self._compute_metrics(port_ret, bench_ret)

    # ------------------------------------------------------------------
    # Data fetch
    # ------------------------------------------------------------------

    async def _fetch_nifty_close(self) -> float:
        """Fetch NIFTY50 latest close via yfinance."""
        try:
            import yfinance as yf
            ticker = yf.Ticker("^NSEI")
            hist = ticker.history(period="2d", interval="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as exc:
            logger.warning(f"BenchmarkTracker: failed to fetch NIFTY50 close: {exc}")
        return self._prev_benchmark_close or 0.0

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_metrics(self, m: dict, today: date) -> None:
        beating = "BEATING" if m["beating_benchmark"] else "LAGGING"
        logger.info(
            f"[BENCHMARK {today}] {beating} | "
            f"Portfolio={m['portfolio_daily_pct']:+.2f}% "
            f"NIFTY50={m['nifty50_daily_pct']:+.2f}% "
            f"Alpha={m['alpha_today_pct']:+.2f}% | "
            f"Cum alpha={m['cumulative_alpha_pct']:+.2f}% | "
            f"Sharpe(20d)={m['sharpe_20d_portfolio']:.2f} vs {m['sharpe_20d_nifty50']:.2f} | "
            f"Beta={m['beta_60d']:.2f} | IR={m['information_ratio']:.2f}"
        )

    def summary_text(self) -> str:
        """One-line summary for Telegram alerts."""
        m = self.latest_metrics()
        if not m:
            return "BenchmarkTracker: no data yet"
        beating = "✓ BEATING" if m["beating_benchmark"] else "✗ LAGGING"
        return (
            f"{beating} NIFTY50 | "
            f"Cumulative: Portfolio {m['cumulative_portfolio_pct']:+.1f}% "
            f"vs NIFTY50 {m['cumulative_nifty50_pct']:+.1f}% "
            f"(Alpha {m['cumulative_alpha_pct']:+.1f}%)"
        )


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _sharpe(returns: list[float], risk_free_daily: float = 0.0) -> float:
    """Annualised Sharpe from daily returns (last 20 days)."""
    window = returns[-20:] if len(returns) >= 20 else returns
    if len(window) < 5:
        return 0.0
    excess = [r - risk_free_daily for r in window]
    mean   = sum(excess) / len(excess)
    var    = sum((r - mean) ** 2 for r in excess) / len(excess)
    std    = math.sqrt(var) if var > 0 else 0.0
    return (mean / std * math.sqrt(252)) if std > 0 else 0.0


def _beta(port: list[float], bench: list[float]) -> float:
    """Rolling beta (up to last 60 days)."""
    n = min(len(port), len(bench), _ROLLING_WINDOW)
    if n < 10:
        return 1.0
    p = port[-n:]
    b = bench[-n:]
    mean_p = sum(p) / n
    mean_b = sum(b) / n
    cov = sum((p[i] - mean_p) * (b[i] - mean_b) for i in range(n)) / n
    var_b = sum((b[i] - mean_b) ** 2 for i in range(n)) / n
    return cov / var_b if var_b > 0 else 1.0


def _information_ratio(alpha_series: list[float]) -> float:
    """Information ratio = mean(alpha) / std(alpha)."""
    if len(alpha_series) < 5:
        return 0.0
    mean = sum(alpha_series) / len(alpha_series)
    var  = sum((a - mean) ** 2 for a in alpha_series) / len(alpha_series)
    std  = math.sqrt(var) if var > 0 else 0.0
    return (mean / std * math.sqrt(252)) if std > 0 else 0.0
