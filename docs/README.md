# Algo Trading Agent

An AI-powered autonomous trading system for Indian equity markets (NSE/BSE) via Zerodha KiteConnect.

Combines **LLM-driven agents** (Claude) for sentiment analysis, market regime detection, portfolio management, and risk review with traditional quantitative strategies — all orchestrated as a multi-agent async pipeline.

> **Status:** In active development · Currently running in paper trading mode · Phase 2 alpha improvements complete

---

## What it does

Every trading day, the system runs a full pipeline:

- **Pre-market (8:45–9:10am IST):** LLM agents analyze NSE corporate announcements and detect the day's market regime, allocating strategy weights accordingly
- **Market hours (9:15am–3:15pm IST):** Quantitative strategies scan Nifty 50 + BankNifty + IT universe every 5 minutes, publishing signals through a risk gate before execution
- **Swing mode:** LLM Portfolio Manager (`LLMPortfolioManager`) evaluates the full universe, assigns allocations, and holds multi-day positions with regime-aware exit logic
- **End of day (3:15pm IST):** All intraday (MIS) positions force-closed before Zerodha auto square-off; P&L report and benchmark comparison generated

---

## Architecture

Eight agents communicate via an async `SignalBus` (asyncio queues), coordinated by a central `TradingOrchestrator`:

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
| `MarketAnalyst` | No | Scans universe every 5 min, runs 7 strategies, publishes signals |
| `SentimentAgent` | Claude | Scores NSE corporate announcements −10 to +10 pre-market |
| `StrategySelector` | Claude | Detects market regime, allocates strategy weights for the day |
| `LLMPortfolioManager` | Claude | Swing-mode portfolio construction; selects stocks and allocation fractions |
| `RiskAgent` | Claude (borderline only) | Hard rules gate + optional LLM review for low-confidence signals |
| `ExecutionAgent` | No | Routes approved signals to paper simulator or live Kite API |
| `PortfolioAgent` | No | 60-second position monitor, manages exits and stop-losses |
| `TradingOrchestrator` | No | Coordinates all agents, runs morning setup and daily lifecycle |

---

## Strategies

Seven strategies inherit from `BaseStrategy`, each suited to a different market condition:

| Strategy | Regime | Entry Signal | Timeframe | Mode |
|---|---|---|---|---|
| **Momentum** | Trending (ADX > 25) | EMA stack + RSI 50–70 + MACD expanding | 5-min / Daily | Both |
| **Mean Reversion** | Sideways (ADX < 20) | Price ≤ BB lower + RSI < 30 + volume surge | 15-min / Daily | Both |
| **Breakout** | Post-consolidation | Close above 20-period high + 2× volume | 15-min / Daily | Both |
| **Oversold Bounce** | Dip in uptrend | RSI < 30 + MACD turning up + above BB lower | Daily | Both |
| **Overbought Short** | Rejection in downtrend | RSI > 75 + MACD turning down + below EMA(20) | Intraday | MIS only |
| **Sentiment Driven** | News catalyst | Claude sentiment score ≥ 7/10 + trend filter | Daily | Swing |
| **LLM Strategy** | Any (swing) | Claude Sonnet evaluates 5-bar table + indicators | Daily | Swing only |

Every morning, Claude reviews market regime indicators (Nifty RSI, ADX, BB width, VIX, macro signals) and dynamically allocates weights across strategies.

---

## Regime-Aware Portfolio Management

The system detects 5 distinct market regimes and adapts all parameters accordingly:

| Regime | Cash Reserve | Universe | Position Cap | Take-Profit |
|---|---|---|---|---|
| CRASH | 95%+ (force liquidation) | — | — | — |
| BEAR | 80% cash target | NIFTY50 only | 8% | 7% hard stop only |
| NEUTRAL | 20% cash | Mid+Large cap (65 stocks) | 10% | 25% |
| BULL | 10% cash | Full universe (100+ stocks) | 12% | 25% |
| STRONG_BULL | 2% cash | Full universe | 15% | 30% |

Regime is scored from **9 technical + 2 macro signals** (FII/DII flows, India VIX):
- Score ≥ 6 → `STRONG_BULL`
- Score ≥ 4 → `BULL`
- Score ≥ 2 → `NEUTRAL`
- Below 2 → `BEAR`
- Jump detection: single-day −5% drop → immediate `BEAR` escalation

---

## LLM Integration (Claude)

Four of the eight agents use `claude-sonnet-4-6`:

