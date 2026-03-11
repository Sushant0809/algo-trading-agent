# Risk Management

## Philosophy

**Hard rules are never overridden by the LLM.** Claude assists with judgment calls but cannot bypass any of the hard limits defined in `config/risk_params.yaml`. This separation is critical — a hallucinating LLM should never be able to blow up the portfolio.

The risk system has three layers:
1. **Kill switches** — halt all trading immediately
2. **Signal gates** — block individual signals that violate limits
3. **Position sizing** — size each position to limit per-trade risk

---

## Kill Switches

Checked before every new entry. If triggered, no new positions are opened for the rest of the day.

| Trigger | Threshold | Action |
|---------|-----------|--------|
| Daily loss | ≥ 2% of capital | Halt all new entries |
| Drawdown from peak | ≥ 10% | Halt all trading |
| Outside market hours | Before 9:15am or after 3:15pm IST | No entries |
| Non-trading day | Holiday or weekend | No entries |

**Kill switch code:** `risk/risk_manager.py → check_kill_switches()`

When a kill switch fires, a `KillSwitchError` is raised, logged to audit trail, and (if configured) a Telegram alert is sent.

---

## Signal Gates (Hard Rules)

Every signal from the market analyst passes through these checks in order:

### 1. Duplicate Position Check
- **Rule:** No new position if symbol already has an open position
- **Reason:** Prevents doubling up on losers or overexposure to one stock

### 2. Max Positions Check
- **Rule:** Max 10 intraday (MIS) positions, max 15 swing (CNC) positions simultaneously
- **Reason:** Concentration risk — too many positions reduce ability to monitor each one

### 3. Sector Exposure Check
- **Rule:** Max 20% of total capital in any single sector
- **Reason:** Prevents sector-specific events (e.g., regulatory change in banking) from wiping out the portfolio

### 4. Correlated Positions Check
- **Rule:** Max 3 positions in highly correlated stocks (e.g., HDFC Bank + ICICI Bank + Axis Bank count as correlated)
- **Reason:** Correlated positions don't provide diversification — they move together in stress events

### 5. Price Floor Check
- **Rule:** Entry price must be ≥ ₹10
- **Reason:** Penny stocks (< ₹10) have extreme spreads, low liquidity, and susceptibility to manipulation

### 6. Timing Cutoff
- **Rule:** No new intraday entries after 3:00pm IST (15 minutes before MIS close at 3:15pm)
- **Reason:** Insufficient time for the trade to develop; forced close at 3:15pm would turn any small gain into a loss after impact

### 7. Liquidity Filter
- **Rule:** Stock must have average daily traded value > ₹50 lakh (₹5 million)
- **Reason:** Low-liquidity stocks are hard to exit quickly without moving the price

---

## Position Sizing

Three methods implemented in `risk/position_sizer.py`:

### Method 1: Fixed Fraction (Default)
```
position_value = capital × position_size_pct
qty = floor(position_value / entry_price)
qty = min(qty, floor(capital × max_position_pct / entry_price))
```
- `position_size_pct` comes from the signal (set by strategy, typically 2%)
- Hard cap: max 5% of total capital per position

### Method 2: Volatility-ATR
```
risk_capital = capital × max_risk_per_trade_pct  (default 1%)
stop_distance = ATR × stop_multiplier             (default 1.5×)
risk_qty = floor(risk_capital / stop_distance)
max_qty = floor(capital × max_position_pct / entry_price)
qty = min(risk_qty, max_qty)
```
- Sizes position so the stop-loss loss never exceeds 1% of capital
- Larger ATR → smaller position (more volatile = smaller bet)

### Method 3: Half-Kelly
```
kelly_f = (win_rate × avg_win - (1 - win_rate) × avg_loss) / avg_win
half_kelly_pct = kelly_f / 2
```
- Requires backtest statistics (win rate, avg win/loss)
- Used for strategies with a proven edge
- Half-Kelly to reduce variance compared to full Kelly

### Smallcap Allocation Cap
- Total allocation to smallcap stocks (Nifty Smallcap 250) ≤ 15% of portfolio
- Applied at portfolio level, not per trade

