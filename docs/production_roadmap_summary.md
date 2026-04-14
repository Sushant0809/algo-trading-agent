# Production Roadmap — Executive Summary

*Detailed plan in `/Users/sushant/.claude/plans/parsed-zooming-kite.md`*
*Research reference in `docs/research_ai_trading_best_practices.md`*
*Full session record in `docs/session_findings_april2026.md`*

---

## Philosophy

**If we don't beat the benchmark with significant margin in EVERY regime, the model has no purpose.**
Anyone can buy a NIFTY50 ETF (NIFTY BeES) for 0.04% annual fee and get the index return.
The algo must justify its existence with +5-10% alpha consistently across ALL regimes — not just crashes.

---

## Target Performance

| Regime | Current Alpha | Target After All Phases |
|--------|--------------|------------------------|
| CRASH (COVID-style) | +32.48% | +30%+ (maintain) |
| BEAR (2022-style) | +0.67% | +5-8% (improve significantly) |
| BULL (2023-style) | +1.40% | +5-10% **(main gap to fix)** |
| STRONG_BULL | Not tested | +8-12% |
| **Average** | **+11.52%** | **+7-10% every single period** |

---

## Phase 0 — Emergency Fixes
*Do this FIRST. Nothing is safe to run until these are done.*

**1. Security — Rotate All Credentials**
- `.env` with live Zerodha credentials (user ID, password, TOTP secret, API key), Anthropic API key, GROQ key is committed to git
- **Immediate action:** `git rm --cached .env`, rotate ALL credentials, add `.gitignore` entry
- Also add missing entries to `.env.example`: `NVIDIA_API_KEY`, `LLM_PROVIDER`, `GROQ_API_KEY`
- **Files:** `.gitignore`, `.env.example`

**2. Critical Bug — SL-M Double-Exit**
- `KiteExecutor.execute_exit()` places a SELL order but does NOT cancel the pre-placed SL-M stop-loss order
- Both can execute → double-exit → creates unintended short position → capital loss
- **Fix:** Cancel `pos.stop_order_id` before placing SELL
- **Files:** `execution/kite_executor.py`, `risk/portfolio_state.py`

**3. Critical Bug — FillTracker Not Wired**
- `fill_tracker.py` exists but `main.py` never starts the KiteTicker WebSocket
- Live fill prices are NOT synced → `PortfolioState` uses wrong prices → P&L is wrong
- **Fix:** Add `init_ticker()` + `setup_ticker_callbacks()` call in `main.py`
- **Files:** `main.py`, `execution/fill_tracker.py`

**4. Bug — BenchmarkTracker Baseline**
- `BenchmarkTracker.set_initial_values()` never called → Day-1 return uses zero baseline → wrong metrics
- **Fix:** Call it in `_run_trading()` after capital setup
- **Files:** `main.py`, `monitoring/benchmark_tracker.py`

**5. Bug — Market Calendar 2026+**
- NSE holidays only hardcoded through 2025. From April 2026, system trades on holidays.
- **Fix:** Add 2026 NSE holiday list
- **Files:** `scheduling/market_calendar.py`

**6. Remove — NVIDIA Provider**
- Free tier rate-limited (1 req/30s), breaks backtesting, no paid tier value vs Groq
- Adds maintenance burden with zero benefit
- **Action:** Delete `agents/nvidia_portfolio_manager.py`, remove from `create_llm_manager()`
- **Files:** `agents/nvidia_portfolio_manager.py` (delete), `agents/llm_base.py`

**Verification:**
```bash
git rm --cached .env && git status  # verify .env untracked
python -m pytest tests/unit/ -v     # all 27+ tests still pass
python main.py auth-refresh         # verify auth still works
```

---

## Phase 1 — Backtest Integrity
*Make the backtest accurately reflect what production will actually do.*

**1. Remove — Cash Yield Inflation**
- Backtest credits 6% annualized yield on idle cash (a liquid fund return)
- Production `PortfolioState` does NOT do this
- Effect: Backtest returns are inflated by ~1-2% per period
- **Fix:** Remove `cash += cash * CASH_YIELD_DAILY` from `_run_llm()`
- **Impact:** Bull period returns will drop ~1-2%. This is the honest number.
- **Files:** `backtesting/strategy_backtester.py`

