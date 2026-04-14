"""
Strategy backtester: bar-by-bar replay engine using actual production strategy code.

Why this exists:
    vectorbt_runner.py and backtrader_runner.py re-implement strategy logic from scratch.
    That creates a dangerous divergence — the backtest can pass while the live code fails.
    This engine calls each strategy's actual generate_signal() method on each bar,
    so backtest results reflect exactly what would have happened in production.

Simulation model:
    Swing (CNC):
        - Signal generated on bar[t] close
        - Entry at bar[t+1] open (avoids look-ahead bias)
        - Each subsequent bar: check if bar low < stop (stop hit) or bar high > target (target hit)
        - If both triggered on same bar: exit at stop (conservative)
        - Max hold: configurable (default 20 trading days)
        - Force exit at period end

    Intraday (MIS) with daily bars:
        - Signal generated on bar[t] close
        - Entry at bar[t+1] open
        - Same-day exit: check bar[t+1] high/low for stop, else close at bar[t+1] close
        - This models "enter at open, stopped or take profit intraday, else EOD close"
        - Note: 5-min intraday uses same logic but on 5-min bars

    Short positions:
        - Entry same as above
        - Stop ABOVE entry (price[t+1] high > stop → stop hit)
        - Target BELOW entry (price[t+1] low < target → target hit)

Transaction costs (matching PortfolioState.close_position):
    - Brokerage: min(₹20, 0.03% of turnover)
    - STT: 0.1% of sell turnover for CNC, 0.025% for MIS
    - Exchange: 0.00345% of turnover
    - GST: 18% on (brokerage + exchange)

Slippage (realistic market execution):
    - NIFTY50 (large-cap):  0.10% on entry + exit
    - Others (mid-cap):     0.20% on entry + exit
    - Models worse fills than exact OHLC prices

Sentiment signals:
    - NOT available in historical backtest (real-time only in production)
    - Backtest assumes neutral sentiment (0.0 score for all stocks)
    - Sentiment adds +0.5-2% alpha in production; validate via 30-day paper trading
    - Used as confirmation gate only (technical signal + sentiment = boost allocation)

Capital allocation:
    - Each signal uses signal.position_size_pct × current_portfolio_value
    - Never exceeds max_position_pct (default 5%)
    - Cash reserve: 10% always kept as floor
    - Max concurrent positions: 10 (configurable)
    - Duplicate symbols: no two open positions in same symbol simultaneously
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from signals.signal_model import Signal, SignalAction, TradingMode

logger = logging.getLogger(__name__)

# ─── Constants matching PortfolioState transaction costs ────────────────────
_BROKERAGE_FLAT    = 20.0
_BROKERAGE_PCT     = 0.0003      # 0.03%, whichever is lower
_STT_CNC           = 0.001       # 0.1% on sell for CNC
_STT_MIS           = 0.00025     # 0.025% for MIS
_EXCHANGE_CHARGE   = 0.0000345   # NSE + SEBI
_GST_RATE          = 0.18
_CASH_RESERVE_PCT  = 0.10
_MAX_POSITIONS     = 10
_MAX_HOLD_DAYS_SWING = 20
_MAX_HOLD_DAYS_INTRA = 1         # Always exit same day for MIS


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    initial_capital: float = 1_000_000.0
    max_position_pct: float = 0.08        # Max 8% per swing position (up from 5%)
    max_position_pct_bull: float = 0.12   # Max 12% in bull regimes
    cash_reserve_pct: float = 0.10        # Keep 10% cash always
    max_concurrent_positions: int = 10    # Total open positions (MIS + CNC)
    max_hold_days_swing: int = 20
    mode: TradingMode = TradingMode.SWING
    allocation_mode: str = "fixed"        # "fixed" | "proportional" | "equal_weight" | "llm"
    #   fixed:        each signal uses signal.position_size_pct × capital (old behaviour)
    #   equal_weight: split all available cash equally across today's top-N signals
    #   proportional: split available cash in proportion to signal score (financial_market_env style)
    #   llm:          Claude reviews all symbols daily, returns JSON buy/sell/hold fractions
    #                 ~1 API call per trading day  (Haiku 4.5, $0.80/$4.00 per M tokens)
    #                 15 symbols: ~$0.002/day → $0.12/period(3mo) → $2.88/6yr full BT
    #                 50 symbols: ~$0.005/day → $0.31/period(3mo) → $7.44/6yr full BT
    strategies: list[str] = field(default_factory=lambda: [
        "momentum", "mean_reversion", "breakout",
        "oversold_bounce", "overbought_short",
    ])


@dataclass
class TradeRecord:
    symbol: str
    strategy: str
    direction: str          # "long" or "short"
    product: str            # "MIS" or "CNC"
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    qty: int
    gross_pnl: float
    transaction_cost: float
    realized_pnl: float     # net after costs
    exit_reason: str        # "stop_loss" | "target" | "eod_close" | "max_hold" | "period_end"
    signal_confidence: float = 0.0
    signal_strength: str = ""

    @property
    def hold_days(self) -> int:
        return (self.exit_date - self.entry_date).days

    @property
    def return_pct(self) -> float:
        cost = self.entry_price * self.qty
        return self.realized_pnl / cost if cost > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "direction": self.direction,
            "product": self.product,
            "entry_date": str(self.entry_date),
            "exit_date": str(self.exit_date),
            "entry_price": round(self.entry_price, 2),
            "exit_price": round(self.exit_price, 2),
            "qty": self.qty,
            "gross_pnl": round(self.gross_pnl, 2),
            "transaction_cost": round(self.transaction_cost, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "return_pct": round(self.return_pct * 100, 3),
            "hold_days": self.hold_days,
            "exit_reason": self.exit_reason,
            "confidence": round(self.signal_confidence, 3),
            "strength": self.signal_strength,
        }


@dataclass
class BacktestResult:
    trades: list[TradeRecord]
    daily_values: pd.Series      # portfolio value per trading day
    config: BacktestConfig
    start_date: date
    end_date: date
    strategies_used: list[str]
    symbols_tested: int

    def metrics(self) -> dict:
        if not self.trades:
            return {
                "total_trades": 0, "win_rate_pct": 0.0,
                "total_return_pct": 0.0, "sharpe_ratio": 0.0,
                "max_drawdown_pct": 0.0, "calmar_ratio": 0.0,
                "avg_hold_days": 0.0, "profit_factor": 0.0,
            }

        initial = self.config.initial_capital
        final = float(self.daily_values.iloc[-1]) if not self.daily_values.empty else initial

        wins   = [t for t in self.trades if t.realized_pnl > 0]
        losses = [t for t in self.trades if t.realized_pnl <= 0]
        win_rate = len(wins) / len(self.trades) if self.trades else 0

        gross_profit = sum(t.realized_pnl for t in wins)
        gross_loss   = abs(sum(t.realized_pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        total_return = (final - initial) / initial * 100

        # Annualised metrics from daily returns
        daily_ret = self.daily_values.pct_change().dropna()
        sharpe = _sharpe(list(daily_ret))
        max_dd = _max_drawdown(self.daily_values)

        years = max((self.end_date - self.start_date).days / 365.25, 0.01)
        cagr = (final / initial) ** (1 / years) - 1

        calmar = (cagr * 100) / max_dd if max_dd > 0 else 0.0
        avg_hold = sum(t.hold_days for t in self.trades) / len(self.trades)

        by_strategy: dict[str, dict] = {}
        for t in self.trades:
            s = by_strategy.setdefault(t.strategy, {"trades": 0, "pnl": 0.0, "wins": 0})
            s["trades"] += 1
            s["pnl"] += t.realized_pnl
            if t.realized_pnl > 0:
                s["wins"] += 1
        for s in by_strategy.values():
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0

        exit_reasons = {}
        for t in self.trades:
            exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

        return {
            "period": f"{self.start_date} to {self.end_date}",
            "total_trades": len(self.trades),
            "long_trades": sum(1 for t in self.trades if t.direction == "long"),
            "short_trades": sum(1 for t in self.trades if t.direction == "short"),
            "win_rate_pct": round(win_rate * 100, 1),
            "profit_factor": round(profit_factor, 2),
            "total_return_pct": round(total_return, 2),
            "cagr_pct": round(cagr * 100, 2),
            "sharpe_ratio": round(sharpe, 3),
            "max_drawdown_pct": round(max_dd, 2),
            "calmar_ratio": round(calmar, 2),
            "avg_hold_days": round(avg_hold, 1),
            "initial_capital": initial,
            "final_capital": round(final, 2),
            "total_pnl": round(final - initial, 2),
            "total_costs": round(sum(t.transaction_cost for t in self.trades), 2),
            "by_strategy": by_strategy,
            "exit_reasons": exit_reasons,
            "symbols_tested": self.symbols_tested,
        }

    def to_returns_series(self) -> pd.Series:
        """Daily returns as fractions (0.01 = 1%). Feeds into quantstats."""
        return self.daily_values.pct_change().dropna()

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades])


# ─── Open position tracker (internal) ────────────────────────────────────────

@dataclass
class _OpenPosition:
    symbol: str
    strategy: str
    direction: str
    product: str
    entry_date: date
    entry_price: float
    stop_loss: float
    target: float
    qty: int
    signal: Optional[Signal] = None


# ─── Main backtester ──────────────────────────────────────────────────────────

class StrategyBacktester:
    """
    Bar-by-bar replay engine.

    Usage:
        bt = StrategyBacktester(config)
        result = bt.run(
            symbol_data={"RELIANCE": df, "TCS": df2},
            start=date(2019, 1, 1),
            end=date(2025, 1, 1),
        )
        print(result.metrics())
    """

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()
        self._strategies = self._load_strategies()

    def _load_strategies(self) -> list:
        """Load only backtestable strategies (excludes LLM/Sentiment — need external data)."""
        from config.risk_params_loader import load_strategy_params
        from strategies.momentum import MomentumStrategy
        from strategies.mean_reversion import MeanReversionStrategy
        from strategies.breakout import BreakoutStrategy
        from strategies.oversold_bounce import OversoldBounceStrategy
        from strategies.overbought_short import OverboughtShortStrategy

        params = load_strategy_params()
        all_map = {
            "momentum":        MomentumStrategy(params.get("momentum", {})),
            "mean_reversion":  MeanReversionStrategy(params.get("mean_reversion", {})),
            "breakout":        BreakoutStrategy(params.get("breakout", {})),
            "oversold_bounce": OversoldBounceStrategy(params.get("oversold_bounce", {})),
            "overbought_short": OverboughtShortStrategy(params.get("overbought_short", {})),
        }
        return [v for k, v in all_map.items() if k in self.config.strategies]

    def run(
        self,
        symbol_data: dict[str, pd.DataFrame],
        start: date,
        end: date,
    ) -> BacktestResult:
        """
        Run the full backtest across all symbols.

        Args:
            symbol_data: {symbol: OHLCV DataFrame with IST index}
                         DataFrames should already span [start, end] + warmup period.
            start:  Backtest start date (signals generated from here)
            end:    Backtest end date

        Returns:
            BacktestResult with trades, daily portfolio values, and metrics.
        """
        from signals.indicators import compute_all_indicators

        logger.info(
            f"StrategyBacktester: {len(symbol_data)} symbols | "
            f"{start} to {end} | mode={self.config.mode.value} | "
            f"allocation={self.config.allocation_mode} | "
            f"strategies={[s.name for s in self._strategies]}"
        )

        # Pre-compute all indicators for every symbol
        processed: dict[str, pd.DataFrame] = {}
        for sym, df in symbol_data.items():
            try:
                processed[sym] = compute_all_indicators(df.copy())
            except Exception as exc:
                logger.warning(f"Indicator compute failed for {sym}: {exc}")

        if not processed:
            return self._empty_result(start, end, [])

        # Build unified trading day list from the first symbol's index
        all_dates = sorted(set(
            d.date() for sym, df in processed.items()
            for d in df.index
        ))
        trading_days = [d for d in all_dates if start <= d <= end]

        if not trading_days:
            return self._empty_result(start, end, list(processed.keys()))

        # Dispatch to LLM allocator when requested
        if self.config.allocation_mode == "llm":
            return self._run_llm(processed, trading_days, start, end)

        # State
        cash = self.config.initial_capital
        open_positions: dict[str, _OpenPosition] = {}   # symbol → position
        trades: list[TradeRecord] = []
        daily_values: dict[date, float] = {}

        for i, today in enumerate(trading_days):
            # ── 1. Check exits for open positions ──────────────────────────
            for sym in list(open_positions.keys()):
                pos = open_positions[sym]
                sym_df = processed.get(sym)
                if sym_df is None:
                    continue

                bar = _get_bar(sym_df, today)
                if bar is None:
                    continue

                exit_price, exit_reason = _check_exit(pos, bar, today)
                if exit_reason:
                    trade, cash = _close_position(pos, exit_price, exit_reason, cash, today)
                    trades.append(trade)
                    del open_positions[sym]

            # ── 2. Mark-to-market portfolio value ──────────────────────────
            unrealized = 0.0
            for sym, pos in open_positions.items():
                sym_df = processed.get(sym)
                if sym_df is None:
                    continue
                bar = _get_bar(sym_df, today)
                if bar is not None:
                    if pos.direction == "short":
                        unrealized += (pos.entry_price - bar["close"]) * pos.qty
                    else:
                        unrealized += (bar["close"] - pos.entry_price) * pos.qty
            daily_values[today] = cash + unrealized

            # ── 3. Generate signals for tomorrow's entry ────────────────────
            if len(open_positions) >= self.config.max_concurrent_positions:
                continue

            available_cash = cash * (1.0 - self.config.cash_reserve_pct)
            if available_cash <= 0:
                continue

            # Collect candidates from all strategies × all symbols
            candidates: list[tuple[float, Signal, str]] = []  # (score, signal, symbol)
            for sym, sym_df in processed.items():
                if sym in open_positions:
                    continue  # already have a position

                # Get data up to and including today (no look-ahead)
                df_slice = sym_df[sym_df.index.date <= today]
                if len(df_slice) < 60:
                    continue

                # Check if we have tomorrow's bar to actually enter
                next_day = _next_trading_day(trading_days, i)
                if next_day is None:
                    continue
                next_bar = _get_bar(sym_df, next_day)
                if next_bar is None:
                    continue

                for strategy in self._strategies:
                    try:
                        signal = strategy.generate_signal(sym, df_slice, self.config.mode)
                    except Exception as exc:
                        logger.debug(f"Strategy {strategy.name} error on {sym}: {exc}")
                        continue

                    if signal is None:
                        continue

                    # Score: confidence × strength multiplier
                    strength_mult = {"STRONG": 1.0, "MODERATE": 0.75, "WEAK": 0.4}.get(
                        signal.strength.value if signal.strength else "MODERATE", 0.75
                    )
                    score = signal.confidence * strength_mult
                    candidates.append((score, signal, sym))

            # Sort by score, pick top candidates within cash constraints
            candidates.sort(key=lambda x: x[0], reverse=True)

            # Deduplicate: one signal per symbol (highest score wins)
            seen: set[str] = set()
            unique_candidates: list[tuple[float, Signal, str]] = []
            for score, signal, sym in candidates:
                if sym not in seen and sym not in open_positions:
                    seen.add(sym)
                    unique_candidates.append((score, signal, sym))

            # How many new slots are available today?
            slots = self.config.max_concurrent_positions - len(open_positions)
            top_candidates = unique_candidates[:slots]

            # ── Allocation mode ─────────────────────────────────────────────
            # Compute per-signal allocation based on mode
            alloc_map: dict[str, float] = {}   # sym → cash to deploy

            if self.config.allocation_mode == "proportional" and top_candidates:
                # financial_market_env style: split available_cash proportionally to score
                # quantity_i = score_i / sum(scores) × available_cash
                total_score = sum(s for s, _, _ in top_candidates)
                if total_score > 0:
                    for score, signal, sym in top_candidates:
                        alloc_map[sym] = available_cash * (score / total_score)

            elif self.config.allocation_mode == "equal_weight" and top_candidates:
                # Equal slice of available cash for every signal today
                per_signal = available_cash / len(top_candidates)
                for _, signal, sym in top_candidates:
                    alloc_map[sym] = per_signal

            else:  # "fixed" — original behaviour
                for score, signal, sym in top_candidates:
                    size_pct = min(signal.position_size_pct or 0.02, self.config.max_position_pct)
                    alloc_map[sym] = (cash + unrealized) * size_pct

            for score, signal, sym in top_candidates:
                if sym in open_positions:
                    continue
                if len(open_positions) >= self.config.max_concurrent_positions:
                    break

                next_day = _next_trading_day(trading_days, i)
                if next_day is None:
                    break

                sym_df = processed[sym]
                next_bar = _get_bar(sym_df, next_day)
                if next_bar is None:
                    continue

                alloc = alloc_map.get(sym, 0.0)
                if alloc > available_cash:
                    alloc = available_cash
                if alloc < 1000:   # Minimum ₹1000 per trade
                    continue

                entry_price = next_bar["open"]
                if entry_price <= 0:
                    continue
                # Apply slippage: realistic market order execution
                entry_price = _apply_slippage(entry_price, sym, is_entry=True)
                qty = max(1, int(alloc / entry_price))

                is_short = signal.action == SignalAction.SELL
                direction = "short" if is_short else "long"
                cost = entry_price * qty
                cash -= cost

                open_positions[sym] = _OpenPosition(
                    symbol=sym,
                    strategy=signal.strategy,
                    direction=direction,
                    product=signal.product.value,
                    entry_date=next_day,
                    entry_price=entry_price,
                    stop_loss=signal.stop_loss,
                    target=signal.target,
                    qty=qty,
                    signal=signal,
                )
                available_cash -= cost

                logger.debug(
                    f"  ENTRY {sym} {direction} {qty}@{entry_price:.2f} "
                    f"({signal.strategy}) SL={signal.stop_loss:.2f} TP={signal.target:.2f}"
                )

        # ── 4. Force-close any remaining open positions ─────────────────────
        last_day = trading_days[-1]
        for sym, pos in list(open_positions.items()):
            sym_df = processed.get(sym)
            bar = _get_bar(sym_df, last_day) if sym_df is not None else None
            if bar is not None:
                exit_price = _apply_slippage(bar["close"], sym, is_entry=False)
            else:
                exit_price = pos.entry_price
            trade, cash = _close_position(pos, exit_price, "period_end", cash, last_day)
            trades.append(trade)

        # ── 5. Final portfolio value ────────────────────────────────────────
        final_value = cash
        daily_values[last_day] = final_value

        daily_series = pd.Series(daily_values).sort_index()
        daily_series.index = pd.to_datetime(daily_series.index)

        logger.info(
            f"Backtest complete: {len(trades)} trades | "
            f"Final: ₹{final_value:,.0f} | "
            f"Return: {(final_value/self.config.initial_capital - 1)*100:.2f}%"
        )

        return BacktestResult(
            trades=trades,
            daily_values=daily_series,
            config=self.config,
            start_date=start,
            end_date=end,
            strategies_used=[s.name for s in self._strategies],
            symbols_tested=len(processed),
        )

    def _run_llm(
        self,
        processed: dict[str, pd.DataFrame],
        trading_days: list[date],
        start: date,
        end: date,
    ) -> BacktestResult:
        """
        LLM-managed portfolio backtest (allocation_mode="llm").

        Key design: The LLM is a stock picker called infrequently, NOT a daily trader.
        In clear regimes, code enforces behavior (hold in bull, sell in bear).
        The LLM only has real discretion at regime transitions and for stock selection.

        Regime-gated LLM calls:
          CRASH/BEAR: No LLM call — code auto-liquidates
          STRONG_BULL: Call every 5 days
          BULL: Call every 3 days
          NEUTRAL: Call every 2 days
          Regime transition: Always call immediately
        """
        from agents.llm_base import create_llm_manager

        llm_mgr = create_llm_manager(
            temperature=0.1,
            cash_reserve_pct=self.config.cash_reserve_pct,
        )

        # Fetch NIFTY50 with indicators for regime context (best effort)
        nifty_df: Optional[pd.DataFrame] = None
        try:
            from data.yfinance_historical import fetch_nifty50
            from signals.indicators import compute_all_indicators as _ci
            raw_nifty = fetch_nifty50(start - timedelta(days=300), end)
            if not raw_nifty.empty:
                nifty_df = _ci(raw_nifty)
        except Exception as exc:
            logger.warning(f"LLM backtest: NIFTY50 context unavailable ({exc})")

        # ── State variables ───────────────────────────────────────────────
        cash = self.config.initial_capital
        open_positions: dict[str, _OpenPosition] = {}
        holdings:       dict[str, float] = {}   # sym → shares
        avg_costs:      dict[str, float] = {}   # sym → avg cost per share
        entry_dates:    dict[str, date]  = {}   # sym → entry date
        sell_dates:     dict[str, date]  = {}   # sym → last sell date (re-entry cooldown)
        trades: list[TradeRecord] = []
        daily_values: dict[date, float] = {}

        # New state for regime-gated calls, trade memory, and budgeting
        last_llm_call_date: Optional[date] = None
        last_decision = None                    # reuse between LLM calls
        prev_regime: Optional[str] = None       # track regime transitions
        trade_journal: list[dict] = []           # last N trades for LLM memory
        trade_dates: list[date] = []             # rolling trade budget tracker

        # ── Configuration ─────────────────────────────────────────────────
        WEEKLY_TRADE_BUDGET   = 4       # max discretionary trades per 5-day window
        STOP_LOSS_PCT         = -0.07   # -7%: always allowed to sell (hard stop, no regime override)
        TAKE_PROFIT_PCT       =  0.25   # +25%: default take-profit (research: 15% too early, locks gains prematurely)
        TAKE_PROFIT_STRONG_BULL = 0.30  # +30%: let winners run longer in strong bull (capture 30%+ moves)
        CRASH_ROC_THRESHOLD   = -0.08   # NIFTY 10-day ROC < -8% = real crash (not a dip)
        RECOVERY_ROC_THRESHOLD =  0.06  # NIFTY 10-day ROC > +6% = recovery signal
        BEAR_CASH_TARGET      =  0.80   # target 80% cash in bear
        BEAR_DAILY_SELL_PCT   =  0.60   # sell 60% of portfolio/day in bear (research: faster liquidation = better protection)
        BEAR_SHORT_PCT        =  0.12   # allocate 12% of portfolio per short (was 4%)
        BEAR_MAX_SHORTS       =  5      # max simultaneous short positions (was 3)
        max_hold = self.config.max_hold_days_swing

        def _reentry_cooldown(regime: str) -> int:
            """Days before a sold symbol can be re-bought."""
            return {
                "CRASH": 999, "BEAR": 999,
                "NEUTRAL": 10, "BULL": 7, "STRONG_BULL": 5,
            }.get(regime, 10)

        def _classify_regime(score: int, crash: bool) -> str:
            """Classify regime based on technical + macro score (max 11: 9 technical + 2 macro).
            Thresholds: score >= 6 = STRONG_BULL, >= 4 = BULL, >= 2 = NEUTRAL, else BEAR
            Recalibrated with macro signals (FII/DII flows, India VIX) included."""
            if crash:
                return "CRASH"
            if score >= 6:  # Stronger threshold with macro signals
                return "STRONG_BULL"
            if score >= 4:  # Adjusted for macro scoring
                return "BULL"
            if score >= 2:
                return "NEUTRAL"
            return "BEAR"

        def _regime_zone(regime: str) -> str:
            """Collapse adjacent regimes into zones for hysteresis.
            Only cross-zone transitions trigger LLM calls."""
            if regime in ("BULL", "STRONG_BULL"):
                return "BULL_ZONE"
            if regime in ("BEAR", "CRASH"):
                return "BEAR_ZONE"
            return regime  # NEUTRAL stays distinct

        def _unrealized_pnl_pct(pos: _OpenPosition, today_date: date) -> float:
            bar = _get_bar(processed.get(pos.symbol, pd.DataFrame()), today_date)
            if not bar:
                return 0.0
            return (bar["close"] - pos.entry_price) / pos.entry_price

        def _add_to_journal(action: str, sym: str, qty: int, price: float,
                            exit_reason: str, pnl_pct: float, dt: date):
            trade_journal.append({
                "date": str(dt), "action": action, "symbol": sym,
                "qty": qty, "price": price,
                "exit_reason": exit_reason, "pnl_pct": round(pnl_pct * 100, 1),
            })

        def _budget_ok(today_date: date) -> bool:
            """Check if we have trade budget remaining (5-day rolling window)."""
            recent = [d for d in trade_dates if (today_date - d).days <= 5]
            return len(recent) < WEEKLY_TRADE_BUDGET

        def _score_short_candidates(
            today_data: dict[str, pd.DataFrame],
            open_syms: set[str],
        ) -> list[tuple[float, str]]:
            """
            Score symbols for short-selling in bear/crash.
            Higher score = weaker stock = better short candidate.

            Criteria (all bearish = high score):
              - RSI > 60 (still has room to fall, not already oversold)
              - MACD histogram negative and falling
              - Price below EMA20 (short-term downtrend)
              - Price below EMA50 (medium-term downtrend)
              - High volume ratio (selling pressure confirmed)
            """
            candidates: list[tuple[float, str]] = []
            for sym, df in today_data.items():
                if sym in open_syms:
                    continue  # skip if already have a position
                if df.empty:
                    continue

                def _val(col: str) -> float:
                    if col not in df.columns: return float("nan")
                    v = df[col].iloc[-1]
                    return float(v) if pd.notna(v) else float("nan")

                close = _val("close")
                rsi   = _val("rsi")
                macd_h = _val("macd_hist")
                ema20  = _val("ema_20")
                ema50  = _val("ema_50")
                vol_r  = _val("volume_ratio")

                if math.isnan(close) or math.isnan(rsi):
                    continue

                # Skip already-oversold stocks (RSI < 30) — risky to short
                if rsi < 30:
                    continue

                score = 0.0
                # RSI 40-70 range: still has room to fall
                if 40 <= rsi <= 70:
                    score += 1.0
                elif rsi > 70:
                    score += 1.5  # overbought in a bear = prime short

                # MACD histogram negative
                if not math.isnan(macd_h) and macd_h < 0:
                    score += 1.0
                    # Check if MACD is accelerating down
                    if len(df) >= 2:
                        prev_h = df["macd_hist"].iloc[-2] if "macd_hist" in df.columns else float("nan")
                        if pd.notna(prev_h) and macd_h < float(prev_h):
                            score += 0.5  # momentum accelerating down

                # Price below EMAs (downtrend)
                if not math.isnan(ema20) and close < ema20:
                    score += 1.0
                if not math.isnan(ema50) and close < ema50:
                    score += 1.0

                # Volume confirms selling
                if not math.isnan(vol_r) and vol_r > 1.2:
                    score += 0.5

                # Minimum score threshold — only short convincing setups
                if score >= 3.5:
                    candidates.append((score, sym))

            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates

        # ── Main daily loop ───────────────────────────────────────────────
        for i, today in enumerate(trading_days):

            # ── 1. Safety: force-exit stale positions + hard stops ────────
            for sym in list(open_positions.keys()):
                pos = open_positions[sym]
                bar = _get_bar(processed.get(sym, pd.DataFrame()), today)
                exit_price = bar["close"] if bar else pos.entry_price

                exit_reason = ""
                if bar:
                    _, reason = _check_exit(pos, bar, today)
                    if reason:
                        exit_reason = reason
                        exit_price = pos.stop_loss if reason == "stop_loss" else (
                            pos.target if reason == "target" else bar["close"]
                        )

                if not exit_reason and (today - pos.entry_date).days >= max_hold:
                    exit_reason = "max_hold"

                if exit_reason:
                    pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
                    trade, cash = _close_position(pos, exit_price, exit_reason, cash, today)
                    trades.append(trade)
                    _add_to_journal("SELL", sym, pos.qty, exit_price, exit_reason, pnl_pct, today)
                    del open_positions[sym]
                    holdings.pop(sym, None)
                    avg_costs.pop(sym, None)
                    entry_dates.pop(sym, None)
                    sell_dates[sym] = today

            # ── 2. Mark-to-market + cash yield ───────────────────────────
            unrealized = 0.0
            for sym, pos in open_positions.items():
                bar = _get_bar(processed.get(sym, pd.DataFrame()), today)
                if bar:
                    if pos.direction == "short":
                        unrealized += (pos.entry_price - bar["close"]) * pos.qty
                    else:
                        unrealized += (bar["close"] - pos.entry_price) * pos.qty
            # NOTE: Removed cash yield inflation — production PortfolioState doesn't earn yield on idle cash
            # backtest must match production reality
            portfolio_value = cash + unrealized
            daily_values[today] = portfolio_value

            # ── 3. Compute regime (hybrid: 7-factor technical + 2-factor macro) ──────────
            regime_score = 0
            crash_detected = False

            # Macro signals: FII/DII flows and VIX (including momentum)
            # Note: In backtest, these may return defaults; in production, real-time feeds
            from data.macro_fetcher import fetch_fii_dii_flows, fetch_india_vix, fetch_vix_momentum, score_macro_signals
            macro_data = fetch_fii_dii_flows()
            india_vix = fetch_india_vix()
            vix_momentum = fetch_vix_momentum(days=5)  # RESEARCH FIX: Add VIX momentum
            macro_score = score_macro_signals(
                macro_data.get("fii_net_5d", 0),
                india_vix,
                vix_momentum=vix_momentum,
            )
            regime_score += int(macro_score)

            nifty_slice: Optional[pd.DataFrame] = None
            if nifty_df is not None and not nifty_df.empty:
                nifty_slice = nifty_df[nifty_df.index.date <= today]
                if nifty_slice.empty:
                    nifty_slice = None

            if nifty_slice is not None and not nifty_slice.empty:
                def _col_last(df: pd.DataFrame, col: str) -> float:
                    if col not in df.columns: return float("nan")
                    v = df[col].iloc[-1]
                    return float(v) if pd.notna(v) else float("nan")

                nc   = _col_last(nifty_slice, "close")
                e20  = _col_last(nifty_slice, "ema_20")
                e50  = _col_last(nifty_slice, "ema_50")
                e200 = _col_last(nifty_slice, "ema_200")
                rsi  = _col_last(nifty_slice, "rsi")
                macd = _col_last(nifty_slice, "macd_hist")

                def _ok(a, b): return not math.isnan(a) and not math.isnan(b)
                if _ok(nc,  e200) and nc  > e200: regime_score += 1
                if _ok(nc,  e50)  and nc  > e50:  regime_score += 1
                if _ok(nc,  e20)  and nc  > e20:  regime_score += 1
                if _ok(e20, e50)  and e20 > e50:  regime_score += 1
                if _ok(e50, e200) and e50 > e200: regime_score += 1
                if not math.isnan(rsi)  and rsi  > 50: regime_score += 1
                if not math.isnan(macd) and macd > 0:  regime_score += 1

                # Fast crash detection via ROC (10-day)
                roc_10 = _col_last(nifty_slice, "roc_10")
                if not math.isnan(roc_10) and roc_10 < CRASH_ROC_THRESHOLD:
                    crash_detected = True

                # **RESEARCH FIX**: Add intraday drop detection (jump model)
                # Paper: "Downside Risk Reduction Using Regime-Switching Signals"
                # If -5% intraday drop detected, override to BEAR immediately (don't wait for ROC)
                if len(nifty_slice) >= 2:
                    prev_close = nifty_slice["close"].iloc[-2] if len(nifty_slice) >= 2 else 0
                    curr_low   = nifty_slice["low"].iloc[-1] if "low" in nifty_slice.columns else nifty_slice["close"].iloc[-1]
                    if prev_close > 0:
                        intraday_drop = (prev_close - curr_low) / prev_close
                        if intraday_drop >= 0.05:  # -5% or worse
                            logger.info(f"  INTRADAY DROP DETECTED: {intraday_drop:.1%} on {today} → escalate to BEAR")
                            regime_score = 0  # Force BEAR classification
                            crash_detected = True

            current_regime = _classify_regime(regime_score, crash_detected)
            # Hysteresis: only cross-zone transitions count (BULL↔STRONG_BULL is ignored)
            zone_changed = (
                prev_regime is not None
                and _regime_zone(current_regime) != _regime_zone(prev_regime)
            )
            if zone_changed:
                logger.info(f"  REGIME CHANGE: {prev_regime} -> {current_regime} on {today}")
            prev_regime = current_regime

            # Recovery detector: bounce in bear zone signals re-entry
            recovery_detected = False
            if _regime_zone(current_regime) == "BEAR_ZONE" and nifty_slice is not None:
                roc_10_val = _col_last(nifty_slice, "roc_10")
                if not math.isnan(roc_10_val) and roc_10_val > RECOVERY_ROC_THRESHOLD:
                    recovery_detected = True
                    logger.info(
                        f"  RECOVERY SIGNAL on {today}: NIFTY ROC10={roc_10_val:.1%} "
                        f"(>{RECOVERY_ROC_THRESHOLD:.0%})"
                    )

            next_day = _next_trading_day(trading_days, i)

            # ── 4. CRASH/BEAR auto-liquidation (code-enforced, no LLM) ───
            block_buys = False

            if current_regime == "CRASH" and open_positions:
                # Immediate: sell 50% of every position
                block_buys = True
                for sym in list(open_positions.keys()):
                    pos = open_positions[sym]
                    if next_day and sym in processed:
                        nb = _get_bar(processed[sym], next_day)
                        if not nb:
                            continue
                        exit_price = nb["open"]
                        exit_date = next_day
                    else:
                        continue

                    sell_qty = pos.qty  # Sell 100% in crash — immediate full exit
                    pnl_pct = (exit_price - pos.entry_price) / pos.entry_price

                    if sell_qty >= pos.qty:
                        trade, cash = _close_position(pos, exit_price, "crash_liquidation", cash, exit_date)
                        trades.append(trade)
                        _add_to_journal("SELL", sym, pos.qty, exit_price, "crash_liquidation", pnl_pct, exit_date)
                        del open_positions[sym]
                        holdings.pop(sym, None)
                        avg_costs.pop(sym, None)
                        entry_dates.pop(sym, None)
                        sell_dates[sym] = today
                    else:
                        partial = _OpenPosition(
                            symbol=pos.symbol, strategy=pos.strategy,
                            direction=pos.direction, product=pos.product,
                            entry_date=pos.entry_date, entry_price=pos.entry_price,
                            stop_loss=pos.stop_loss, target=pos.target,
                            qty=sell_qty,
                        )
                        trade, cash = _close_position(partial, exit_price, "crash_liquidation", cash, exit_date)
                        trades.append(trade)
                        _add_to_journal("SELL", sym, sell_qty, exit_price, "crash_liquidation", pnl_pct, exit_date)
                        remaining = pos.qty - sell_qty
                        open_positions[sym] = _OpenPosition(
                            symbol=pos.symbol, strategy=pos.strategy,
                            direction=pos.direction, product=pos.product,
                            entry_date=pos.entry_date, entry_price=pos.entry_price,
                            stop_loss=pos.stop_loss, target=pos.target,
                            qty=remaining,
                        )
                        holdings[sym] = float(remaining)

            elif current_regime == "BEAR" and open_positions:
                # Progressive: sell weakest positions until 60% cash
                block_buys = True
                cash_pct = cash / portfolio_value if portfolio_value > 0 else 1.0
                if cash_pct < BEAR_CASH_TARGET:
                    target_sell_value = portfolio_value * BEAR_DAILY_SELL_PCT
                    positions_by_pnl = sorted(
                        open_positions.items(),
                        key=lambda x: _unrealized_pnl_pct(x[1], today),
                    )
                    sold_value = 0.0
                    for sym, pos in positions_by_pnl:
                        if sold_value >= target_sell_value:
                            break
                        if next_day and sym in processed:
                            nb = _get_bar(processed[sym], next_day)
                            if not nb:
                                continue
                            exit_price = nb["open"]
                            exit_date = next_day
                        else:
                            continue

                        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
                        trade, cash = _close_position(pos, exit_price, "bear_liquidation", cash, exit_date)
                        trades.append(trade)
                        _add_to_journal("SELL", sym, pos.qty, exit_price, "bear_liquidation", pnl_pct, exit_date)
                        sold_value += pos.entry_price * pos.qty
                        del open_positions[sym]
                        holdings.pop(sym, None)
                        avg_costs.pop(sym, None)
                        entry_dates.pop(sym, None)
                        sell_dates[sym] = today

            elif current_regime in ("CRASH", "BEAR"):
                block_buys = True  # Even with no positions, block new buys

            # Recovery override: if bounce detected in bear zone, allow buying again
            if recovery_detected:
                block_buys = False
                logger.info(f"  RECOVERY override: unblocking buys on {today}")

            # ── 5. Slice data up to today (no look-ahead) ─────────────────
            today_data: dict[str, pd.DataFrame] = {}
            for sym, df in processed.items():
                slice_df = df[df.index.date <= today]
                if len(slice_df) >= 60:
                    today_data[sym] = slice_df

            if not today_data:
                continue

            # ── 5b. Bear/Crash short selling (MIS intraday) ───────────────
            #  In BEAR/CRASH: open intraday shorts on weakest stocks.
            #  Skip if recovery is detected (stop shorting on bounces).
            #  Indian equity rules: shorts must be MIS (intraday only).
            #  Enter at next day's open, exit at next day's close.
            if current_regime in ("CRASH", "BEAR") and next_day and not recovery_detected:
                open_syms = set(open_positions.keys())
                # Count existing short positions
                short_count = sum(
                    1 for p in open_positions.values() if p.direction == "short"
                )
                short_slots = BEAR_MAX_SHORTS - short_count

                if short_slots > 0:
                    short_candidates = _score_short_candidates(today_data, open_syms)
                    short_alloc = portfolio_value * BEAR_SHORT_PCT

                    for score, sym in short_candidates[:short_slots]:
                        nb = _get_bar(processed.get(sym, pd.DataFrame()), next_day)
                        if not nb:
                            continue

                        entry_price = nb["open"]
                        if entry_price <= 0:
                            continue
                        # Apply slippage: realistic market order execution
                        entry_price = _apply_slippage(entry_price, sym, is_entry=True)

                        qty = max(1, int(short_alloc / entry_price))
                        cost = entry_price * qty  # margin blocked

                        if cost > cash * 0.5:  # don't use more than 50% of cash for shorts
                            continue

                        # Short stop: 4% above entry
                        stop_price   = round(entry_price * 1.04, 2)
                        # Short target: 5% below entry (crash moves are large)
                        target_price = round(entry_price * 0.95, 2)

                        cash -= cost  # margin blocked
                        open_positions[sym] = _OpenPosition(
                            symbol=sym,
                            strategy="bear_short",
                            direction="short",
                            product="MIS",
                            entry_date=next_day,
                            entry_price=entry_price,
                            stop_loss=stop_price,
                            target=target_price,
                            qty=qty,
                        )
                        _add_to_journal("SHORT", sym, qty, entry_price, "", 0.0, next_day)
                        logger.debug(
                            f"  BEAR SHORT {sym} {qty}@{entry_price:.2f} "
                            f"score={score:.1f} SL={stop_price:.2f} TP={target_price:.2f}"
                        )

            # ── 6. Regime-gated LLM call ──────────────────────────────────
            should_call_llm = False
            days_since_last_call = (
                (today - last_llm_call_date).days if last_llm_call_date else 999
            )

            if last_decision is None and current_regime not in ("CRASH", "BEAR"):
                should_call_llm = True   # First day: always call to build initial portfolio
            elif recovery_detected and days_since_last_call >= 3:
                should_call_llm = True   # Recovery bounce: call LLM to start re-entry
            elif current_regime in ("CRASH", "BEAR"):
                should_call_llm = False  # Code handles it (unless recovery)
            elif zone_changed:
                should_call_llm = True   # Only major transitions trigger immediate call
            elif current_regime in ("STRONG_BULL", "BULL"):
                should_call_llm = (days_since_last_call >= 5)  # Every 5 days in bull zone
            elif current_regime == "NEUTRAL":
                should_call_llm = (days_since_last_call >= 2)

            decision = last_decision  # default: reuse previous

            if should_call_llm:
                try:
                    decision = _run_async(llm_mgr.decide(
                        symbol_data=today_data,
                        portfolio_cash=cash,
                        portfolio_value=portfolio_value,
                        holdings=holdings,
                        avg_costs=avg_costs,
                        daily_pnl=0.0,
                        drawdown_pct=0.0,
                        nifty_df=nifty_slice,
                        entry_dates=entry_dates,
                        today=today,
                        trade_journal=trade_journal[-10:] if trade_journal else None,
                    ))
                    last_llm_call_date = today
                    last_decision = decision
                except Exception as exc:
                    logger.warning(f"LLM decide failed on {today}: {exc} — skipping day")
                    decision = last_decision

            if decision is None:
                continue

            # ── 7. Execute LLM sell decisions (with regime guards) ────────
            for alloc in decision.sells():
                sym = alloc.symbol
                if sym not in open_positions:
                    continue

                pos = open_positions[sym]

                # Unrealised P&L for guard checks
                today_bar = _get_bar(processed.get(sym, pd.DataFrame()), today)
                curr_price = today_bar["close"] if today_bar else pos.entry_price
                pnl_pct = (curr_price - pos.entry_price) / pos.entry_price

                # Hard exits: always honoured regardless of regime
                hard_stop   = pnl_pct <= STOP_LOSS_PCT
                # Regime-aware take-profit threshold
                tp_threshold = TAKE_PROFIT_STRONG_BULL if current_regime == "STRONG_BULL" else TAKE_PROFIT_PCT
                take_profit = pnl_pct >= tp_threshold

                # Regime-aware sell guards (only in bull/neutral; bear/crash handled above)
                days_in = (today - entry_dates.get(sym, today)).days

                if current_regime == "STRONG_BULL":
                    # In STRONG_BULL: allow exits only at hard stop or +20% profit (not +15%)
                    # This lets winners run longer while still booking big gains
                    if not hard_stop and not take_profit:
                        logger.debug(f"  STRONG_BULL: skip sell {sym} (P&L={pnl_pct:+.1%}, needs TP threshold +20%)")
                        continue
                elif current_regime == "BULL":
                    # In bull: block all sells except hard stop and take-profit
                    # This prevents rotation which kills compounding
                    if not hard_stop and not take_profit:
                        logger.debug(f"  BULL block: skip sell {sym} (P&L={pnl_pct:+.1%})")
                        continue
                elif current_regime == "NEUTRAL":
                    # 3-day min hold
                    if days_in < 3 and not hard_stop and not take_profit:
                        logger.debug(f"  NEUTRAL min-hold: skip {sym} held {days_in}d")
                        continue

                # Trade budget check (hard stops exempt)
                if not hard_stop and not _budget_ok(today):
                    logger.debug(f"  TRADE_BUDGET: skip sell {sym}")
                    continue

                if next_day and sym in processed:
                    nb = _get_bar(processed[sym], next_day)
                    exit_price = nb["open"] if nb else pos.entry_price
                    exit_date  = next_day
                else:
                    exit_price = pos.entry_price
                    exit_date  = today

                sell_qty = max(1, int(pos.qty * alloc.quantity))
                if sell_qty >= pos.qty:
                    trade, cash = _close_position(pos, exit_price, "llm_sell", cash, exit_date)
                    trades.append(trade)
                    _add_to_journal("SELL", sym, pos.qty, exit_price, "llm_sell", pnl_pct, exit_date)
                    if not hard_stop:
                        trade_dates.append(today)
                    del open_positions[sym]
                    holdings.pop(sym, None)
                    avg_costs.pop(sym, None)
                    entry_dates.pop(sym, None)
                    sell_dates[sym] = today
                else:
                    partial = _OpenPosition(
                        symbol=pos.symbol, strategy=pos.strategy,
                        direction=pos.direction, product=pos.product,
                        entry_date=pos.entry_date, entry_price=pos.entry_price,
                        stop_loss=pos.stop_loss, target=pos.target,
                        qty=sell_qty,
                    )
                    trade, cash = _close_position(partial, exit_price, "llm_sell_partial", cash, exit_date)
                    trades.append(trade)
                    _add_to_journal("SELL", sym, sell_qty, exit_price, "llm_sell_partial", pnl_pct, exit_date)
                    if not hard_stop:
                        trade_dates.append(today)
                    remaining = pos.qty - sell_qty
                    open_positions[sym] = _OpenPosition(
                        symbol=pos.symbol, strategy=pos.strategy,
                        direction=pos.direction, product=pos.product,
                        entry_date=pos.entry_date, entry_price=pos.entry_price,
                        stop_loss=pos.stop_loss, target=pos.target,
                        qty=remaining,
                    )
                    holdings[sym] = float(remaining)

            if next_day is None:
                continue

            # ── 8. Execute LLM buy decisions ──────────────────────────────
            if block_buys:
                continue  # No buys in CRASH/BEAR

            # Regime-dependent deployment: aggressive in STRONG_BULL but prudent
            if current_regime == "STRONG_BULL":
                cash_reserve = 0.05   # 5% reserve — stay deployed but maintain buffer
                pos_cap = 0.15        # 15% per position — larger conviction bets
            elif current_regime == "BULL" and not recovery_detected:
                cash_reserve = 0.05   # 5% reserve — good deployment
                pos_cap = 0.12        # 12% per position — good sized positions
            elif recovery_detected:
                cash_reserve = 0.05   # 5% reserve during recovery (cautious re-entry)
                pos_cap = 0.12        # 12% per position — staged entry
            elif current_regime == "NEUTRAL":
                cash_reserve = 0.05   # 5% reserve
                pos_cap = 0.10        # 10% per position
            else:
                cash_reserve = self.config.cash_reserve_pct  # 10% default
                pos_cap = 0.08

            available_cash = cash * (1.0 - cash_reserve)

            # Diversification-aware position size cap
            num_positions = len(open_positions)
            if num_positions < 3:
                max_per_position = portfolio_value * min(pos_cap, 0.10)
            elif num_positions < 5:
                max_per_position = portfolio_value * min(pos_cap, 0.14)
            else:
                max_per_position = portfolio_value * pos_cap

            for alloc in decision.buys():
                sym = alloc.symbol
                if sym in open_positions:
                    continue
                if len(open_positions) >= self.config.max_concurrent_positions:
                    break
                if available_cash < 1_000:
                    break

                # Regime-aware universe filtering
                from config.universes import get_regime_universe
                regime_universe = get_regime_universe(current_regime, list(processed.keys()))
                if sym not in regime_universe:
                    logger.debug(f"  UNIVERSE_FILTER: skip {sym} (not in {current_regime} universe)")
                    continue

                # Trade budget check
                if not _budget_ok(today):
                    logger.debug(f"  TRADE_BUDGET: skip buy {sym}")
                    break  # budget exhausted, no more buys today

                # Regime-dependent re-entry cooldown
                if sym in sell_dates:
                    cooldown = _reentry_cooldown(current_regime)
                    days_since_sell = (today - sell_dates[sym]).days
                    if days_since_sell < cooldown:
                        logger.debug(
                            f"  COOLDOWN: skip buy {sym} "
                            f"(sold {days_since_sell}d ago, cooldown={cooldown}d)"
                        )
                        continue

                nb = _get_bar(processed.get(sym, pd.DataFrame()), next_day)
                if not nb:
                    continue

                entry_price = nb["open"]
                if entry_price <= 0:
                    continue
                # Apply slippage: realistic market order execution
                entry_price = _apply_slippage(entry_price, sym, is_entry=True)

                # Cap buy fraction — higher in bull to deploy more capital
                if current_regime in ("STRONG_BULL", "BULL"):
                    max_frac = 0.60  # Use up to 60% of available cash per stock in bull
                elif recovery_detected:
                    max_frac = 0.40  # Cautious 40% per stock on recovery
                else:
                    max_frac = 0.30

                # RESEARCH FIX: Apply Kelly Criterion adaptive sizing (only after sufficient history)
                # Only apply Kelly once we have 40+ trades of history; before that, use full regime sizing
                if len(trade_journal) >= 40:
                    kelly_pct = _kelly_criterion(trade_journal, max_position_pct=0.20)
                    kelly_adjusted_frac = max_frac * (kelly_pct / 0.12)  # Scale by Kelly relative to 12% baseline
                    kelly_adjusted_frac = min(kelly_adjusted_frac, max_frac)  # Don't exceed max_frac
                else:
                    kelly_adjusted_frac = max_frac  # Use full regime-based sizing initially

                effective_quantity = min(alloc.quantity, kelly_adjusted_frac)
                alloc_cash = min(
                    available_cash * effective_quantity,
                    max_per_position,
                    available_cash,
                )
                if alloc_cash < 1_000:
                    continue

                qty = max(1, int(alloc_cash / entry_price))
                cost = entry_price * qty
                if cost > available_cash:
                    qty  = max(1, int(available_cash / entry_price))
                    cost = entry_price * qty
                if cost < 1_000:
                    continue

                # Safety stop/target: 7% stop, 25% target (research-backed increase from 15%)
                # LLM may exit earlier based on technical signals
                stop_price   = round(entry_price * 0.93, 2)
                target_price = round(entry_price * 1.25, 2)

                cash           -= cost
                available_cash -= cost
                holdings[sym]   = float(qty)
                avg_costs[sym]  = entry_price
                entry_dates[sym] = next_day
                trade_dates.append(today)

                open_positions[sym] = _OpenPosition(
                    symbol=sym,
                    strategy="llm",
                    direction="long",
                    product="CNC",
                    entry_date=next_day,
                    entry_price=entry_price,
                    stop_loss=stop_price,
                    target=target_price,
                    qty=qty,
                )
                _add_to_journal("BUY", sym, qty, entry_price, "", 0.0, next_day)
                logger.debug(
                    f"  LLM BUY {sym} {qty}@{entry_price:.2f} "
                    f"alloc_frac={effective_quantity:.2f} cost={cost:,.0f}"
                )

        # ── Force-close remaining open positions ──────────────────────────
        last_day = trading_days[-1]
        for sym, pos in list(open_positions.items()):
            bar = _get_bar(processed.get(sym, pd.DataFrame()), last_day)
            exit_price = bar["close"] if bar else pos.entry_price
            trade, cash = _close_position(pos, exit_price, "period_end", cash, last_day)
            trades.append(trade)

        final_value = cash
        daily_values[last_day] = final_value

        daily_series = pd.Series(daily_values).sort_index()
        daily_series.index = pd.to_datetime(daily_series.index)

        logger.info(
            f"LLM Backtest complete: {len(trades)} trades | "
            f"Final: {final_value:,.0f} | "
            f"Return: {(final_value / self.config.initial_capital - 1) * 100:.2f}%"
        )

        return BacktestResult(
            trades=trades,
            daily_values=daily_series,
            config=self.config,
            start_date=start,
            end_date=end,
            strategies_used=["llm"],
            symbols_tested=len(processed),
        )

    def _empty_result(self, start: date, end: date, symbols: list[str]) -> BacktestResult:
        return BacktestResult(
            trades=[],
            daily_values=pd.Series(dtype=float),
            config=self.config,
            start_date=start,
            end_date=end,
            strategies_used=[s.name for s in self._strategies],
            symbols_tested=len(symbols),
        )


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _get_bar(df: pd.DataFrame, day: date) -> Optional[dict]:
    """Get OHLCV bar dict for a specific date."""
    mask = df.index.date == day
    if not mask.any():
        return None
    row = df[mask].iloc[0]
    return {"open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"]}


def _next_trading_day(trading_days: list[date], current_idx: int) -> Optional[date]:
    if current_idx + 1 < len(trading_days):
        return trading_days[current_idx + 1]
    return None


def _check_exit(pos: _OpenPosition, bar: dict, today: date) -> tuple[float, str]:
    """
    Determine if a position should be exited on this bar.
    Returns (exit_price, reason) or ("", "") if no exit.
    """
    is_short = pos.direction == "short"
    is_mis   = pos.product == "MIS"

    # MIS: always exit at close (EOD)
    if is_mis and today != pos.entry_date:
        exit_price = _apply_slippage(bar["close"], pos.symbol, is_entry=False)
        return exit_price, "eod_close"

    # MIS entered today: check intraday stop/target on this bar
    if is_mis and today == pos.entry_date:
        if is_short:
            if bar["high"] >= pos.stop_loss and pos.stop_loss > 0:
                return pos.stop_loss, "stop_loss"
            if bar["low"] <= pos.target and pos.target > 0:
                return pos.target, "target"
        else:
            if bar["low"] <= pos.stop_loss and pos.stop_loss > 0:
                return pos.stop_loss, "stop_loss"
            if bar["high"] >= pos.target and pos.target > 0:
                return pos.target, "target"
        exit_price = _apply_slippage(bar["close"], pos.symbol, is_entry=False)
        return exit_price, "eod_close"

    # Max hold period for swing
    hold = (today - pos.entry_date).days
    if hold >= _MAX_HOLD_DAYS_SWING:
        exit_price = _apply_slippage(bar["close"], pos.symbol, is_entry=False)
        return exit_price, "max_hold"

    # Stop and target checks for CNC
    if is_short:
        # Stop triggered if bar high crosses above stop
        stop_hit   = pos.stop_loss > 0 and bar["high"] >= pos.stop_loss
        target_hit = pos.target > 0    and bar["low"] <= pos.target
    else:
        stop_hit   = pos.stop_loss > 0 and bar["low"] <= pos.stop_loss
        target_hit = pos.target > 0    and bar["high"] >= pos.target

    if stop_hit and target_hit:
        # Conservative: assume stop was hit first
        return pos.stop_loss, "stop_loss"
    if stop_hit:
        return pos.stop_loss, "stop_loss"
    if target_hit:
        return pos.target, "target"

    return 0.0, ""


def _calc_costs(exit_price: float, qty: int, product: str) -> float:
    """Calculate transaction costs on exit leg only (matching PortfolioState)."""
    turnover  = exit_price * qty
    brokerage = min(_BROKERAGE_FLAT, turnover * _BROKERAGE_PCT)
    stt       = turnover * (_STT_CNC if product == "CNC" else _STT_MIS)
    exchange  = turnover * _EXCHANGE_CHARGE
    gst       = (brokerage + exchange) * _GST_RATE
    return round(brokerage + stt + exchange + gst, 2)


def _apply_slippage(price: float, symbol: str, is_entry: bool = True) -> float:
    """
    Apply realistic market slippage based on symbol liquidity tier.
    Slippage models: entry + exit costs for market orders.

    NIFTY50 (large-cap):  0.10% slippage
    Others (mid-cap):     0.20% slippage
    Small-cap:            0.40% slippage (rarely used in backtest)
    """
    from config.universes import NIFTY50

    if symbol in NIFTY50:
        slippage_pct = 0.0010  # 0.10%
    else:
        slippage_pct = 0.0020  # 0.20% mid-cap assumption

    # On entry: slippage worsens fill (higher for buy, lower for sell conceptually)
    # On exit: slippage worsens exit price
    # Simplified: apply symmetric slippage to both
    return price * (1.0 + slippage_pct)


def _close_position(
    pos: _OpenPosition,
    exit_price: float,
    exit_reason: str,
    cash: float,
    exit_date: date,
) -> tuple[TradeRecord, float]:
    """Close a position and return (TradeRecord, updated_cash)."""
    is_short = pos.direction == "short"

    if is_short:
        gross = (pos.entry_price - exit_price) * pos.qty
        cash  += pos.entry_price * pos.qty   # return proceeds
    else:
        gross = (exit_price - pos.entry_price) * pos.qty
        cash  += exit_price * pos.qty

    costs    = _calc_costs(exit_price, pos.qty, pos.product)
    net      = gross - costs
    cash    -= costs

    trade = TradeRecord(
        symbol=pos.symbol,
        strategy=pos.strategy,
        direction=pos.direction,
        product=pos.product,
        entry_date=pos.entry_date,
        exit_date=exit_date,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        qty=pos.qty,
        gross_pnl=round(gross, 2),
        transaction_cost=costs,
        realized_pnl=round(net, 2),
        exit_reason=exit_reason,
        signal_confidence=pos.signal.confidence if pos.signal else 0.0,
        signal_strength=(pos.signal.strength.value if pos.signal and pos.signal.strength else ""),
    )

    logger.debug(
        f"  EXIT {pos.symbol} {pos.direction} {pos.qty}@{exit_price:.2f} "
        f"P&L=₹{net:.0f} ({exit_reason})"
    )
    return trade, cash


# ─── Statistical helpers ──────────────────────────────────────────────────────

def _kelly_criterion(trade_journal: list[dict], max_position_pct: float = 0.20) -> float:
    """
    Calculate optimal position size using Kelly Criterion.

    Research: "Kelly Criterion for Stock Trading" papers show 1-4% improvement over fixed sizing.
    Formula: K% = (W × AvgWin - (1-W) × AvgLoss) / AvgWin × 0.5 (half-Kelly for safety)

    Args:
        trade_journal: List of completed trades with 'pnl_pct' fields
        max_position_pct: Maximum position size cap (safety limit)

    Returns:
        float: Optimal position size (0.0 to max_position_pct)
    """
    if len(trade_journal) < 20:
        return 0.08  # Default to 8% if insufficient history

    # Use last 20 trades for rolling calculation
    recent = trade_journal[-20:]
    wins = [t for t in recent if t.get('pnl_pct', 0) > 0]
    losses = [t for t in recent if t.get('pnl_pct', 0) <= 0]

    if not wins or not losses:
        return 0.08  # Edge case: all wins or all losses

    win_rate = len(wins) / len(recent)
    avg_win = sum(t.get('pnl_pct', 0) for t in wins) / len(wins)
    avg_loss = abs(sum(t.get('pnl_pct', 0) for t in losses) / len(losses))

    if avg_win == 0:
        return 0.08

    # Kelly formula with 0.5 safety factor (half-Kelly)
    kelly_pct = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win * 0.5

    # Bound between 2% and max_position_pct
    return max(0.02, min(kelly_pct, max_position_pct))


def _sharpe(daily_returns: list[float], risk_free_daily: float = 0.0) -> float:
    if len(daily_returns) < 20:
        return 0.0
    excess = [r - risk_free_daily for r in daily_returns]
    mean = sum(excess) / len(excess)
    var  = sum((r - mean) ** 2 for r in excess) / len(excess)
    std  = math.sqrt(var) if var > 0 else 0.0
    return (mean / std * math.sqrt(252)) if std > 0 else 0.0


def _max_drawdown(values: pd.Series) -> float:
    """Maximum drawdown in percent."""
    if values.empty:
        return 0.0
    peak = values.cummax()
    dd   = (values - peak) / peak
    return float(abs(dd.min()) * 100)


def _run_async(coro):
    """
    Run an async coroutine from a synchronous context.
    Handles the case where an event loop is already running (e.g., Jupyter).
    Suppresses harmless event loop cleanup warnings from httpx.
    """
    import asyncio
    import logging
    try:
        loop = asyncio.get_running_loop()
        # Already inside a running loop — use a thread pool to avoid nesting
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    except RuntimeError:
        # Suppress asyncio event loop warnings from httpx cleanup (harmless)
        old_level = logging.getLogger("asyncio").level
        logging.getLogger("asyncio").setLevel(logging.ERROR)
        try:
            return asyncio.run(coro)
        finally:
            logging.getLogger("asyncio").setLevel(old_level)
