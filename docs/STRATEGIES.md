# Trading Strategies

All 4 strategies inherit from `BaseStrategy` (`strategies/base.py`) and implement:

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

### Works Best On
- Nifty 50 stocks (liquid, trending)
- BankNifty components

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

### Works Best On
- Mid and small-cap stocks (more volatile, revert faster)
- Sector stocks during sector-wide selloffs

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

### Works Best On
- Midcap 150 stocks (larger moves)
- Sector-specific events (e.g., budget day, RBI policy)

---

## Strategy 4: Sentiment Driven

**File:** `strategies/sentiment_driven.py`

### Logic

Uses Claude (LLM) to score NSE corporate announcements, filings, and news on a -10 to +10 scale. Only enters when:
1. Sentiment score ≥ 7 (strong positive catalyst)
2. Price hasn't already moved more than 3% (not chasing)
3. Price is above EMA(50) (trend filter)

### Entry Conditions

| Condition | Value |
|-----------|-------|
| Sentiment score | ≥ 7/10 (Claude-rated) |
| Price move since news | < 3% (not already priced in) |
| Trend filter | Price > EMA(50) |
| Confidence | Claude confidence ≥ 0.5 |

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

### What Claude Scores Low (< 3)
- Routine compliance filings
- AGM/EGM notices
- Director changes (unless CEO/CFO)
- Boilerplate "Update" announcements

### Exit Conditions
- Target: Entry + 3× ATR(14) (larger move expected from catalyst)
- Stop-loss: Entry − 1.5× ATR(14)
- Time exit: If catalyst effect not seen in 3 days (swing), exit

### Works Best On
- Any Nifty 50 / Midcap 150 stock with a genuine news catalyst
- Earnings season (Jan, Apr, Jul, Oct)

---

## Strategy Selection: Claude's Role

Every morning at 9:00am IST, Claude reviews the market regime and allocates weights across strategies:

```
regime = "trending"  →  momentum: 50%, breakout: 30%, mean_rev: 10%, sentiment: 10%
regime = "sideways"  →  mean_rev: 50%, breakout: 20%, momentum: 20%, sentiment: 10%
regime = "volatile"  →  mean_rev: 40%, sentiment: 30%, breakout: 20%, momentum: 10%
regime = "uncertain" →  25% each (equal weights, default)
```

Strategy weights do not block any strategy — they influence position sizing within each strategy.

---

## Technical Indicators Used

All computed by `signals/indicators.py` using `pandas-ta`:

| Indicator | Parameters | Used By |
|-----------|-----------|---------|
| EMA | 9, 20, 50, 200 | Momentum, Sentiment |
| RSI | 14-period | Momentum, Mean Reversion |
| MACD | 12/26/9 | Momentum |
| Bollinger Bands | 20-period, 2σ | Mean Reversion |
| ATR | 14-period | All (stop-loss sizing) |
| ADX | 14-period | Momentum (swing), Strategy Selector |
| VWAP | Session | Momentum (intraday) |
| Volume SMA | 20-period | Breakout, Mean Reversion |
| Rolling High/Low | 20-period | Breakout |

---

## Signal Flow

```
Market Analyst
    │
    │  generate_signal(symbol, df, mode)
    │
    ▼
Signal {
    symbol: "RELIANCE"
    action: BUY
    strategy: "momentum"
    mode: INTRADAY
    product: MIS
    entry_price: 1431.50
    stop_loss: 1409.00
    target: 1476.00
    position_size_pct: 0.02
    confidence: 0.73
    indicators: {rsi: 62.1, adx: 31.2, ema20: 1428.0}
}
    │
    ▼
Risk Manager → approves → ApprovedSignal {approved_qty: 14}
    │
    ▼
Execution Agent → Paper: PAPER-00001 BUY 14 RELIANCE MIS @ 1431.50
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
| Number of trades | ≥ 200 |
| Test period | ≥ 2 years |

Run backtests with:
```bash
.venv/bin/python main.py backtest --start 2022-01-01 --end 2024-12-31 --strategy momentum
```
