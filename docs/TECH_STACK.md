# Tech Stack — Libraries Used and Why

## Broker & Market Data

### kiteconnect
**What:** Official Zerodha Python SDK
**Why:** Only official way to place real orders on Zerodha. Provides instrument token lookup, order placement, historical data (requires Connect subscription), and WebSocket streaming for live fills.
**Used for:** Order execution, instrument cache, WebSocket fill tracking

### yfinance
**What:** Yahoo Finance unofficial Python wrapper
**Why:** Free fallback for OHLCV data when KiteConnect historical API is unavailable (e.g., basic plan). Works for NSE stocks using `.NS` suffix. Reliable, no authentication needed.
**Used for:** Fetching historical bars and live quotes when KiteConnect plan doesn't cover them

### httpx
**What:** Async HTTP client
**Why:** Used to scrape NSE India's public API (`nseindia.com/api/`) for corporate announcements. Async-native, works well with Python's asyncio event loop unlike `requests`.
**Used for:** News fetching for sentiment analysis

---

## AI / LLM

### anthropic
**What:** Official Anthropic Python SDK
**Why:** Direct access to Claude API. Synchronous client used (not async) since Claude calls are isolated and wrapped in `asyncio.to_thread`.
**Model used:** `claude-sonnet-4-6` — best balance of reasoning quality and cost for financial analysis tasks
**Used for:** Sentiment scoring, strategy selection, borderline risk review

### langgraph + langchain-anthropic
**What:** Graph-based agent orchestration framework
**Why:** Chosen for the `StateGraph` pattern — allows defining agent steps as nodes with typed state passed between them. Makes the morning setup sequence (sentiment → strategy select → risk pre-approve) explicit and inspectable.
**Used for:** Morning setup orchestration flow (currently simplified to direct async calls, LangGraph state machine planned for v2)

---

## Technical Analysis

### pandas-ta
**What:** 130+ technical indicators built on pandas
**Why:** Pure Python, no TA-Lib C library required (easier installation). All indicators needed (EMA, RSI, MACD, Bollinger Bands, ATR, ADX, VWAP) are available with clean pandas DataFrame API.
**Used for:** Computing all indicators in `signals/indicators.py`

---

## Data & Config

### pandas + numpy
**What:** Standard data science stack
**Why:** Industry standard for financial time series. All OHLCV data is stored as DataFrames. numpy used for vectorized calculations.

### pydantic-settings
**What:** Settings management with `.env` file loading
**Why:** Type-safe configuration. `.env` values are automatically parsed to correct Python types (bool, int, Path). Validation catches config errors at startup.
**Used for:** `config/settings.py` — all environment variables

### pyotp
**What:** Python TOTP/HOTP library
**Why:** Generates Time-based One-Time Passwords (TOTP) programmatically. Required to automate the Zerodha 2FA step during daily login without manual intervention.
**Used for:** Auto-generating TOTP codes in Playwright login flow

---

## Web Automation

### playwright
**What:** Modern browser automation library
**Why:** KiteConnect OAuth requires a real browser login — there's no headless API. Playwright automates Chromium to navigate the login page, fill credentials, enter TOTP, and capture the `request_token` from the redirect URL. Chosen over Selenium for better async support and more reliable element interception.
**Used for:** Daily 8:30am automated Zerodha login

---

## Scheduling

### APScheduler
**What:** Advanced Python Scheduler
**Why:** Supports cron-style scheduling with timezone awareness (`Asia/Kolkata`). Can run async jobs natively in the asyncio event loop. `BackgroundScheduler` runs jobs without blocking the main loop.
**Used for:** 5-minute intraday cycles, daily morning setup, EOD close jobs

### holidays
**What:** Python library for public holiday detection
**Why:** NSE is closed on Indian public holidays. Used in `scheduling/market_calendar.py` to check if today is a trading day before starting any market operations.
**Used for:** `is_trading_day()` check in market calendar

---

## Backtesting

### vectorbt
**What:** High-performance vectorized backtesting library
**Why:** Extremely fast (numpy-based, not loop-based). Can test a strategy across 50 stocks × 5 years in seconds. Good for parameter optimization and walk-forward analysis.
**Used for:** Fast signal-based backtests, parameter sweeps

### backtrader
**What:** Event-driven backtesting framework
**Why:** More realistic simulation — models order slippage, partial fills, and position sizing more accurately than vectorized approaches. Good for final strategy validation before live trading.
**Used for:** Realistic backtests with order execution modeling

### quantstats
**What:** Portfolio analytics and reporting
**Why:** Generates professional HTML tearsheets (Sharpe, drawdown, monthly returns heatmap, etc.) with one function call. Standard tool used by quant funds.
**Used for:** Generating backtest performance reports

---

## Async & Concurrency

### asyncio (stdlib)
**What:** Python's built-in async I/O framework
**Why:** All agents run as concurrent async tasks. Signal queues (`asyncio.Queue`) are the communication backbone between agents. `asyncio.Lock` protects shared portfolio state.
**Used for:** Core concurrency model for all agents

---

## Logging & Monitoring

### structlog
**What:** Structured logging library
**Why:** Outputs JSON-formatted logs instead of plain text. Makes logs machine-readable and easy to grep/filter. Each log entry has consistent fields: `event`, `logger`, `level`, `timestamp`.
**Used for:** All application logging → `logs/trading.log`

---

## Testing

### pytest + pytest-asyncio
**What:** Testing framework with async support
**Why:** `pytest-asyncio` allows testing async functions with `@pytest.mark.asyncio`. Clean fixture system for setting up portfolio state, audit trails, etc.
**Used for:** All unit and integration tests

---

## CLI

### click
**What:** Python CLI framework
**Why:** Clean decorator-based CLI definition. `main.py` uses it to create `run`, `backtest`, and `auth-refresh` subcommands with typed arguments.
**Used for:** `main.py` command-line interface

---

## Summary Table

| Category | Library | Version Constraint |
|----------|---------|-------------------|
| Broker | kiteconnect | ≥ 4.2 |
| Market Data (fallback) | yfinance | ≥ 0.2 |
| LLM | anthropic | ≥ 0.40 |
| Agent Orchestration | langgraph | ≥ 0.2 |
| Technical Analysis | pandas-ta | ≥ 0.3 |
| Data | pandas, numpy | latest |
| Config | pydantic-settings | ≥ 2.0 |
| TOTP | pyotp | ≥ 2.9 |
| Browser Automation | playwright | ≥ 1.44 |
| Scheduler | APScheduler | ≥ 3.10 |
| Holidays | holidays | ≥ 0.46 |
| Backtesting (fast) | vectorbt | ≥ 0.26 |
| Backtesting (realistic) | backtrader | ≥ 1.9 |
| Reporting | quantstats | ≥ 0.0.62 |
| Logging | structlog | ≥ 24.0 |
| HTTP | httpx | ≥ 0.27 |
| CLI | click | ≥ 8.1 |
| Testing | pytest, pytest-asyncio | latest |
| Parquet cache | pyarrow | ≥ 16.0 |
