# Algo Trading Agent

An AI-powered autonomous trading system for Indian equity markets (NSE/BSE) via Zerodha KiteConnect.

Combines **LLM-driven agents** (Claude) for sentiment analysis, market regime detection, and risk review with traditional quantitative strategies — all orchestrated as a multi-agent async pipeline.

> **Status:** In active development · Currently running in paper trading mode

---

## What it does

Every trading day, the system runs a full pipeline:

- **Pre-market (8:45–9:10am IST):** LLM agents analyze NSE corporate announcements and detect the day's market regime, allocating strategy weights accordingly
- **Market hours (9:15am–3:15pm IST):** Quantitative strategies scan Nifty 50 + BankNifty + IT universe every 5 minutes, publishing signals through a risk gate before execution
- **End of day (3:15pm IST):** All intraday (MIS) positions force-closed before Zerodha auto square-off; P&L report generated

---

## Architecture

Seven agents communicate via an async `SignalBus` (asyncio queues), coordinated by a central `TradingOrchestrator`:

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
             │   Risk Agent    │  ← hard rules + optional Claude for borderline
             └────────┬────────┘
                      │ approved signals
             ┌────────▼───────────────────┐
             │   Execution Agent          │
             │ paper_simulator OR kite    │
             └────────┬───────────────────┘
                      │
             ┌────────▼────────┐
             │ Portfolio Agent │  ← 60s heartbeat, manages exits
             └─────────────────┘
```

| Agent | LLM? | Role |
|---|---|---|
| `MarketAnalyst` | No | Scans universe every 5 min, runs 4 strategies, publishes signals |
| `SentimentAgent` | Claude | Scores NSE corporate announcements −10 to +10 pre-market |
| `StrategySelector` | Claude | Detects market regime, allocates strategy weights for the day |
| `RiskAgent` | Claude (borderline only) | Hard rules gate + optional LLM review for low-confidence signals |
| `ExecutionAgent` | No | Routes approved signals to paper simulator or live Kite API |
| `PortfolioAgent` | No | 60-second position monitor, manages exits and stop-losses |
| `TradingOrchestrator` | No | Coordinates all agents, runs morning setup and daily lifecycle |

---

## Strategies

Four strategies inherit from `BaseStrategy`, each suited to a different market regime:

| Strategy | Regime | Entry signal | Timeframe |
|---|---|---|---|
| **Momentum** | Trending (ADX > 25) | EMA stack + RSI 50–70 + MACD expanding | 5-min / Daily |
| **Mean Reversion** | Sideways (ADX < 20) | Price ≤ BB lower band + RSI < 30 + volume surge | 15-min / Daily |
| **Breakout** | Post-consolidation | Close above 20-period high + 2× volume | 15-min / Daily |
| **Sentiment Driven** | News catalyst | Claude sentiment score ≥ 7/10 + trend filter | Daily |

Every morning, Claude reviews market regime indicators (Nifty RSI, ADX, BB width, VIX) and dynamically allocates weights across strategies:

```
regime = "trending"  →  momentum: 50%, breakout: 30%, mean_rev: 10%, sentiment: 10%
regime = "sideways"  →  mean_rev: 50%, breakout: 20%, momentum: 20%, sentiment: 10%
regime = "volatile"  →  mean_rev: 40%, sentiment: 30%, breakout: 20%, momentum: 10%
```

---

## LLM Integration (Claude)

Three of the seven agents use `claude-sonnet-4-6`:

**Sentiment Agent** — pre-market, runs once at 8:45am:
```
Rate the market impact of these NSE announcements for {symbol} on a scale of -10 to +10.
Return JSON: { score, confidence, reasoning }
```

**Strategy Selector** — pre-market, runs once at 9:00am:
```
Market regime indicators: { nifty_rsi, adx, bb_width, 5d_change, vix }
Return JSON: { regime, strategy_weights, risk_level, reasoning }
```

**Risk Agent** — intraday, only for borderline signals (confidence < 0.7):
```
Signal: BUY {symbol} @ {price} | strategy={name} | confidence={score}
Should this be approved? Return JSON: { approve, reasoning, adjusted_qty }
```

---

## Signal Flow

```
Strategy generates signal
    │
    ▼
Signal { symbol, action, strategy, entry_price, stop_loss, target, confidence }
    │
    ▼
RiskAgent — hard rules gate (position limits, sector exposure, timing cutoff)
    │
    ▼
ExecutionAgent → paper_simulator (PAPER-XXXXX) or kite_executor (real order)
    │
    ▼
