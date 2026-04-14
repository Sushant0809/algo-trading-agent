# System Architecture

## Overview

The agent is a Python asyncio application that runs continuously during NSE market hours (9:15am–3:30pm IST). It combines traditional quantitative strategies with LLM-powered agents for sentiment analysis, regime detection, swing portfolio management, and risk review.

```
┌────────────────────────────────────────────────────────────────┐
│                   ORCHESTRATOR (LangGraph)                      │
│         Morning setup → Intraday loop → Swing cycle             │
└──────┬──────────────┬──────────────┬──────────────┬────────────┘
       │              │              │              │
  ┌────▼────┐   ┌─────▼────┐  ┌─────▼─────┐  ┌────▼──────────┐
  │ Market  │   │Sentiment │  │ Strategy  │  │  LLM Portfolio │
  │ Analyst │   │  Agent   │  │ Selector  │  │   Manager      │
  │(no LLM) │   │ (Claude) │  │ (Claude)  │  │  (Claude)      │
  └────┬────┘   └─────┬────┘  └───────────┘  └────┬──────────┘
       │              │                             │
       └──────────────┴─────────────────────────────┘
                      │ signals → SignalCombiner → PortfolioAllocator
             ┌────────▼────────┐
             │   Risk Agent    │  ← hard rules first, Claude for borderline
             └────────┬────────┘
                      │ approved signals
             ┌────────▼───────────────────┐
             │   Execution Agent          │
             │ paper_simulator OR kite    │
             └────────┬───────────────────┘
                      │
             ┌────────▼────────┐
             │ Portfolio Agent │  ← 1min heartbeat, manages exits
             └─────────────────┘
```

---

## Layers

### 1. Data Layer (`data/`)

| Module | Responsibility |
|--------|---------------|
| `kite_client.py` | Singleton KiteConnect instance, initialized once at startup |
| `market_data.py` | OHLCV bars — tries KiteConnect, falls back to yfinance |
| `yfinance_historical.py` | Extended yfinance fetcher with `.NS` suffix — used by backtester |
| `news_fetcher.py` | NSE corporate announcements via NSE India public API |
| `macro_fetcher.py` | FII/DII flows (NSE API), India VIX (`^INDIAVIX`), RBI repo rate |
| `cache.py` | Parquet-based bar cache to avoid re-fetching |

**Data flow for market data:**
1. Try KiteConnect `historical_data()` API
2. If permission denied / unavailable → fall back to `yfinance` with `.NS` suffix
3. Cache result to parquet to speed up next request

**Macro signals (`macro_fetcher.py`):**
- `fetch_fii_dii_flows()` — NSE public API, no key required; returns 5-day net inflow in ₹ crore
- `fetch_india_vix()` — yfinance `^INDIAVIX`; returns current VIX level
- `get_rbi_rate()` — static lookup (updated manually ~4× per year)
- `score_macro_signals()` — converts FII + VIX into 0–2 point regime score

### 2. Signal Layer (`signals/`)

| Module | Responsibility |
|--------|---------------|
| `indicators.py` | pandas-ta wrappers — computes EMA, RSI, MACD, BB, ATR, ADX, VWAP |
| `signal_model.py` | Pydantic dataclasses: `Signal`, `ExitSignal`, `ApprovedSignal` |
| `signal_bus.py` | Three asyncio queues connecting agents |
| `signal_combiner.py` | Cross-validates sentiment signals against technical picture |

**Signal Bus queues:**
```
raw_queue:      strategy → portfolio_allocator → risk_agent
approved_queue: risk_agent → execution_agent
exit_queue:     portfolio_agent → execution_agent
```

**Signal Combiner** (`signal_combiner.py`):
- Only applies to sentiment-driven signals (not pure technical signals)
- Runs 5 technical checks (RSI range, MACD direction, EMA(20), volume, ADX)
- `combined_confidence = 0.6 × sentiment_confidence + 0.4 × tech_score`
- tech_score ≥ 0.6 → CONFIRM; ≥ 0.4 → DOWNGRADE; < 0.4 → REJECT

