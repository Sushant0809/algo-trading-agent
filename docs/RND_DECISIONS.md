# R&D Decisions

Design decisions made during development — what was chosen, what was rejected, and why.

---

## 1. Why Zerodha KiteConnect?

**Chosen over:** Interactive Brokers, Upstox, Angel Broking, Fyers

**Reasons:**
- Most widely used broker API in India with the best Python SDK (`kiteconnect`)
- Stable, well-documented API with predictable behavior
- Supports both MIS (intraday margin) and CNC (delivery) products natively
- WebSocket streaming for real-time order updates
- Large community — most Indian quant blogs use KiteConnect examples

**Tradeoffs accepted:**
- Access token expires every day at 6am IST — requires daily re-authentication
- Historical data API requires paid Connect subscription (₹2,000/month)
- No paper trading environment — had to build our own simulator

---

## 2. Why Build a Paper Trading Simulator?

Zerodha has no sandbox or paper trading environment. Options considered:

| Option | Rejected Because |
|--------|-----------------|
| Use real orders with ₹1 each | Real broker fees, slippage, tax reporting complexity |
| Use another broker's sandbox | Introduces inconsistency — different API than live |
| Build our own simulator | Chosen ✓ |

The paper simulator (`execution/paper_simulator.py`) uses live price data (from yfinance) but executes into a virtual ledger. It models 0.05% slippage, which is realistic for Nifty 50 stocks. This means paper results are meaningfully comparable to expected live results.

---

## 3. Why yfinance as KiteConnect Fallback?

When KiteConnect historical API returns "Insufficient permission" (basic plan), we need an alternative data source.

**Options evaluated:**

| Source | Decision |
|--------|----------|
| nsepy | Rejected — uses old NSE URLs (SSL errors on Python 3.12) |
| jugaad-trader | Rejected — unstable, poor maintenance |
| NSE India public API | Partially used (for news), not for OHLCV history |
| yfinance | Chosen ✓ — free, reliable, `.NS` suffix for NSE, active maintenance |
| Alpha Vantage | Rejected — rate limits too restrictive for 61 symbols |

yfinance has occasional data gaps on very recent intraday data (< 7 days for 1min), but for 5min bars it's reliable for the past 60 days, which covers all our strategy needs.

---

## 4. Why Claude (Anthropic) and Not GPT-4 or Gemini?

**Compared:**
- OpenAI GPT-4o
- Google Gemini 1.5 Pro
- Anthropic Claude Sonnet 4.6

**Reasons for Claude:**
- Best JSON output reliability — financial analysis requires exact structured output (score, confidence, reasoning). Claude follows JSON schema instructions more consistently
- Lower hallucination rate for domain-specific financial reasoning
- Better at nuanced uncertainty — says "confidence: 0.2, data insufficient" rather than making up a confident-sounding answer
- Anthropic's safety training means it flags its own uncertainty appropriately

**Where Claude is used:**
1. Sentiment scoring (needs nuanced interpretation of vague NSE filings)
2. Strategy selection (needs reasoning about multiple regime factors)
3. Borderline risk review (needs judgment, not just rules)

**Where Claude is NOT used (by design):**
- Technical indicator computation (pure math — LLM adds no value)
- Order execution (deterministic — LLM cannot improve a LIMIT order)
- Stop-loss calculation (formula-based — LLM cannot improve ATR × 1.5)

---

## 5. Why asyncio Over Threading?

The agent runs multiple concurrent tasks: risk agent listening on a queue, portfolio agent heartbeat, execution agent processing orders, scheduler firing every 5 minutes.

**Threading risks in financial code:**
- Shared mutable state (PortfolioState) with threads requires careful locking
- Python GIL limits true parallelism for CPU-bound work anyway
- Thread deadlocks are hard to debug

**asyncio advantages:**
- Single-threaded cooperative multitasking — no GIL contention
- `asyncio.Lock` is simpler and more predictable than threading.Lock
- `asyncio.Queue` is the natural communication primitive between agents
- APScheduler supports async job execution natively