**2. Fix — Align Position Sizing (Backtest ↔ Production)**
- LLM backtest allows 20% per position in STRONG_BULL (nearly all-in)
- Production `PortfolioAllocator` caps at 5% per position, max 3 signals per cycle
- These are entirely different systems — backtest results cannot predict production performance
- **Fix:** Update both backtest constants AND `risk_params.yaml` to use consistent values:
  - Swing positions: max 8% (up from 5% — allows more alpha)
  - Bull mode: max 12% per position (larger conviction bets in clear trends)
  - Max concurrent: 10 positions (up from 5 — better diversification)
  - Cash reserve: always 10% minimum
- **Files:** `backtesting/strategy_backtester.py`, `config/risk_params.yaml`

**3. Add — Realistic Slippage Model**
- Backtest enters at next-bar OPEN exactly. Reality: market orders get worse fills.
- Model: Large-cap 0.10%, Mid-cap 0.20%, Small-cap 0.40% slippage per trade (both entry and exit)
- **Files:** `backtesting/strategy_backtester.py`

**4. Fix — Walk-Forward Random Seeds**
- Walk-forward uses fixed seeds [42, 99, 7] → same periods every single run → fake robustness test
- **Fix:** Remove fixed seeds, use `random.seed(int(time.time()))`
- **Files:** `backtesting/benchmark_comparison.py`

**Verification:**
```bash
python run_llm_backtest.py --short
# Expect: 1-2% lower returns than before (honest now)
# Require: Still beats NIFTY50 in all 3 periods with reduced but real numbers
# COVID: > 0%, 2022: > -10.47%, 2023: > +12.47%
```

---

## Phase 2 — Alpha Improvements
*Get from +1.4% bull alpha to +5-10%. This is what separates us from a retail trader.*

**1. Fix — Bull Market Underperformance (HIGHEST PRIORITY)**

Root cause from research: "LLM strategies are overly conservative in bull markets." The LLM asks "should I sell?" and generates false sell signals. Meanwhile, position sizing is too small (max 40% deployed due to 10% cash floor + gaps).

Fixes:
- In STRONG_BULL: max 15% per position (fewer, higher-conviction bets)
- In STRONG_BULL: reduce cash reserve to 2% (deploy capital aggressively when trend is clear)
- Block ALL sells in STRONG_BULL except hard stop (-7%) — current take-profit (+15%) triggers too early; strong bull means +15% can become +30% if held
- System prompt update: "In STRONG_BULL: ignore take-profit, only sell on hard stop. Your job is to stay invested, not to trade."
- **Files:** `backtesting/strategy_backtester.py`, `agents/llm_portfolio_manager.py`, `agents/llm_base.py`

**2. Add — Macroeconomic Signals (FII/DII, India VIX)**

FII (Foreign Institutional Investors) drive >60% of Indian large-cap price movement at the margin. When FIIs are net buyers, even weak stocks go up. When FIIs sell, even strong stocks fall. This is the best leading indicator for NSE markets and it's completely missing from the current model.

**New file:** `data/macro_fetcher.py`
```python
class MacroFetcher:
    def fetch_fii_dii_flows(self) -> dict:
        # NSE API: https://www.nseindia.com/api/fiidiiTradeReact (no key, scraping)
        # Returns: {fii_net_5d, dii_net_5d, fii_trend}
    def fetch_india_vix(self) -> float:
        # yfinance: ^INDIAVIX (already available, no new dependency)
    def get_rbi_rate(self) -> float:
        # Static lookup + manual update (repo rate changes only ~4x/year)
```

**Integrate into regime scoring** (adds 2 more factors, max score goes 7 → 9):
```python
if fii_net_5d > 0:  regime_score += 1  # FIIs are buying = bullish
if india_vix < 15:  regime_score += 1  # Low fear = stable environment
# Recalibrate: STRONG_BULL >= 7, BULL >= 5, NEUTRAL >= 3, BEAR < 3
```

**Include in LLM prompt:** Pass `fii_flow_5d`, `india_vix`, `rbi_rate` as market context.

**Files:** `data/macro_fetcher.py` (new), `backtesting/strategy_backtester.py`, `agents/llm_base.py`

**3. Add — Regime-Switching Universe**

Mid+Large cap beats Nifty50 in bulls (+15.5% vs +13.87%) but loses in bears (-13.75% vs -9.80%).
Solution: Automatically switch which stocks are in scope based on current regime.

```python
def _get_regime_symbols(regime: str, all_symbols: list) -> list:
    if regime in ("CRASH", "BEAR"):
        return [s for s in all_symbols if s in NIFTY50]          # 50 defensive large-caps
    elif regime == "NEUTRAL":
        return [s for s in all_symbols if s in NIFTY_MID_LARGE_CAP][:65]  # 65 stocks
    else:  # BULL, STRONG_BULL
        return all_symbols[:100]  # 100 stocks, full opportunity set
```