**Sentiment Agent** — pre-market, runs once at 8:45am:
```
Rate the market impact of these NSE announcements for {symbol} on a scale of -10 to +10.
Return JSON: { score, confidence, reasoning }
```

**Strategy Selector** — pre-market, runs once at 9:00am:
```
Market regime indicators: { nifty_rsi, adx, bb_width, 5d_change, vix, fii_net_5d, india_vix }
Return JSON: { regime, strategy_weights, risk_level, reasoning }
```

**LLM Portfolio Manager** — swing cycle, every 3–5 days in bull / at regime transitions:
```
Holdings: { symbol, qty, entry_price, unrealised_pnl_pct }
Available symbols with 5-bar OHLCV + indicators
Portfolio cash/capital/drawdown
Return JSON array: [{ symbol, action_type, quantity }]
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
SignalCombiner (sentiment signals only)
    │  Cross-validates: RSI range, MACD direction, EMA(20), volume, ADX
    │  combined_confidence = 0.6 × sentiment + 0.4 × tech_score
    ▼
PortfolioAllocator
    │  Scores and ranks competing signals
    │  Respects 10% cash floor, max position size, max concurrent positions
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

## Backtesting & Benchmark Comparison

The LLM backtester (`backtesting/strategy_backtester.py`) simulates full swing-mode portfolio management:
- Realistic slippage model (0.10% large-cap, 0.20% mid-cap)
- Regime-switching universe (NIFTY50 in bear, full universe in bull)
- Kelly Criterion adaptive position sizing (after 40 trades of history)
- Intraday jump detection for bear escalation
- Benchmark comparison against NIFTY50

**Verified results (Phase 2, 3 backtested periods):**

| Period | Strategy | NIFTY50 | Alpha | Beat? |
|---|---|---|---|---|
| COVID Crash (2020 Q1) | +1.93% | −29.43% | +31.36% | ✓ |
| 2022 Correction | −8.81% | −10.47% | +1.66% | ✓ |
| 2023 Slow Grind (bull) | +12.53% | +12.47% | +0.06% | ✓ |
| **Average** | — | — | **+11.03%** | **3/3** |

Avg Sharpe: 1.88 · Win rate: 50–69%

**Promotion gate** (before live capital):

| Metric | Minimum |
|---|---|
| Sharpe Ratio | > 1.0 (out-of-sample) |
| Max Drawdown | < 15% |
| Win Rate | > 45% |
| Benchmark beat | 3/3 periods |
| Test period | ≥ 2 years |

---

## Tech Stack

| Layer | Libraries |
|---|---|
| LLM | `anthropic` (Claude Sonnet 4.6) |
| Orchestration | `asyncio`, LangGraph |
| Market data | KiteConnect API, `yfinance` fallback |
| Macro signals | NSE FII/DII API, yfinance `^INDIAVIX` |
| Indicators | `pandas-ta` |
| Backtesting | `backtesting/strategy_backtester.py` (custom), `vectorbt`, `backtrader` |
| Scheduling | `APScheduler` |
| Auth automation | `playwright` (headless Chromium for daily TOTP login) |
| Position sizing | Fixed-fraction, volatility-ATR, half-Kelly (adaptive) |
| Monitoring | `structlog`, append-only audit trail, Telegram alerts, `BenchmarkTracker` |
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
.venv/bin/python main.py run --mode paper --trading both

# Run LLM backtest (3 historical periods, 15 symbols)
.venv/bin/python run_llm_backtest.py --short

# Run unit tests
.venv/bin/python -m pytest tests/unit/ -v
```

> See [docs/QUICKSTART.md](QUICKSTART.md) for full setup including Zerodha KiteConnect app creation.

---

## Project Structure