### 3. Strategy Layer (`strategies/`)

Seven strategies all inherit from `BaseStrategy`:

| Strategy | Market Condition | Product | Timeframe |
|----------|-----------------|---------|-----------|
| Momentum | Trending (ADX > 25) | MIS/CNC | 5min / Daily |
| Mean Reversion | Sideways, RSI < 30 | MIS/CNC | 15min / Daily |
| Breakout | Consolidation → break | MIS/CNC | 15min / Daily |
| Oversold Bounce | Dip in uptrend | MIS/CNC | Daily |
| Overbought Short | Rejection in downtrend | MIS only | Daily |
| Sentiment Driven | News catalyst ≥ 7/10 | MIS/CNC | Daily |
| LLM Strategy | Any (swing) | CNC | Daily |

Each strategy's `generate_signal(symbol, df, mode)` returns a `Signal` or `None`.

### 4. Agent Layer (`agents/`)

#### Market Analyst (no LLM)
- Runs every 5 minutes during market hours
- Fetches OHLCV → computes all indicators → runs all 7 strategies
- Publishes signals to `raw_queue`
- No Claude API calls — purely quantitative

#### Sentiment Agent (Claude)
- Runs once at 8:45am IST (pre-market)
- Fetches last 24h NSE corporate announcements for top 30 symbols
- Sends each symbol's news to Claude with a scoring prompt
- Claude returns score (-10 to +10) with reasoning
- High scores (≥ 7) → `SentimentDrivenStrategy` → `SignalCombiner` for tech cross-validation

#### Strategy Selector (Claude)
- Runs once at 9:00am IST (pre-market)
- Feeds Nifty 50 regime indicators (RSI, ADX, BB width, 5d/20d change, FII flows, India VIX) to Claude
- Claude returns: regime type + strategy weight allocation (sum = 1.0)
- Weights are pushed into `RiskAgent.strategy_weights` for position sizing

#### LLM Portfolio Manager (Claude)
- Runs in swing mode every 3–5 days in bull regimes; at every regime transition otherwise
- Receives full symbol universe with 5-bar OHLCV + all indicators + current holdings
- Returns allocation decision: per-symbol `{action, quantity}` fractions
- Backed by `BaseLLMPortfolioManager` (supports Claude, Groq backends)
- Decision log stored in `trade_journal` for Kelly Criterion rolling calculation

#### Risk Agent (hard rules + optional Claude)
- Listens on `raw_queue` continuously
- Applies hard rules (never overridden): duplicate check, position limits, sector exposure, price floor, timing cutoff
- For borderline signals (confidence < 0.7): optionally asks Claude for a second opinion
- Approved signals → `approved_queue`; rejected → logged with reason

#### Execution Agent
- Listens on `approved_queue` and `exit_queue`
- `PAPER_TRADING=true` → routes to `paper_simulator.py`
- `PAPER_TRADING=false` → routes to `kite_executor.py` (real orders)
- Places stop-loss orders immediately after entry

#### Portfolio Agent
- 60-second heartbeat monitoring all open positions
- Checks stop-loss, target, trailing stop for each position
- Publishes exit signals to `exit_queue`
- At 3:15pm IST: force-closes all MIS positions before Zerodha auto square-off

### 5. Risk Layer (`risk/`)

| Module | Responsibility |
|--------|---------------|
| `risk_manager.py` | Hard rules gate — evaluates every signal |
| `position_sizer.py` | Three sizing methods: fixed-fraction, volatility-ATR, half-Kelly |
| `portfolio_state.py` | Thread-safe state: positions, cash, P&L, drawdown |
| `portfolio_allocator.py` | Ranks competing signals; respects 10% cash floor, position limits |

