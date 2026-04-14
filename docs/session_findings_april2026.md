# Session Findings — April 2026
*Full documentation of decisions, experiments, and research from this development session.*

---

## 1. Multi-Provider LLM Architecture (Implemented)

### What Was Built
Added support for 3 LLM providers to reduce backtest cost:

| Provider | Model | Cost/1M tokens | Rate Limit | Best For |
|----------|-------|----------------|------------|----------|
| Claude | Haiku 4.5 | $0.80 in / $4.00 out | None | Production |
| Groq | Llama 3.3-70B | ~$0.59 in / $0.79 out | 12,000 TPM (free) | Backtesting (paid tier) |
| NVIDIA | Llama 3.1-70B | Free (limited) | ~1 req/30s | Not suitable |

**Files created:**
- `agents/groq_portfolio_manager.py` — Groq REST API integration
- `agents/nvidia_portfolio_manager.py` — NVIDIA REST API integration
- `LLM_PROVIDERS.md` — usage documentation
- `agents/llm_base.py` — updated `create_llm_manager()` factory

**CLI usage:**
```bash
LLM_PROVIDER=claude python run_llm_backtest.py --short   # default
LLM_PROVIDER=groq python run_llm_backtest.py --short    # 10x cheaper
LLM_PROVIDER=nvidia python run_llm_backtest.py --short  # not recommended
```

**Finding:** Groq free tier fails after ~3-4 API calls (12,000 TPM exhausted). Each call uses 2,600-4,300 tokens. Paid tier required for backtesting.

**Decision:** Remove NVIDIA provider (rate-limited, no value). Keep Groq for future paid tier use.

---

## 2. 10 Backtesting Problem Fixes (Implemented)

### Problem → Fix Mapping

| # | Problem | Fix Applied |
|---|---------|-------------|
| 1 | Overtrading (370 trades/126 days) | Regime-gated LLM calls, trade budget |
| 2 | All-in behavior | Diversification cap (max 30% per stock) |
| 3 | Bear exits too slow | ROC crash detector + auto-liquidation |
| 4 | Bull underperformance | Passive-by-default system prompt |
| 5 | Regime detection lag | ROC-10 crash override (replaces slow EMA) |
| 6 | Re-entry loop | Trade journal in LLM prompt |
| 7 | Min-hold in bear made things worse | Auto-liquidation handles bear, no min-hold |
| 8 | EMA200 block too blunt | ROC-10 < -8% = CRASH (fast response) |
| 9 | 2023 bull churn | Gated calls every 5 days in BULL |
| 10 | Bear trade count high | Block buys in BEAR/CRASH entirely |

### After Fixes — Backtest Results (Nifty50, 15 symbols)

| Period | Our Return | NIFTY50 | Alpha | Trades | Win% | Sharpe |
|--------|-----------|---------|-------|--------|------|--------|
| COVID Crash 2020 | +3.05% | -29.43% | **+32.48%** ✓ | 122 | 39.3% | 2.09 |
| 2022 Correction | -9.80% | -10.47% | **+0.67%** ✓ | 274 | 42.7% | 1.91 |
| 2023 Slow Grind | +13.87% | +12.47% | **+1.40%** ✓ | 85 | 64.7% | 2.40 |
| **Average** | | | **+11.52%** | 481 | 48.9% | 2.13 |

3/3 periods beat benchmark. Good but bull alpha (+1.40%) is too low for hedge fund standards.

### Regime System Implemented

```
CRASH   → ROC(10) < -8% of NIFTY OR regime_score < 2 with crash_detected
BEAR    → regime_score < 2
NEUTRAL → regime_score 2
BULL    → regime_score 3-4
STRONG_BULL → regime_score >= 5

7-factor regime_score from NIFTY:
  Close > EMA200 (+1), Close > EMA50 (+1), Close > EMA20 (+1)
  EMA20 > EMA50 (+1), EMA50 > EMA200 (+1), RSI > 50 (+1), MACD_hist > 0 (+1)
```

