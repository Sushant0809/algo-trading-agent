# Trading Strategies

All 7 strategies inherit from `BaseStrategy` (`strategies/base.py`) and implement:

```python
def generate_signal(symbol: str, df: pd.DataFrame, mode: TradingMode) -> Optional[Signal]
```

`df` contains OHLCV bars with all indicators pre-computed by `signals/indicators.py`.

Parameters are loaded from `config/strategy_params.yaml` and can be tuned without code changes.

---

## Strategy 1: Momentum

**File:** `strategies/momentum.py`

### Logic

Captures stocks in strong uptrends using a multi-indicator filter stack.

### Entry Conditions

| Condition | Intraday (MIS) | Swing (CNC) |
|-----------|---------------|-------------|
| EMA stack | EMA(20) > EMA(50) > EMA(200) | Same |
| RSI | 50–70 (trending, not overbought) | Same |
| MACD | Histogram expanding (current > previous) | Same |
| Price vs VWAP | Price > VWAP | Not required |
| ADX | Not required | ADX > 25 (confirmed trend) |
| Timeframe | 5-minute bars | Daily bars |
| Product | MIS (auto square-off 3:15pm) | CNC (held overnight) |

### Exit Conditions
- Target: Entry + 2× ATR(14)
- Stop-loss: Entry − 1.5× ATR(14), placed as SL-M immediately
- Trailing stop: 1× ATR below highest close after entry

### Best Market Conditions
- Clear directional trend (Nifty ADX > 25)
- RSI of index 50–65
- Low VIX (< 15)

---

## Strategy 2: Mean Reversion

**File:** `strategies/mean_reversion.py`

### Logic

Buys oversold stocks when price touches the Bollinger lower band and RSI is in extreme oversold territory. Expects reversion to the mean.

### Entry Conditions

| Condition | Value |
|-----------|-------|
| Price | ≤ Bollinger lower band (20-period, 2σ) |
| RSI | < 30 (oversold) |
| Volume | > 1.5× 20-period average (confirms reversal interest) |
| Timeframe | 15-minute bars (intraday) or Daily bars (swing) |

**Extra filters for smallcaps:**
- Market cap > ₹500 crore (avoid illiquid traps)
- Average daily traded value > ₹50 lakh

### Exit Conditions
- Target: BB midline (20-period SMA)
- Stop-loss: Entry − 1.5× ATR(14)
- Exit also triggered if RSI > 50 (mean reversion complete)

### Best Market Conditions
- Sideways or range-bound market (ADX < 20)
- High BB width (volatile but not trending)
- VIX elevated (15–25)

---

## Strategy 3: Breakout

**File:** `strategies/breakout.py`

### Logic

Buys when price breaks out of a consolidation range with volume confirmation. Waits for one confirmation bar before entering to avoid false breakouts.

### Entry Conditions

| Condition | Value |
|-----------|-------|
| Price | Closes above 20-period rolling high |
| Volume | > 2× 20-period average volume |
| Confirmation | Wait 1 bar (next candle must not reverse) |
| Timeframe | 15-minute bars (intraday) or Daily bars (swing) |

### Exit Conditions
- Target: Breakout level + 2× ATR(14)
- Stop-loss: 20-period rolling low (below breakout base), placed as SL-M immediately

### Best Market Conditions
- After prolonged consolidation (low ATR period)
- Before/after earnings or major announcements
- Sector rotation events

---

## Strategy 4: Oversold Bounce

**File:** `strategies/oversold_bounce.py`

### Logic

Targets deep pullbacks in stocks that are oversold but have not broken their long-term trend. A more focused version of Mean Reversion with a MACD confirmation filter and EMA(200) support check.

### Entry Conditions

| Condition | Value |
|-----------|-------|
| RSI | < 30 (oversold zone) |
| MACD histogram | Turning up: current hist > previous hist (momentum shift) |
| Price | ≥ BB lower band (not in free fall — has support) |
| EMA(200) | Price > EMA(200) OR EMA(200) missing (avoids falling knives) |
| Volume ratio | > 0.8× (some participation; avoids dead stocks) |
| Timeframe | Daily bars |