```
algo-trading-agent/
├── main.py                    CLI entry point (paper / live / backtest)
├── run_llm_backtest.py        LLM swing-mode backtest with benchmark comparison
├── status.py                  Live position dashboard
│
├── agents/
│   ├── orchestrator.py        Central coordinator + morning setup
│   ├── market_analyst.py      5-min strategy scanner (no LLM)
│   ├── sentiment_agent.py     Claude: news → score −10 to +10
│   ├── strategy_selector.py   Claude: regime detection + weight allocation
│   ├── llm_portfolio_manager.py  Claude: swing portfolio construction
│   ├── llm_base.py            Abstract base for LLM portfolio backends
│   ├── risk_agent.py          Hard rules gate + optional Claude
│   ├── portfolio_agent.py     60s heartbeat, exits + SL monitoring
│   └── llm_strategy.py        Claude as a strategy (swing only)
│
├── strategies/
│   ├── momentum.py            EMA/RSI/MACD trend following
│   ├── mean_reversion.py      BB lower + RSI < 30 reversion
│   ├── breakout.py            20-period high + volume breakout
│   ├── oversold_bounce.py     RSI < 30 + MACD turning up
│   ├── overbought_short.py    RSI > 75 + MACD turning down (MIS only)
│   ├── sentiment_driven.py    Claude sentiment ≥ 7 + tech filter
│   └── llm_strategy.py        Claude as swing decision maker
│
├── signals/
│   ├── signal_model.py        Pydantic: Signal / ExitSignal / ApprovedSignal
│   ├── signal_bus.py          Three asyncio.Queue connectors
│   ├── indicators.py          pandas-ta wrappers (EMA, RSI, MACD, BB, ATR, ADX, VWAP)
│   └── signal_combiner.py     Cross-validates sentiment + technical signals
│
├── risk/
│   ├── risk_manager.py        Hard rules (never overridden by LLM)
│   ├── position_sizer.py      Fixed-fraction / ATR / half-Kelly sizing
│   ├── portfolio_state.py     asyncio.Lock-protected state
│   └── portfolio_allocator.py Ranks signals, respects cash floor + position limits
│
├── data/
│   ├── kite_client.py         Singleton KiteConnect instance
│   ├── market_data.py         OHLCV bars: KiteConnect → yfinance fallback
│   ├── yfinance_historical.py Extended yfinance fetcher for backtesting
│   ├── news_fetcher.py        NSE corporate announcements API
│   ├── macro_fetcher.py       FII/DII flows + India VIX + RBI rate
│   └── cache.py               Parquet-based bar cache
│
├── execution/
│   ├── paper_simulator.py     Virtual execution with slippage model
│   ├── kite_executor.py       Live KiteConnect orders + SL-M placement
│   └── fill_tracker.py        WebSocket fill updates → portfolio_state
│
├── backtesting/
│   ├── strategy_backtester.py Full LLM swing backtest engine
│   └── benchmark_comparison.py NIFTY50 benchmark vs strategy comparison
│
├── monitoring/
│   ├── logger.py              structlog JSON logger
│   ├── audit_trail.py         Append-only JSONL decision log
│   ├── alerting.py            Telegram alerts (fills, P&L, kill switches)
│   └── benchmark_tracker.py   Daily alpha vs NIFTY50 tracker
│
├── config/
│   ├── settings.py            Pydantic settings (.env loader)
│   ├── universes.py           NIFTY50 / Midcap150 / Smallcap250 + regime universe
│   ├── instruments.py         NSE token cache
│   └── risk_params.yaml       Hard risk limits
│
├── scheduling/
│   ├── market_calendar.py     NSE hours, holidays, IST timezone
│   ├── intraday_scheduler.py  APScheduler 5min cron 9:15–3:15pm
│   └── swing_scheduler.py     Pre-market + EOD swing cycle
│
├── auth/                      Daily Playwright TOTP automation
└── tests/unit/                98+ unit tests (all green)
```

---

## Documentation

- [Architecture & agent pipeline](ARCHITECTURE.md)
- [Trading strategies](STRATEGIES.md)
- [Risk management rules](RISK_MANAGEMENT.md)
- [R&D decisions and tradeoffs](RND_DECISIONS.md)
- [Tech stack rationale](TECH_STACK.md)
- [Quickstart guide](QUICKSTART.md)
- [LLM provider options](../LLM_PROVIDERS.md)

---

## Safety

- `PAPER_TRADING=true` in `.env` forces paper mode regardless of CLI flags — real orders require explicit override
- All MIS positions force-closed at 3:15pm IST before Zerodha auto square-off
- Hard risk rules are **never overridden** by LLM agents — Claude only reviews borderline signals
- Daily max drawdown kill switch halts trading for the rest of the session if breached
- Regime-based circuit breakers: CRASH/BEAR regimes auto-liquidate to 80%+ cash

---

*Built as a personal project to explore multi-agent LLM orchestration patterns applied to quantitative finance. The same architectural principles — async agent coordination, event-driven signal passing, LLM-augmented decision layers, and evaluation gates before promotion — are directly applicable to enterprise agentic AI systems.*
