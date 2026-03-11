# System Architecture

## Overview

The agent is a Python asyncio application that runs continuously during NSE market hours (9:15am–3:30pm IST). It combines traditional quantitative strategies with LLM-powered agents for sentiment analysis, regime detection, and risk review.

```
┌─────────────────────────────────────────────────────┐
│              ORCHESTRATOR (LangGraph)                │
│         Morning setup → Intraday loop                │
└──────┬──────────────┬─────────────┬─────────────────┘
       │              │             │
  ┌────▼────┐   ┌─────▼────┐  ┌────▼──────────┐
  │ Market  │   │Sentiment │  │  Strategy     │
  │ Analyst │   │  Agent   │  │  Selector     │
  │(no LLM) │   │ (Claude) │  │  (Claude)     │
  └────┬────┘   └─────┬────┘  └────┬──────────┘
       │              │             │
       └──────────────┴─────────────┘
                      │ signals (asyncio.Queue)
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
| `news_fetcher.py` | NSE corporate announcements via NSE India public API |
| `universe_filter.py` | Filters stock universes by liquidity and volume |
| `cache.py` | Parquet-based bar cache to avoid re-fetching |
| `historical.py` | Extended historical data for backtesting |

**Data flow for market data:**
1. Try KiteConnect `historical_data()` API
2. If permission denied / unavailable → fall back to `yfinance` with `.NS` suffix
3. Cache result to parquet to speed up next request

### 2. Signal Layer (`signals/`)

| Module | Responsibility |
|--------|---------------|
| `indicators.py` | pandas-ta wrappers — computes EMA, RSI, MACD, BB, ATR, ADX, VWAP |
| `signal_model.py` | Pydantic dataclasses: `Signal`, `ExitSignal`, `ApprovedSignal` |
| `signal_bus.py` | Three asyncio queues connecting agents |

**Signal Bus queues:**
```
raw_queue:      strategy → risk_agent
approved_queue: risk_agent → execution_agent
exit_queue:     portfolio_agent → execution_agent
```

### 3. Strategy Layer (`strategies/`)

Four strategies all inherit from `BaseStrategy`:

| Strategy | Market Condition | Product | Timeframe |
|----------|-----------------|---------|-----------|
| Momentum | Trending (ADX > 25) | MIS/CNC | 5min / Daily |
| Mean Reversion | Sideways, RSI < 30 | MIS/CNC | 15min / Daily |
| Breakout | Consolidation → break | MIS/CNC | 15min / Daily |
| Sentiment Driven | News catalyst ≥ 7/10 | MIS/CNC | Daily |

Each strategy's `generate_signal(symbol, df, mode)` returns a `Signal` or `None`.

### 4. Agent Layer (`agents/`)

#### Market Analyst (no LLM)
- Runs every 5 minutes during market hours
- Fetches OHLCV → computes all indicators → runs all 4 strategies
- Publishes signals to `raw_queue`
- No Claude API calls — purely quantitative

#### Sentiment Agent (Claude)
- Runs once at 8:45am IST (pre-market)
- Fetches last 24h NSE corporate announcements for top 30 symbols
- Sends each symbol's news to Claude with a scoring prompt
- Claude returns score (-10 to +10) with reasoning
- High scores (≥ 7) can trigger the sentiment_driven strategy

#### Strategy Selector (Claude)
- Runs once at 9:00am IST (pre-market)
- Feeds Nifty 50 regime indicators (RSI, ADX, BB width, 5d/20d change) to Claude
- Claude returns: regime type + strategy weight allocation (sum = 1.0)
- Weights adjust how aggressively each strategy is used that day

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

`PortfolioState` uses `asyncio.Lock` for all writes, ensuring no race conditions between the portfolio agent (reading/writing positions) and execution agent (opening/closing).

### 6. Execution Layer (`execution/`)

| Module | Responsibility |
|--------|---------------|
| `order_manager.py` | Routes orders to paper or live based on `PAPER_TRADING` flag |
| `paper_simulator.py` | Virtual execution: 0.05% slippage model, PAPER-XXXXXX order IDs |
| `kite_executor.py` | Real KiteConnect orders + SL-M stop placement |
| `fill_tracker.py` | KiteConnect WebSocket → update portfolio_state on fills |

### 7. Scheduling Layer (`scheduling/`)

| Module | Responsibility |
|--------|---------------|
| `market_calendar.py` | NSE trading hours, holidays 2024-2025, IST timezone |
| `intraday_scheduler.py` | APScheduler: 5min cron 9:15–3:15pm IST |
| `swing_scheduler.py` | APScheduler: pre-market 9:00am + EOD 4:00pm |

### 8. Auth Layer (`auth/`)

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

### 9. Monitoring Layer (`monitoring/`)

| Module | Responsibility |
|--------|---------------|
| `logger.py` | structlog JSON logger → `logs/trading.log` |
| `audit_trail.py` | Append-only JSONL decision log → `logs/audit/` |
| `alerting.py` | Telegram alerts for fills, P&L, kill switches (optional) |

---

## LLM Agents

Three agents use Claude (`claude-sonnet-4-6`):

### Sentiment Agent prompt structure
```
You are a sentiment analyst for Indian equity markets.
News items for {symbol}: {headlines}
Rate sentiment from -10 to +10. Return JSON: {score, confidence, reasoning}
```

### Strategy Selector prompt structure
```
Market regime indicators: {nifty_rsi, adx, bb_width, 5d_change, vix}
Available strategies: momentum, mean_reversion, breakout, sentiment_driven
Return JSON: {regime, strategy_weights, risk_level, reasoning}
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
            ├── 9:00am: morning_setup()             ← Claude agents
            ├── 3:15pm: close_all_mis()             ← force close intraday
            └── 3:35pm: generate_pnl_report()
```

All state sharing goes through `PortfolioState` (asyncio.Lock protected) and `SignalBus` (asyncio.Queue — thread-safe by design).

---

## File Structure

```
algo-trading-agent/
├── main.py                    CLI entry point
├── status.py                  Live status dashboard
├── pyproject.toml             Dependencies
│
├── config/
│   ├── settings.py            Pydantic BaseSettings (.env loader)
│   ├── universes.py           Nifty50, BankNifty, IT, Midcap150, Smallcap250
│   ├── instruments.py         NSE token cache (9,400+ instruments)
│   ├── strategy_params.yaml   Tunable strategy parameters
│   └── risk_params.yaml       Hard risk limits
│
├── agents/                    LLM + coordinator agents
├── strategies/                4 trading strategies
├── signals/                   Signal model + async bus
├── risk/                      Risk manager + position sizer
├── data/                      Market data (KiteConnect + yfinance)
├── execution/                 Paper simulator + live executor
├── auth/                      Daily OAuth automation (Playwright)
├── scheduling/                APScheduler + market calendar
├── monitoring/                Logging, audit trail, alerts
├── backtesting/               vectorbt + backtrader runners
└── tests/                     Unit + integration tests
```