### Exit Conditions
- Target: EMA(20) — RSI bounces typically mean-revert to moving average
- Stop-loss: Entry − 1.5× ATR(14)

### Position Sizing
Scales inversely with RSI depth:
- RSI 25–30 → smaller size (mild oversold, could go lower)
- RSI < 20 → larger size (deeply oversold, higher bounce probability)
- Base 2% of capital × scalar = (30 − RSI) / 30, capped at 1.0

### When NOT to use
- Price already below EMA(200) → avoid (classic falling knife)
- In CRASH/BEAR regime → signal is suppressed by regime universe filter

---

## Strategy 5: Overbought Short

**File:** `strategies/overbought_short.py`

### Logic

Mirror of Oversold Bounce on the short side. Targets stocks that are severely overbought and showing momentum reversal. **India allows intraday equity short selling only (MIS product).**

### Entry Conditions

| Condition | Value |
|-----------|-------|
| RSI | > 75 (overbought zone) |
| MACD histogram | Turning down: current hist < previous hist (momentum fading) |
| Price | ≤ EMA(20) — short-term trend already rolling over |
| Price | ≤ BB upper × 1.02 — not in a parabolic blow-off (can run further) |
| Volume ratio | > 0.8× |
| Mode | Always INTRADAY (equity short intraday-only in India) |

### Exit Conditions
- Target: EMA(50) — overbought stocks typically mean-revert to 50-day average
- Stop-loss: Entry + 1.5× ATR(14) (hard stop above entry)

### Position Sizing
Scales with RSI depth above 75:
- RSI 75–80 → smaller size
- RSI > 85 → larger size (higher reversal probability)
- Base 2% of capital × scalar = (RSI − 75) / 25, capped at 1.0

### When NOT to use
- In STRONG_BULL regime — overbought can stay overbought for weeks
- Stocks above EMA(200) in a clear uptrend

---

## Strategy 6: Sentiment Driven

**File:** `strategies/sentiment_driven.py`

### Logic

Uses Claude (LLM) to score NSE corporate announcements, filings, and news on a -10 to +10 scale. Only enters when:
1. Sentiment score ≥ 7 (strong positive catalyst)
2. Price hasn't already moved more than 3% (not chasing)
3. Price is above EMA(50) (trend filter)
4. `SignalCombiner` cross-validation passes (≥ 3/5 technical checks)

### Entry Conditions

| Condition | Value |
|-----------|-------|
| Sentiment score | ≥ 7/10 (Claude-rated) |
| Price move since news | < 3% (not already priced in) |
| Trend filter | Price > EMA(50) |
| Claude confidence | ≥ 0.5 |
| Tech cross-validation | ≥ 3/5 checks: RSI range, MACD, EMA(20), volume, ADX |

### Position Sizing
Position size scales with sentiment score:
- Score 7 → 70% of normal position size
- Score 8 → 80%
- Score 9 → 90%
- Score 10 → 100% (max position)

### What Claude Scores Highly (≥ 7)
- Earnings beats with significant upside surprise
- Major contract wins or order book additions
- Dividend announcements above expectations
- Strategic acquisitions / M&A with clear value
- Regulatory approvals for new products

### Exit Conditions
- Target: Entry + 3× ATR(14) (larger move expected from catalyst)
- Stop-loss: Entry − 1.5× ATR(14)
- Time exit: If catalyst effect not seen in 3 days (swing), exit

---

## Strategy 7: LLM Strategy

**File:** `strategies/llm_strategy.py`

### Logic

Claude Sonnet acts as a swing trade decision maker. Receives a compact 5-bar OHLCV table + key indicators and returns a structured action with confidence score.

**Role in pipeline:** Supplements technical strategies. Runs in SWING mode only (intraday latency is too high for LLM calls).