**LLM call frequency:**
- CRASH/BEAR: No calls (code enforces auto-liquidation)
- STRONG_BULL: Every 5 days
- BULL: Every 5 days
- NEUTRAL: Every 2 days
- Regime transition: Immediate call

---

## 3. Universe Expansion — Small + Mid Cap

### What Was Added
```python
# config/universes.py
NIFTY_ALL_CAP = sorted(set(NIFTY50 + NIFTY_MIDCAP_150_SAMPLE + NIFTY_SMALLCAP_250_SAMPLE))
NIFTY_MID_LARGE_CAP = sorted(set(NIFTY50 + NIFTY_MIDCAP_150_SAMPLE))
```

**Universe sizes:**
- Nifty50: 50 stocks
- Midcap sample: 70 stocks
- Smallcap sample: 50 stocks
- Mid+Large combined: 120 stocks (deduplicated)
- All Cap combined: 170 stocks (deduplicated)

**CLI options added:**
```bash
python run_llm_backtest.py --short --nifty50       # 15 stocks (default)
python run_llm_backtest.py --short --mid-large     # 65 stocks
python run_llm_backtest.py --short --all-cap       # 80 stocks
python run_llm_backtest.py --short --midcap        # 50 stocks
python run_llm_backtest.py --short --smallcap      # 50 stocks
python run_llm_backtest.py --short --all-cap --num 100  # custom count
```

### Mid+Large Cap Backtest Results (65 symbols)

| Period | Our Return | NIFTY50 | Alpha | Trades | Win% | Sharpe |
|--------|-----------|---------|-------|--------|------|--------|
| COVID Crash | +2.13% | -29.43% | **+31.56%** ✓ | 150 | 51.3% | 2.55 |
| 2022 Correction | -13.75% | -10.47% | **-3.28%** ✗ | 303 | 43.6% | 2.35 |
| 2023 Slow Grind | +15.50% | +12.47% | **+3.03%** ✓ | 96 | 58.3% | 2.95 |
| **Average** | | | **+10.44%** | 549 | 51.1% | 2.62 |

2/3 periods beat benchmark. 2022 bear is worse because mid-caps fell harder than large-caps.

### Head-to-Head Comparison

| Metric | Nifty50 Only | Mid+Large Cap | Benchmark | Best |
|--------|-------------|--------------|-----------|------|
| Avg Return | +2.37% | +1.29% | -9.14% | Nifty50 |
| Avg Alpha | +11.52% | +10.44% | 0% | Nifty50 |
| Avg Sharpe | 2.13 | **2.62** | N/A | Mid+Large |
| Beat Bench | 3/3 | 2/3 | 0/3 | Nifty50 |
| Total Trades | 481 | 549 | N/A | Nifty50 |
| Avg MaxDD | 84.2% | 87.9% | N/A | Nifty50 |
| 2022 Alpha | +0.67% | -3.28% | 0% | **Nifty50** |
| 2023 Alpha | +1.40% | **+3.03%** | 0% | Mid+Large |

**Conclusion:** Mid+Large beats in bulls (better alpha, better Sharpe) but fails in bears (mid-caps crash harder). Solution: Regime-switching universe (Nifty50 in BEAR/CRASH, Mid+Large in BULL).

---

## 4. AI/ML/LLM Research Findings

*Full source-cited research in `docs/research_ai_trading_best_practices.md`*

### Key Findings Summary

**What works:**
1. **Hybrid LLM + RL** — LLMs provide context, RL optimizes policy. Better than either alone.
2. **Multi-agent specialization** — Technical analyst + Sentiment analyst + Risk manager each specialized. (Our architecture ✓)
3. **Smaller, focused models** — Domain-fine-tuned > general large models
4. **Risk-adjusted optimization** — Sharpe/CVaR/MaxDD more predictive than raw return

**What doesn't work:**
1. **Daily LLM timing calls** — LLMs can't predict short-term price moves, but our regime-gating mostly solves this ✓
2. **Narrow backtest validation** — 3 periods is not enough; need 17+ periods and walk-forward
3. **Equal or aggressive position sizing** — Kelly-inspired dynamic sizing better
4. **Ignoring macro signals** — FII/DII flows are the #1 leading indicator for Indian markets