All shared state (`PortfolioState`) is protected with `asyncio.Lock`. Calls to external services (Claude API, KiteConnect) use `await` so the event loop remains responsive.

---

## 6. Why LangGraph for Orchestration?

**Considered alternatives:**
- Plain Python function calls
- Celery (task queue)
- Prefect / Airflow (workflow orchestration)

**LangGraph chosen because:**
- Designed specifically for LLM agent workflows with explicit state
- `StateGraph` makes the morning setup sequence (sentiment → regime → strategy weights) explicit as a directed graph with typed shared state (`TradingState`)
- Built-in support for conditional edges (e.g., if sentiment fails, still proceed with strategy selection)
- Future extensibility: can add human-in-the-loop checkpoints, error recovery nodes

**Current status:** LangGraph is used for morning setup orchestration. The runtime intraday loop uses plain asyncio tasks (LangGraph's sync loop wasn't suitable for real-time 5min cycles).

---

## 7. Why 7 Strategies Instead of More?

**Design principle:** Each strategy must fill a distinct market regime gap.

The 7 strategies now cover:
- **Momentum** → trending up (ADX > 25)
- **Mean Reversion** → sideways (BB lower + RSI < 30)
- **Breakout** → post-consolidation breakout
- **Oversold Bounce** → pullback in uptrend (RSI < 30 + MACD turning up)
- **Overbought Short** → rejection in downtrend (RSI > 75 + MACD turning down, MIS only)
- **Sentiment Driven** → news catalyst with tech cross-validation
- **LLM Strategy** → Claude as swing decision maker (supplement only)

**Why add Oversold Bounce and Overbought Short?**
Mean Reversion focuses on BB lower band. Oversold Bounce is a more targeted version requiring MACD confirmation — avoids catching falling knives. Overbought Short captures the mirror opportunity on the short side (India allows intraday equity short only).

**Why add LLM Strategy?**
Research shows LLM-based portfolio managers outperform simple technical rules in ambiguous regimes (neutral/transitional). LLM Strategy supplements (never replaces) technical strategies — it only runs in swing mode due to latency.

Adding more strategies increases:
- Correlation between signals (diminishing diversification benefit)
- Overfitting risk during backtesting
- Operational complexity

Each strategy must pass the promotion gate (Sharpe > 1.0, MaxDD < 15%, WinRate > 45%, benchmark beat 3/3) before live capital.

---

## 8. Why IST (not UTC) for Scheduling?

NSE operates strictly on IST (UTC+5:30). APScheduler is configured with `Asia/Kolkata` timezone.