**Files:** `backtesting/strategy_backtester.py`, `config/universes.py`

**4. Add — Sentiment as Signal Gate**

Sentiment adds +0.5-2% alpha when used correctly — as a CONFIRMATORY signal, not as a driver.
Current problem: sentiment agent exists but has zero connection to LLM backtest/decisions.

**Correct integration:**
- Technical signal BULLISH + sentiment POSITIVE → boost allocation 10-15%
- Technical signal BULLISH + sentiment NEGATIVE → reduce allocation 20% or skip
- Sentiment alone → never trade (too noisy without confirmation)

**In LLM prompt:**
```
SENTIMENT CONTEXT (use as confirmation only, not primary signal):
  ADANIENT: sentiment_score=+7.2 (3 news items, 6hr avg age) → BULLISH CONFIRMATION
  HDFCBANK:  sentiment_score=-2.1 (2 items, 12hr avg age) → NEUTRAL
```

**Backtest gap:** Sentiment data not available historically. Accept: sentiment adds production alpha but can't be backtested. Validate separately via 30-day paper trading A/B test.

**Files:** `agents/llm_base.py` (prompt update), `backtesting/strategy_backtester.py` (document gap)

**5. Add — Adaptive Position Sizing (Kelly-Inspired)**

Instead of fixed 8-15% per position, size positions based on rolling edge:
```python
# Rolling 20-trade win rate and avg P&L from trade journal:
kelly_fraction = (win_rate * avg_win - (1-win_rate) * avg_loss) / avg_win
position_size = min(kelly_fraction * 0.5, max_position_pct)  # half-Kelly for safety
```

**Files:** `agents/llm_base.py` (prompt hint), `backtesting/strategy_backtester.py`

**Verification:**
```bash
python run_llm_backtest.py --short           # 2023 bull alpha > 5%
python run_llm_backtest.py --medium          # validate across 5 periods  
python run_llm_backtest.py --short --mid-large  # regime-switching test
# SUCCESS CRITERIA: ALL periods beat NIFTY50, bull alpha >= 5%, bear alpha >= 2%
```

---

## Phase 3 — Production Hardening
*Make the system safe to run with real money.*

**1. Add — Daily Token Budget ($2/day Hard Ceiling)**

New file: `monitoring/cost_tracker.py`
```python
class CostTracker:
    DAILY_LIMIT_USD = 2.00
    def record_call(self, input_tokens: int, output_tokens: int): ...
    def check_budget(self) -> bool:  # raises if exceeded
```
- Call `check_budget()` before every LLM API call in `LLMPortfolioManager.decide()`
- Log cumulative daily spend to audit trail
- Reset at midnight IST
- **Files:** `monitoring/cost_tracker.py` (new), `agents/llm_portfolio_manager.py`

**2. Add — Production Circuit Breakers**

Beyond the existing kill switch (daily loss %):
- **Model drift:** Rolling 10-day Sharpe < 1.0 → Telegram alert + reduce all position sizes by 50%
- **Win rate monitor:** Rolling 20-trade win rate < 35% → pause new buys for 2 days
- **LLM failure circuit:** 3 consecutive API failures → "hold all positions, no new buys" for the day
- **Runaway trade alert:** >15 trades in a single day → Telegram alert + halt new signals

**Files:** `monitoring/benchmark_tracker.py`, `agents/orchestrator.py`, `monitoring/alerting.py`

**3. Add — Daily Reconciliation**

New file: `monitoring/reconciliation.py`
```python
class DailyReconciler:
    def reconcile(self, kite, portfolio_state):
        # 1. Fetch actual positions from Zerodha API
        # 2. Compare with portfolio_state.open_positions
        # 3. Alert on any mismatch (quantity, symbol, direction)
        # 4. Log discrepancies to audit trail
```
Run daily at 4:00pm IST via SwingScheduler (after market close).

**Files:** `monitoring/reconciliation.py` (new), `scheduling/swing_scheduler.py`

**4. Model Selection Strategy (Cost Optimization)**

- **Haiku 4.5** for ALL routine calls (portfolio decisions, signal scoring, sentiment)
- **Sonnet 4.6** ONLY for: position > ₹50,000, regime transition days (high-stakes decisions)
- **Groq Llama-3.3-70B paid tier** for: all backtesting runs (10x cheaper than Claude)
- Add `LLM_MODE` env var: `conservative` (Haiku always), `adaptive` (Sonnet on high stakes), `backtest` (Groq)