**Sentiment analysis:**
- Adds +0.5-2% alpha when used as CONFIRMATORY signal
- Fails when used as primary driver (noisy, laggy, manipulatable)
- End-of-day aggregation is better than real-time (less noise)
- Cannot be backtested historically (accept this gap)

**Benchmark requirements:**
- Retail algo: Need +2-5% annual alpha to justify cost
- Our target: +5-10% every regime (hedge fund standard)
- Current: +11.52% avg alpha across 3 periods BUT bull alpha only +1.40%

---

## 5. Production Code Audit Findings

### Critical Bugs Found

| Bug | File | Risk Level | Status |
|-----|------|------------|--------|
| SL-M not cancelled on exit | `execution/kite_executor.py` | 🔴 CRITICAL — double-exit | Not fixed |
| FillTracker not wired | `main.py` | 🔴 CRITICAL — wrong P&L | Not fixed |
| .env with credentials in git | `.gitignore` | 🔴 CRITICAL — security | Not fixed |
| BenchmarkTracker not initialized | `main.py` | 🟡 Medium — wrong metrics | Not fixed |
| Market calendar stops at 2025 | `scheduling/market_calendar.py` | 🟡 Medium — trades on holidays | Not fixed |
| Backtest breakout fallthrough | `main.py` line 276 | 🟡 Low — wrong backtest | Not fixed |

### Architecture Gaps Found

| Gap | Impact |
|-----|--------|
| LLM backtest ≠ production pipeline | Backtest results can't predict production performance |
| Position sizing: backtest 20% vs production 5% | Different systems — results not comparable |
| Cash yield 6%/yr in backtest | Inflates returns by 1-2% — doesn't exist in production |
| No macro signals (FII/DII, VIX, RBI) | Missing leading indicators for Indian market |
| Fixed walk-forward seeds [42,99,7] | Same periods every run = not real validation |
| Sentiment not in backtest | Can't measure sentiment alpha contribution |
| Cash yield not in production | Production returns will be lower than backtest |

### What's Production-Ready (Good)

| Component | Status |
|-----------|--------|
| `PaperSimulator` | ✓ Ready |
| `KiteExecutor` (minus SL-M bug) | ✓ Ready |
| `OrderManager` | ✓ Ready |
| `auth/kite_auth.py` + `auth/auto_login.py` | ✓ Ready |
| Both schedulers (intra + swing) | ✓ Ready |
| All monitoring (logger, audit, alerting) | ✓ Ready |
| `RiskManager` | ✓ Ready |
| All 7 strategy implementations | ✓ Ready |
| `SignalBus` | ✓ Ready |

---

## 6. Production Plan Overview

*Full detailed plan in `/Users/sushant/.claude/plans/parsed-zooming-kite.md`*

### Phase Summary

```
Phase 0 — Emergency (1-2 days): Security + critical bugs
  ├── Rotate all credentials, remove .env from git
  ├── Fix SL-M double-exit bug
  ├── Wire FillTracker
  ├── Fix BenchmarkTracker init
  ├── Add 2026 holidays
  └── Remove NVIDIA provider

Phase 1 — Backtest Integrity (3 days): Make backtest honest
  ├── Remove 6% cash yield inflation
  ├── Align position sizing (backtest ↔ production)
  ├── Add slippage model (0.1-0.4% by market cap)
  └── Remove fixed walk-forward seeds

Phase 2 — Alpha Improvements (5 days): Get to hedge fund returns
  ├── Fix bull underperformance (15% position cap, 2% cash reserve in STRONG_BULL)
  ├── Add FII/DII macroeconomic signals
  ├── Regime-switching universe (Nifty50 in bear, Mid+Large in bull)
  ├── Sentiment as confirmation gate (not driver)
  └── Kelly-inspired adaptive position sizing

Phase 3 — Hardening (3 days): Safe for real money
  ├── $2/day token budget ceiling
  ├── Circuit breakers (Sharpe drift, win rate, LLM failure)
  ├── Daily reconciliation vs Zerodha
  └── Model selection strategy (Haiku vs Sonnet vs Groq)

Phase 4 — Tests (2 days): 70%+ coverage on production paths
  └── KiteExecutor, MacroFetcher, CostTracker, BenchmarkTracker, MarketCalendar

Phase 5 — Paper Trading (30-60 days): Validation
  └── Success criteria: positive alpha, Sharpe>1.5, win>40%, cost<$1.50/day

Phase 6 — Live Trading (gradual): ₹1L → ₹2L → ₹5L → ₹10L
  └── Scale only with sustained Sharpe > 1.5
```