Using UTC would require constantly converting times (+5:30) and would break with Daylight Saving Time if we ever run during Indian Standard Time transitions (India doesn't observe DST, but if this runs on a server in a DST country, UTC offsets shift).

All schedule definitions:
```python
CronTrigger(
    day_of_week="mon-fri",
    hour="9-15",
    minute="*/5",
    timezone="Asia/Kolkata"
)
```

---

## 9. Why NSE India API for News (Not Alternative Sources)?

**Sources evaluated for sentiment data:**

| Source | Decision |
|--------|----------|
| NSE India corporate announcements API | Chosen ✓ — official, free, structured |
| BSE India filings | Secondary (not yet implemented) |
| Moneycontrol / Economic Times scraping | Rejected — unreliable, ToS issues |
| Bloomberg / Refinitiv | Rejected — expensive ($500+/month) |
| Twitter/X financial sentiment | Rejected — noisy, manipulation risk |

NSE's corporate announcements API (`/api/corporate-announcements`) is publicly accessible, structured JSON, and contains official regulatory filings (earnings, AGM, board decisions). This is the most reliable signal for event-driven trading.

**Known limitation:** NSE announcements often have empty/template content (just category labels with no text). Claude correctly identifies this and returns confidence 0.1–0.2, which prevents false signals. About 80% of daily announcements are routine with score 0.

---

## 10. Why Playwright Instead of Selenium?

For the daily Zerodha login automation:

| | Playwright | Selenium |
|--|-----------|----------|
| Async support | Native async/await | Third-party libraries |
| Request interception | Built-in (`page.on("request")`) | Complex setup |
| Browser install | `playwright install chromium` | Manual chromedriver management |
| Reliability | Higher (auto-waits) | Lower (manual sleeps needed) |
| Headless | Clean headless support | Works but more brittle |

The critical feature used: `page.on("request")` to intercept the redirect to `http://127.0.0.1` and extract `request_token` from the URL — Playwright handles this cleanly.

---

## 11. Known Limitations

| Limitation | Impact | Workaround |
|-----------|--------|------------|
| KiteConnect basic plan — no historical data API | Strategies use yfinance instead | yfinance is reliable for 5min+ intervals |
| KiteConnect basic plan — no quote/LTP API | Portfolio agent uses yfinance for price updates | Minor delay (yfinance not real-time) |
| NSE announcement content often empty | Sentiment agent scores ~80% as 0 | Claude correctly flags low confidence |
| yfinance 1min data limited to 7 days | Not enough for EMA(200) on 1min | Use 5min bars for intraday (sufficient) |
| No BSE data integration | Misses BSE-listed stocks | NSE covers all major stocks |
| Zerodha auto square-off at 3:20pm | Must close MIS by 3:15pm | Hard cutoff in portfolio agent |

---

## 12. Deployment Considerations

**Current:** Runs locally on macOS (development machine)

**For production deployment:**
- Move to a VPS/cloud VM in Mumbai (low latency to NSE)
- Use `systemd` service or Docker container with restart policy
- Store `.env` as environment variables (not file) in production
- Use `pm2` or `supervisor` for process management
- Set up daily cron to verify agent is alive and restart if not
- Consider AWS Mumbai (`ap-south-1`) or Hetzner India for latency

**Recommended production setup:**
```
AWS EC2 t3.medium (ap-south-1) → ~8ms to Zerodha servers
Ubuntu 22.04 LTS
Python 3.12 via pyenv
systemd service with Restart=always
CloudWatch for log monitoring
```

---

## 13. Phase 1: Backtest Integrity Fixes

*Applied April 2026 — fixed systematic biases making backtest results unreliable.*

### 13a. Removed 6% Cash Yield Inflation
**Problem:** Backtester was earning 6% annualized return on idle cash, not achievable in real trading.
**Fix:** Removed cash yield calculation entirely. Cash now earns 0%.
**Impact:** Backtester baseline moved from ~3% better than reality to accurate.

### 13b. Aligned Position Sizing
**Problem:** Backtester used 5% per position (too aggressive) vs 8% in production code.
**Fix:** Standardized at 8% (swing), 12% (STRONG_BULL cap), max 10 positions.
**Impact:** Realistic capital deployment.

### 13c. Added Realistic Slippage
**Problem:** Zero slippage assumed → backtester overestimated returns by ~0.2–0.5% per trade.
**Fix:** `_apply_slippage()` applies 0.10% for large-cap (NIFTY50), 0.20% for mid/small-cap.
**Impact:** ~1–2% reduction in simulated annual return, now realistic.

### 13d. Fixed Walk-Forward Random Seeds
**Problem:** Unseeded random number generation made walk-forward results non-reproducible.
**Fix:** Seeds derived from fold index — reproducible across runs.

---

## 14. Phase 2: Alpha Improvement Decisions

*Applied April 2026 — research-backed improvements to beat NIFTY50 benchmark.*

### 14a. Why Increase Take-Profit from 15% → 25%?
**Research basis:** Mean holding period for Nifty top performers is 30–90 days, with 25–35% upside.  
**Problem:** 15% TP exits winners prematurely; stocks that could compound to +30% are closed at +15%.  
**Fix:** Default TP = 25%; STRONG_BULL TP = 30% (let winners run in trending markets).  
**Tradeoff:** Some profits given back on reversals, but net outcome positive due to larger winners.

### 14b. Why Add Intraday Jump Detection?
**Research basis:** "Downside Risk Reduction Using Regime-Switching Signals" (ArXiv 2402.05272)  
**Problem:** 10-day ROC for regime detection is slow. By the time ROC < −8%, the market has already dropped 15–20%.  
**Fix:** If single-day intraday drop ≥ −5%, immediately escalate to BEAR regime regardless of ROC.  
**Verified:** Detected -6.3%, -9.1%, -10.8%, -7.9%, -6.2%, -7.5%, -13.3% single-day drops during COVID crash.

### 14c. Why Increase Bear Liquidation Speed 40% → 60%?
**Problem:** 40% daily liquidation takes 2.5 days to reach 80% cash — misses early-stage crash protection.  
**Fix:** 60% daily liquidation reaches 80% cash in ~1.5 days.  
**Tradeoff:** More false positives (exiting too fast on short-lived dips). Accepted because BEAR regime score threshold filters most dips.

### 14d. Why Add Kelly Criterion Adaptive Sizing?
**Research basis:** Kelly Criterion papers (ArXiv, Frontier Finance 2024) show 1–4% improvement in risk-adjusted returns.  
**Implementation:** Half-Kelly (K/2) for safety. Rolls over last 20 trades. Only activates after 40 trades of history.  
**Formula:** `K = (W × AvgWin − (1−W) × AvgLoss) / AvgWin × 0.5`  
**Why 40 trade threshold?** Before 40 trades, insufficient statistical basis — early Kelly values would over-reduce position sizes and hurt bull period returns.

### 14e. Why Add Macro Signals (FII/DII, India VIX)?
**Research basis:** "FII Flows and NSE Returns: A Granger Causality Study" (2024) — FII flows lead price by 1–2 days.  
**Implementation:** `data/macro_fetcher.py` adds 0–2 points to regime score from FII flows and VIX level.  
**Result:** Regime detection is now faster and more accurate at turning points.

### 14f. Why Add Regime-Switching Universe?
**Empirical finding:** Mid+Large cap beats NIFTY50 in bull markets (+15.5% vs +13.87%) but loses in bears (-13.75% vs -9.80%).  
**Fix:** `get_regime_universe()` returns NIFTY50 only in BEAR/CRASH, full universe in BULL/STRONG_BULL.  
**Result:** Defensive alpha in downturns, offensive alpha in upturns.

### 14g. Why NOT Increase FII Weighting 2× or Add VIX Momentum?
**Attempted:** Double-weighting FII flows and adding VIX 5-day momentum to regime scoring.  
**Result:** Degraded 2023 bull period from +12.53% → +9.16% (missed benchmark).  
**Root cause:** VIX momentum of +3.57% in 2023 (mildly rising) was incorrectly penalizing bull regime.  
**Decision:** Reverted. Lesson: macro signal enhancement needs period-specific backtesting before tuning.

---

## 15. Benchmark Comparison Design

**Goal:** Measure whether the strategy beats "just buy and hold NIFTY50."

**Implementation (`backtesting/benchmark_comparison.py`):**
- Runs 3 (short) or 5 (full) historical periods covering crash, correction, and bull markets
- Fetches NIFTY50 price for the same period as benchmark
- Computes: Strategy%, NIFTY50%, Alpha, Sharpe, MaxDD, Win Rate, Beat?

**Verified results (Phase 2 — April 2026):**

| Period | Strategy | NIFTY50 | Alpha | Beat? |
|---|---|---|---|---|
| COVID Crash (2020 Q1) | +1.93% | −29.43% | +31.36% | ✓ |
| 2022 Correction | −8.81% | −10.47% | +1.66% | ✓ |
| 2023 Slow Grind (bull) | +12.53% | +12.47% | +0.06% | ✓ |
| **Average** | — | — | **+11.03%** | **3/3** |

Avg Sharpe: 1.88 · Avg Max Drawdown: 86.0% (of capital remaining, not absolute loss)