**Portfolio Allocator** (`portfolio_allocator.py`):
- Drains raw queue and scores signals: `strategy_weight × confidence × strength_mult`
- Sorts by score, greedily allocates cash up to position cap
- Enforces 10% cash reserve (always kept for flexibility)
- Limits to `MAX_SIGNALS_SWING=3` or `MAX_SIGNALS_INTRA=5` new positions per cycle
- Deduplicates: no two signals for the same symbol

`PortfolioState` uses `asyncio.Lock` for all writes, ensuring no race conditions between the portfolio agent (reading/writing positions) and execution agent (opening/closing).

### 6. Regime Classification

Regime is computed fresh every trading day (and at intraday cycle start in swing mode):

**Technical signals (9 factors max):**
1. EMA stack: price vs EMA(20), EMA(50), EMA(200)
2. ROC 10-day: % change over 10 trading days
3. RSI: current level vs 50 midline
4. Volume ratio: current vs 20-day average
5. VIX level: `^INDIAVIX` absolute level

**Macro signals (0–2 added points):**
6. FII net flows: buying = +1 point; strong buying (>₹1000cr) = +0.5 more
7. India VIX: low (<15) = +1; high (>25) = −0.5
8. RBI rate: low (<6%) = +0.5

**Jump detection (override):**
- Single-day intraday drop ≥ −5% → immediately classify as BEAR regardless of score

| Score | Regime | Action |
|-------|--------|--------|
| ≥ 6 | STRONG_BULL | Max deployment, 2% cash, 15% per position, 30% TP |
| ≥ 4 | BULL | 10% cash, 12% per position, 25% TP |
| ≥ 2 | NEUTRAL | 20% cash, 10% per position, 25% TP |
| < 2 | BEAR | 80% cash target, 60%/day liquidation, NIFTY50 only |
| crash_detected | CRASH | 95%+ cash, immediate full liquidation |

### 7. Execution Layer (`execution/`)

| Module | Responsibility |
|--------|---------------|
| `paper_simulator.py` | Virtual execution: 0.05% slippage model, PAPER-XXXXXX order IDs |
| `kite_executor.py` | Real KiteConnect orders + SL-M stop placement |
| `fill_tracker.py` | KiteConnect WebSocket → update portfolio_state on fills |

### 8. Backtesting Layer (`backtesting/`)

| Module | Responsibility |
|--------|---------------|
| `strategy_backtester.py` | Full LLM swing-mode backtest engine (regime + LLM + Kelly + slippage) |
| `benchmark_comparison.py` | Runs multiple historical periods and compares against NIFTY50 |

**Backtest features:**
- Realistic slippage: 0.10% for large-cap (NIFTY50), 0.20% for mid/small-cap
- Regime-switching universe: NIFTY50 only in BEAR/CRASH; full 100+ stock universe in BULL
- Kelly Criterion adaptive position sizing (kicks in after 40 trades of history)
- Intraday jump detection for fast bear regime escalation
- Daily trade budget cap (configurable)
- Re-entry cooldown by regime: BEAR=999 days, BULL=7 days

Run backtest:
```bash
.venv/bin/python run_llm_backtest.py --short    # 3 periods, 15 symbols (~$0.36)
.venv/bin/python run_llm_backtest.py            # Full 5 periods, 30 symbols
```

### 9. Scheduling Layer (`scheduling/`)

| Module | Responsibility |
|--------|---------------|
| `market_calendar.py` | NSE trading hours, holidays 2024-2026, IST timezone |
| `intraday_scheduler.py` | APScheduler: 5min cron 9:15–3:15pm IST |
| `swing_scheduler.py` | APScheduler: pre-market 9:00am + EOD 4:00pm |

### 10. Auth Layer (`auth/`)

KiteConnect access tokens expire **daily at 6am IST**. The auth flow:

```
8:30am IST
     │
     ▼
load_cached_token()  →  exists & today's date?  →  YES → use it
                                │
                               NO
                                │
                                ▼
             Playwright launches Chromium (headless)
                                │
                    Fill user_id + password
                                │
                    Generate TOTP via pyotp
                                │
                    Fill TOTP input
                                │
                    Intercept redirect to http://127.0.0.1
                    (request_token captured from URL)
                                │
                    exchange_request_token() via KiteConnect API
                                │
                    Save access_token to cache + .env
```