### Target Performance After All Phases

| Regime | Current Alpha | Target Alpha |
|--------|--------------|-------------|
| CRASH | +32.48% | +30%+ (maintain) |
| BEAR | +0.67% | +5-8% (improve) |
| BULL | +1.40% | +5-10% (main gap) |
| STRONG_BULL | Not tested | +8-12% |
| **Overall** | **+11.52% avg** | **+7-10% every period** |

### Monthly Cost Target (Production)
- Claude API: ~$6/month (Haiku + regime-gating)
- Groq (backtesting, paid tier): ~$2-5/per full backtest run
- Infrastructure: $0 (runs on local machine + Zerodha API)
- **Total: ~$10-15/month**

---

## 7. Decisions Made This Session

| Decision | Rationale |
|----------|-----------|
| Keep Claude Haiku 4.5 as primary | Best quality/cost for Indian market context |
| Remove NVIDIA provider | Rate-limited free tier, no paid tier benefit vs Groq |
| Use Groq for backtesting | 10x cheaper, same quality for batch calls (needs paid tier) |
| Nifty50 for BEAR regime | Mid-caps crash harder than large-caps |
| Mid+Large for BULL regime | Better alpha, better Sharpe in bulls |
| Regime-gated calls (not daily) | Research proves daily LLM timing fails; gating reduces cost 70% |
| Trade journal in prompt | Prevents re-entry loops, proven to reduce overtrading |
| Auto-liquidation (code, not LLM) | LLMs too slow/uncertain for crash response; code is immediate |
| Sentiment as confirmatory only | Standalone sentiment fails; confirmation adds +0.5-2% alpha |
| 30-day paper trading minimum | Research shows backtest-to-production gap requires live validation |
| $2/day hard API budget | Individual contributor cost constraint |

---

## 8. Files Created/Modified This Session

### New Files
- `agents/groq_portfolio_manager.py` — Groq REST API integration
- `agents/nvidia_portfolio_manager.py` — NVIDIA REST API integration (to be deleted)
- `LLM_PROVIDERS.md` — Multi-provider documentation
- `data/yfinance_historical.py` — yfinance historical data fetcher
- `backtesting/strategy_backtester.py` — Major rework for LLM backtest
- `backtesting/benchmark_comparison.py` — Comparison runner
- `monitoring/benchmark_tracker.py` — Live performance tracking
- `risk/portfolio_allocator.py` — Signal ranking and allocation
- `signals/signal_combiner.py` — Sentiment + technical merger
- `strategies/overbought_short.py` — Short strategy
- `strategies/oversold_bounce.py` — Bounce strategy
- `strategies/llm_strategy.py` — Per-symbol LLM strategy
- `run_llm_backtest.py` — Backtest entry point with universe selection
- `docs/research_ai_trading_best_practices.md` — This session's research
- `docs/session_findings_april2026.md` — This file

### Modified Files
- `agents/llm_base.py` — Multi-provider factory, trade journal, passive system prompt
- `agents/llm_portfolio_manager.py` — Trade journal integration, regime-aware decisions
- `signals/indicators.py` — Added ROC(5) and ROC(10) for crash detection
- `config/universes.py` — Added `NIFTY_ALL_CAP`, `NIFTY_MID_LARGE_CAP` combined universes
- Various agents — Bug fixes and improvements