### Design Principles
1. Structured output enforced — prompt demands JSON; fallback = no signal
2. Only trades when Claude confidence ≥ 0.65 (configurable)
3. Uses the same pre-computed indicators (`compute_all_indicators()`)
4. Supports BUY (long CNC), SELL (short MIS), or HOLD
5. One API call per symbol per day (cached) — no repeated calls in scan loops
6. Temperature = 0.1 for deterministic, conservative output

### Entry Conditions
Claude evaluates:
- Last 5 bars of OHLCV
- RSI, MACD, EMA stack, ATR, ADX, VWAP
- Portfolio context (if holding already)
- Returns: `{action, confidence, stop_loss, target, reasoning}`

Minimum confidence threshold: 0.65 (skip if Claude is uncertain)

### Exit Conditions
- Uses Claude-provided `stop_loss` and `target`
- Falls back to: 7% hard stop, 25% target (regime-aligned default)

---

## Strategy Selection: Claude's Role

Every morning at 9:00am IST, Claude reviews the market regime (including FII flows and India VIX) and allocates weights across strategies:

```
regime = "trending"  →  momentum: 40%, breakout: 25%, oversold_bounce: 15%, sentiment: 10%, llm: 10%
regime = "sideways"  →  mean_rev: 40%, oversold_bounce: 20%, breakout: 15%, sentiment: 15%, llm: 10%
regime = "volatile"  →  mean_rev: 30%, overbought_short: 20%, sentiment: 25%, breakout: 15%, llm: 10%
```

Strategy weights influence position sizing. They do not block any strategy.

---

## Technical Indicators Used

All computed by `signals/indicators.py` using `pandas-ta`:

| Indicator | Parameters | Used By |
|-----------|-----------|---------|
| EMA | 9, 20, 50, 200 | Momentum, Sentiment, Oversold Bounce, Overbought Short |
| RSI | 14-period | Momentum, Mean Reversion, Oversold Bounce, Overbought Short |
| MACD | 12/26/9 | Momentum, Oversold Bounce, Overbought Short |
| Bollinger Bands | 20-period, 2σ | Mean Reversion, Oversold Bounce, Overbought Short |
| ATR | 14-period | All strategies (stop-loss sizing) |
| ADX | 14-period | Momentum (swing), Strategy Selector, Signal Combiner |
| VWAP | Session | Momentum (intraday), LLM Strategy |
| Volume SMA | 20-period | Breakout, Mean Reversion, Oversold Bounce |
| Rolling High/Low | 20-period | Breakout |

---

## Signal Flow

```
Market Analyst / Orchestrator swing cycle
    │
    │  generate_signal(symbol, df, mode)
    │
    ▼
Signal {
    symbol: "RELIANCE"
    action: BUY
    strategy: "oversold_bounce"
    mode: SWING
    product: CNC
    entry_price: 1431.50
    stop_loss: 1409.00
    target: 1475.00      ← EMA(20) target
    position_size_pct: 0.024   ← RSI-scaled
    confidence: 0.73
    indicators: {rsi: 24.1, macd_hist: -0.8, macd_hist_prev: -1.2}
}
    │
    │ (if sentiment-driven: SignalCombiner cross-validates first)
    ▼
PortfolioAllocator → ranks, deduplicates, respects cash floor
    │
    ▼
Risk Manager → approves → ApprovedSignal {approved_qty: 14}
    │
    ▼
Execution Agent → Paper: PAPER-00001 BUY 14 RELIANCE CNC @ 1431.50
                  Live:  Zerodha order #1234567890
```

---

## Backtest Promotion Gate

Before any strategy trades real money:

| Metric | Minimum |
|--------|---------|
| Sharpe Ratio | > 1.0 (out-of-sample) |
| Max Drawdown | < 15% |
| Win Rate | > 45% |
| Benchmark beat | 3/3 periods vs NIFTY50 |
| Test period | ≥ 2 years |

Run backtests with:
```bash
.venv/bin/python run_llm_backtest.py --short   # quick 3-period test
.venv/bin/python run_llm_backtest.py           # full 5-period test
```