### 11. Monitoring Layer (`monitoring/`)

| Module | Responsibility |
|--------|---------------|
| `logger.py` | structlog JSON logger → `logs/trading.log` |
| `audit_trail.py` | Append-only JSONL decision log → `logs/audit/` |
| `alerting.py` | Telegram alerts for fills, P&L, kill switches (optional) |
| `benchmark_tracker.py` | Tracks daily portfolio return vs NIFTY50; computes rolling alpha |

---

## LLM Agents — Prompt Structures

### Sentiment Agent
```
You are a sentiment analyst for Indian equity markets.
News items for {symbol}: {headlines}
Rate sentiment from -10 to +10. Return JSON: {score, confidence, reasoning}
```

### Strategy Selector
```
Market regime indicators: {nifty_rsi, adx, bb_width, 5d_change, vix, fii_net_5d, india_vix}
Available strategies: momentum, mean_reversion, breakout, oversold_bounce, overbought_short, sentiment_driven, llm_strategy
Return JSON: {regime, strategy_weights, risk_level, reasoning}
```

### LLM Portfolio Manager
```
You are an expert Indian equity portfolio manager trading NIFTY50 stocks.
Holdings: {symbol, qty, entry_price, unrealised_pnl_pct}
Available symbols with full indicator table...
Return JSON array: [{symbol, action_type, quantity}]
```

### Risk Agent (borderline signals only)
```
Signal: BUY {symbol} @ {price} | strategy={name} | confidence={score}
Market context: {regime, risk_level}
Should this be approved? Return JSON: {approve, reasoning, adjusted_qty}
```

---

## Concurrency Model

```
main thread
    │
    ├── asyncio.create_task(risk_agent.run())      ← listens on raw_queue
    ├── asyncio.create_task(execution_agent.run()) ← listens on approved_queue
    ├── asyncio.create_task(portfolio_agent.run()) ← 60s heartbeat
    │
    └── APScheduler (runs in asyncio event loop)
            ├── every 5min: intraday_cycle()        ← market_analyst scan
            ├── 9:00am: morning_setup()             ← Claude agents + BenchmarkTracker init
            ├── 3:15pm: close_all_mis()             ← force close intraday
            └── 3:35pm: generate_pnl_report()
```

All state sharing goes through `PortfolioState` (asyncio.Lock protected) and `SignalBus` (asyncio.Queue — thread-safe by design).

---

## File Structure

```
algo-trading-agent/
├── main.py                     CLI entry point
├── run_llm_backtest.py         LLM swing backtest with benchmark comparison
├── status.py                   Live status dashboard
├── pyproject.toml              Dependencies
│
├── config/
│   ├── settings.py             Pydantic BaseSettings (.env loader)
│   ├── universes.py            Nifty50/Midcap150/Smallcap250 + regime universe switching
│   ├── instruments.py          NSE token cache
│   ├── strategy_params.yaml    Tunable strategy parameters
│   └── risk_params.yaml        Hard risk limits
│
├── agents/                     LLM + coordinator agents (8 total)
├── strategies/                 7 trading strategies (BaseStrategy pattern)
├── signals/                    Signal model (Pydantic) + async SignalBus + combiner
├── risk/                       RiskManager, PositionSizer, PortfolioState, Allocator
├── data/                       Market data (KiteConnect + yfinance) + macro signals
├── execution/                  PaperSimulator + KiteExecutor + FillTracker
├── backtesting/                Full LLM swing backtester + benchmark comparison
├── auth/                       Daily OAuth automation (Playwright)
├── scheduling/                 APScheduler + market calendar
├── monitoring/                 Logging, audit trail, alerts, benchmark tracker
└── tests/                      Unit tests (98+ tests, all green)
```