---

## Daily Risk Budget

The intraday and swing modes share a daily loss budget:

```
Total daily loss limit = 2% of capital

If intraday loses 1.5% → swing gets only 0.5% remaining budget
If either mode alone loses 2% → all trading stops for the day
```

This prevents intraday losses from bleeding into swing positions and vice versa.

---

## Position Management

### Entry
1. Signal approved → execution agent places LIMIT order at entry_price
2. Immediately after fill: SL-M (stop-loss market) order placed at stop_loss price
3. Target stored in portfolio state for portfolio agent to monitor

### During Trade
Portfolio agent checks every 60 seconds:
- Has price hit target? → Close position
- Has price hit stop? → Zerodha's SL-M order auto-triggers
- Trailing stop update: if price moved 1 ATR in our favor, trail stop up by 1 ATR

### Exit — Intraday (MIS)
At **3:15pm IST**, portfolio agent force-closes all MIS positions:
```python
# portfolio_agent.py → close_all_mis()
for position in open_mis_positions:
    await order_manager.place_exit_order(position, reason="EOD_CLOSE")
```
This happens 5 minutes before Zerodha's automatic 3:20pm square-off to avoid market impact of Zerodha's bulk square-off.

### Exit — Swing (CNC)
- Held overnight
- Stop-loss order updated each day based on updated ATR
- No automatic EOD close

---

## Audit Trail

Every risk decision is logged to `logs/audit/YYYY-MM-DD.jsonl`:

```json
{
  "timestamp": "2026-03-11T09:32:15.123Z",
  "agent": "RiskManager",
  "decision": "REJECTED: duplicate position",
  "signal": {"symbol": "RELIANCE", "action": "BUY", "entry_price": 1431.5},
  "reason": "Position already open for RELIANCE"
}
```

```json
{
  "timestamp": "2026-03-11T09:32:45.456Z",
  "agent": "RiskManager",
  "decision": "APPROVED qty=14",
  "signal": {"symbol": "HDFCBANK", "action": "BUY", "entry_price": 1720.0},
  "approved_qty": 14,
  "stop_loss": 1694.0,
  "position_value": 24080.0
}
```

---

## Risk Parameters Reference

From `config/risk_params.yaml`:

```yaml
# Position limits
max_position_size_pct: 0.05        # 5% per position
max_sector_exposure_pct: 0.20      # 20% per sector
max_daily_loss_pct: 0.02           # 2% daily loss kill switch
max_drawdown_pct: 0.10             # 10% from peak kill switch
max_intraday_positions: 10         # MIS simultaneous
max_swing_positions: 15            # CNC simultaneous
max_correlated_positions: 3        # correlated stocks

# Trade quality filters
min_liquidity_daily_value_cr: 0.5  # ₹50 lakh daily traded value
min_price: 10.0                    # No stocks below ₹10
max_loss_per_trade_pct: 0.01       # 1% max loss per trade
stop_loss_atr_multiplier: 1.5      # Stop = 1.5 × ATR below entry

# Allocation caps
smallcap_max_allocation_pct: 0.15  # Max 15% in smallcaps

# Timing
intraday_entry_cutoff_hour: 15     # No new entries after 3pm
intraday_entry_cutoff_minute: 0
mis_close_hour: 15
mis_close_minute: 15

# Backtest promotion gate
backtest_min_sharpe: 1.0
backtest_max_drawdown: 0.15
backtest_min_win_rate: 0.45
backtest_min_trades: 200
```

---

## Paper Trading Safety

While `PAPER_TRADING=true`:
- All order calls go to `paper_simulator.py` — no real API calls to Zerodha
- Virtual orders get IDs like `PAPER-00001`, `PAPER-00002`
- Slippage modeled at 0.05% (realistic for liquid large-caps)
- Portfolio state tracked identically to live mode
- All risk rules still apply (same code path — paper mode is not a bypass)

The only difference between paper and live:
```python
# order_manager.py
if settings.is_paper:
    return await paper_simulator.execute(signal)
else:
    return await kite_executor.execute(signal)
```