PortfolioAgent — monitors 60s, publishes ExitSignal when SL/target/trailing hit
```

---

## Backtesting & Promotion Gate

Strategies are backtested with VectorBT before being allowed near real capital. A strategy must pass all gates to be promoted:

| Metric | Minimum threshold |
|---|---|
| Sharpe Ratio | > 1.0 (out-of-sample) |
| Max Drawdown | < 15% |
| Win Rate | > 45% |
| Trade count | ≥ 200 |
| Test period | ≥ 2 years |

Walk-forward testing is supported to avoid overfitting:

```bash
python main.py backtest --start 2022-01-01 --end 2024-12-31 --strategy momentum --walk-forward
```

---

## Tech Stack

| Layer | Libraries |
|---|---|
| LLM | `anthropic` (Claude Sonnet) |
| Orchestration | `asyncio`, LangGraph |
| Market data | KiteConnect API, `yfinance` fallback, `nsepy` |
| Indicators | `pandas-ta` |
| Backtesting | `vectorbt`, `backtrader` |
| Scheduling | `APScheduler` |
| Auth automation | `playwright` (headless Chromium for daily TOTP login) |
| Position sizing | Fixed-fraction, volatility-ATR, half-Kelly |
| Monitoring | `structlog`, append-only audit trail, Telegram alerts |
| Config | `pydantic-settings` + `.env` |

---

## Quick Start

```bash
# Install
git clone https://github.com/Sushant0809/algo-trading-agent
cd algo-trading-agent
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Configure
cp .env.example .env
# Fill in: KITE_API_KEY, KITE_API_SECRET, ZERODHA_USER_ID, ZERODHA_PASSWORD,
#          ZERODHA_TOTP_SECRET, ANTHROPIC_API_KEY, PAPER_TRADING=true

# Run in paper mode
python main.py run --mode paper --trading both

# Backtest a strategy
python main.py backtest --start 2023-01-01 --end 2024-12-31 --strategy momentum
```

> See [docs/QUICKSTART.md](docs/QUICKSTART.md) for full setup including Zerodha KiteConnect app creation.

---

## Project Structure

```
algo-trading-agent/
├── main.py                    CLI entry point (paper / live / backtest)
├── status.py                  Live position dashboard
│
├── agents/                    7 agents + orchestrator
├── strategies/                4 trading strategies (BaseStrategy pattern)
├── signals/                   Signal model (Pydantic) + async SignalBus
├── risk/                      RiskManager, PositionSizer, PortfolioState
├── data/                      KiteConnect + yfinance data layer + cache
├── execution/                 PaperSimulator + KiteExecutor + FillTracker
├── auth/                      Daily Playwright-based TOTP auth automation
├── scheduling/                APScheduler + NSE market calendar
├── monitoring/                structlog, AuditTrail, TelegramAlerter
├── backtesting/               VectorBT runner + walk-forward + report generator
├── config/                    Pydantic settings, universes, strategy YAML params
└── docs/
    ├── ARCHITECTURE.md        Full system design and concurrency model
    ├── STRATEGIES.md          All 4 strategies — logic, parameters, entry/exit rules
    ├── RISK_MANAGEMENT.md     Risk rules, kill switches, position sizing
    ├── TECH_STACK.md          Every library and the reasoning behind choosing it
    ├── RND_DECISIONS.md       Research decisions, tradeoffs, what was tried and rejected
    └── QUICKSTART.md          Install, configure, and run
```

---

## Documentation

- [Architecture & agent pipeline](docs/ARCHITECTURE.md)
- [Trading strategies](docs/STRATEGIES.md)
- [Risk management rules](docs/RISK_MANAGEMENT.md)
- [R&D decisions and tradeoffs](docs/RND_DECISIONS.md)
- [Tech stack rationale](docs/TECH_STACK.md)
- [Quickstart guide](docs/QUICKSTART.md)

---

## Safety

- `PAPER_TRADING=true` in `.env` forces paper mode regardless of CLI flags — real orders require explicit override
- All MIS positions force-closed at 3:15pm IST before Zerodha auto square-off
- Hard risk rules are never overridden by LLM agents — Claude only reviews borderline signals
- Daily max drawdown kill switch halts trading for the rest of the session if breached

---

*Built as a personal project to explore multi-agent LLM orchestration patterns applied to quantitative finance. The same architectural principles — async agent coordination, event-driven signal passing, LLM-augmented decision layers, and evaluation gates before promotion — are directly applicable to enterprise agentic AI systems.*