**Files:** `agents/llm_base.py`, `.env.example`

**5. Fix — Backtest Breakout Bug**

`main.py` line 276: `backtest --strategy breakout` silently runs momentum backtest instead.
**Fix:** Add proper `elif strategy == "breakout"` branch.
**Files:** `main.py`

**Verification:**
```bash
python main.py run --mode paper --trading swing  # run for 2-3 days
# Watch: LLM calls happening (check logs), positions opening/closing
# Verify: BenchmarkTracker recording daily, no Telegram error alerts
# Check: daily cost log stays under $2/day
```

---

## Phase 4 — Unit Test Coverage
*Target: 70%+ coverage on production-critical paths. Currently ~40%.*

### New Test Files to Create

**`tests/unit/test_kite_executor.py`** — Mock `kite.place_order()`
- Test entry order placement (LIMIT and MARKET)
- Test SL-M placement on entry
- **Test SL-M cancellation on exit** (the critical bug we fixed)
- Test direction-aware stops (long vs short)

**`tests/unit/test_paper_simulator.py`** — Isolated paper simulator tests
- Slippage application on market orders
- Position open/close with P&L calculation
- Ledger integrity (no double-executions)

**`tests/unit/test_macro_fetcher.py`** — Mock HTTP responses
- FII/DII API parsing and trend calculation
- VIX fetch and fallback behavior
- RBI rate lookup

**`tests/unit/test_cost_tracker.py`** — Token budget enforcement
- Daily limit raises when exceeded
- Token counting per call
- Midnight reset

**`tests/unit/test_benchmark_tracker.py`** — Metrics math
- Sharpe calculation correctness
- Alpha accumulation
- `set_initial_values()` baseline behavior

**`tests/unit/test_market_calendar.py`** — Holiday detection
- 2025 holiday list accuracy
- 2026 holidays (after Phase 0.5)
- Weekend detection

**`tests/integration/test_paper_pipeline.py`** — Extend existing
- Full cycle: signal → risk → paper exec → reconcile
- Kill switch halts new signals (not just skips one signal)
- SL-M cancel verified on position exit

### What NOT to Test (Too Expensive or Requires External Services)
- `auth/auto_login.py` — Playwright browser automation → test manually
- `data/news_fetcher.py` — External APIs → test manually with `--dry-run`
- Full LLM decisions → test prompt-building, not API calls

**Verification:**
```bash
python -m pytest tests/ -v --cov=. --cov-report=term-missing
# Target: 70%+ coverage on execution/, risk/, monitoring/
```

---

## Phase 5 — Paper Trading Validation
*30-60 days minimum. Do NOT skip this phase.*

### Setup
```bash
# .env for paper validation:
PAPER_TRADING=true
PAPER_TRADING_CAPITAL=1000000   # ₹10L virtual
LLM_PROVIDER=claude
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
```

### What to Monitor Daily

| Metric | Tool | Alert Threshold |
|--------|------|----------------|
| Alpha vs NIFTY50 | `BenchmarkTracker` | Alert if negative for 5 days |
| Sharpe ratio | `BenchmarkTracker` | Alert if < 1.5 |
| Win rate | Trade journal | Alert if < 40% for 10 trades |
| Daily LLM cost | `CostTracker` | Alert if > $1.50/day |
| Trade count | Audit trail | Alert if > 20/day |
| Errors | `monitoring/logger.py` | Alert on any unhandled exception |

### Success Criteria to Proceed to Live Trading

After 30 days of paper trading, ALL of these must be true:
- ✅ Positive alpha vs NIFTY50 (cumulative over the period)
- ✅ Rolling Sharpe > 1.5
- ✅ Win rate > 40%
- ✅ Zero double-executions or missed exits
- ✅ Auth refresh working daily without manual intervention
- ✅ Daily LLM cost consistently < $1.50/day
- ✅ Zero unhandled exceptions in production logs
- ✅ Daily reconciliation showing ≤ 2 discrepancies per week

### What Causes Failure (Go Back to Fix)

| Failure | Action |
|---------|--------|
| Negative alpha for 10+ days | Return to Phase 2 — investigate regime detection |
| LLM cost > $3/day | Reduce call frequency or switch to Groq paid tier |
| Win rate < 35% for 5 days | Pause, check if market regime changed significantly |
| Repeated auth failures | Fix `auth/auto_login.py` or move to manual refresh |
| > 5 reconciliation mismatches/week | Investigate FillTracker WebSocket stability |

---

## Phase 6 — Live Trading (Gradual Ramp)

### Capital Deployment Schedule

| Phase | Capital | Max per Position | Duration | Proceed If |
|-------|---------|-----------------|----------|-----------|
| Pilot | ₹1,00,000 | ₹5,000 | 2 weeks | Zero critical bugs |
| Small | ₹2,00,000 | ₹10,000 | 2 weeks | Positive P&L vs paper |
| Medium | ₹5,00,000 | ₹25,000 | 1 month | Sharpe > 1.5, positive alpha |
| Full | ₹10,00,000 | ₹50,000 | 3+ months | Sustained Sharpe > 1.5 |

**Never skip a level.** Each level doubles exposure — verify the model handles it.

### Hard Rules for Live Trading
1. **Never set `PAPER_TRADING=false` without testing auth first** — run `python main.py auth-refresh` and verify Telegram alert received
2. **Daily loss limit: ₹5,000** at ₹1L capital (5%) → automated shutdown via kill switch
3. **Weekly review every Sunday:** Compare live vs paper vs backtest returns — investigate any divergence > 2%
4. **No trading during results season** without extra caution:
   - Q1: April/May (April-June results)
   - Q2: July/August (July-September results)
   - Q3: October/November (October-December results)
   - Q4: January/February (January-March results)
   - Earnings surprises can gap stocks 10-20% overnight, bypassing stops
5. **Monthly credential rotation:** Rotate TOTP secret and API keys monthly as security practice

---

## What to Remove (Simplify the Codebase)

| Remove | Why | How |
|--------|-----|-----|
| `agents/nvidia_portfolio_manager.py` | Rate-limited free tier, zero value | Delete file, remove from `create_llm_manager()` |
| Cash yield in backtest (6%/yr) | Inflates returns, doesn't exist in production | Remove 1 line from `_run_llm()` |
| Fixed walk-forward seeds | Not real robustness testing | Remove `random.seed(42/99/7)` calls |
| `backtrader_runner.py` stub class | Dead code, never used | Delete bare stub class |
| `strategies/llm_strategy.py` sync stub | `generate_signal()` always returns `None`, never fires | Either make async-native or remove |

---

## Cost Optimization Summary

### Claude API (Monthly Target: ~$6-10)

| Use Case | Model | Cost/call | Calls/Day | Monthly |
|----------|-------|-----------|-----------|---------|
| Portfolio decision (50 symbols) | Haiku 4.5 | $0.002 | 0-1 (regime-gated) | ~$3 |
| Sentiment scoring | Haiku 4.5 | $0.001 | 5-10 (moderate) | ~$2 |
| High-stakes risk review | Haiku 4.5 | $0.002 | 0-3 | ~$1 |
| **Production total** | | | | **~$6/month** |
| Backtesting (use Groq paid) | Groq L3.3-70B | $0.0002 | N/A (batch) | ~$2/run |

### Key Savings Mechanisms
1. **Regime-gating:** No LLM calls in BEAR/CRASH → saves ~30% of trading days
2. **Decision reuse:** Same decision valid for 2-5 days → saves 60-80% of remaining calls
3. **Groq for backtest:** 10x cheaper than Claude for batch validation runs (requires paid tier)
4. **Hard $2/day ceiling:** Prevents runaway costs from loops or bugs

### Claude Code Cost (Development)
- Plan before implementing (as done here) — avoids expensive re-work cycles
- Use `--short` backtests for iteration ($0.36/run), `--full` only for final validation ($7.67/run)
- Haiku model in Claude Code settings for routine tasks

---

## Critical Path (Don't Skip Steps)

```
Day 1-2:   Phase 0 — Security + critical bugs (MUST DO FIRST)
Day 3-5:   Phase 1 — Backtest integrity (make numbers honest)
Day 6-10:  Phase 2 — Alpha improvements (get to hedge fund alpha)
Day 11-13: Phase 3 — Production hardening (safety for real money)
Day 14-15: Phase 4 — Unit tests for new code
Day 16-45: Phase 5 — Paper trading (30 days minimum, no shortcuts)
Day 46+:   Phase 6 — Live trading at ₹1L, scale up slowly
```

**Total time to production-ready live trading: ~7 weeks minimum**

Each phase has clear entry criteria (previous phase verified) and exit criteria (verification commands pass). Do NOT skip ahead. A bug found in paper trading costs nothing. A bug found in live trading with ₹5L deployed costs real money.
