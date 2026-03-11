# Quickstart Guide

## Prerequisites

| Requirement | Details |
|------------|---------|
| Python | 3.11 or 3.12 |
| Zerodha Account | Active Zerodha trading account |
| KiteConnect App | Created at developers.kite.trade (free) |
| Anthropic API Key | From platform.claude.com |
| TOTP 2FA | Set up via Zerodha → My Profile → Security → Enable TOTP |

---

## 1. Installation

```bash
# Clone / navigate to project
cd "/Volumes/D Drive/Algo-trading-agent"

# Create virtual environment
python3.12 -m venv .venv

# Activate
source .venv/bin/activate

# Install all dependencies
pip install -e .

# Install Playwright browser (for daily auto-login)
.venv/bin/playwright install chromium
```

---

## 2. Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Zerodha KiteConnect
KITE_API_KEY=your_api_key_here          # From developers.kite.trade
KITE_API_SECRET=your_secret_here
KITE_ACCESS_TOKEN=                       # Auto-populated daily

# Zerodha login (for Playwright auto-login each morning)
ZERODHA_USER_ID=your_user_id            # e.g. DHW106
ZERODHA_PASSWORD=your_password
ZERODHA_TOTP_SECRET=your_base32_secret  # From TOTP QR code scan

# Anthropic Claude API
ANTHROPIC_API_KEY=sk-ant-api03-...
ANTHROPIC_MODEL=claude-sonnet-4-6

# Trading mode — KEEP TRUE until strategy is verified
PAPER_TRADING=true
TRADING_MODE=both                        # intraday | swing | both

# Capital
PAPER_TRADING_CAPITAL=1000000           # ₹10 lakh virtual capital
LIVE_CAPITAL_LIMIT=100000               # ₹1 lakh cap for real trading
```

### KiteConnect Setup (One-time)

1. Go to [developers.kite.trade](https://developers.kite.trade)
2. Create a new app
3. Set **Redirect URL** to `http://127.0.0.1` (exactly this)
4. Add your Zerodha user ID as an **authorized user**
5. Copy API Key and API Secret to `.env`

### Anthropic API Setup (One-time)

1. Go to [platform.claude.com](https://platform.claude.com)
2. Create an API key under **API Keys**
3. Go to **Limits** → set **Monthly spend limit** to ≥ $5
4. Add credits under **Billing**

### TOTP Secret

The TOTP secret is the Base32 string from the QR code shown when you enable TOTP in Zerodha:

- Zerodha → My Profile → Security → Two-factor Authentication → TOTP
- Scan the QR code with any authenticator app **and** copy the raw secret string
- Paste the Base32 secret (e.g. `L7XVWMLCTJ4XIEP4`) into `.env` as `ZERODHA_TOTP_SECRET`

---

## 3. Running

### Paper Trading (Recommended first)

```bash
cd "/Volumes/D Drive/Algo-trading-agent"

# Run in foreground (see logs live)
.venv/bin/python main.py run --mode paper --trading both

# Run in background (append to log file)
.venv/bin/python main.py run --mode paper --trading both >> logs/trading.log 2>&1 &
```

### Intraday only

```bash
.venv/bin/python main.py run --mode paper --trading intraday
```

### Swing only

```bash
.venv/bin/python main.py run --mode paper --trading swing
```

### Stop the agent

```bash
pkill -f "main.py"
```

---

## 4. Monitoring

### Live log stream (recommended)

```bash
tail -f logs/trading.log | grep -E "Signal|BUY|SELL|PAPER|Approved|Rejected|scan complete"
```

### Status dashboard

```bash
.venv/bin/python status.py
```

Shows:
- Agent running status
- Today's sentiment scores for 30 Nifty 50 stocks
- Any signals generated (BUY/SELL)
- Paper trades placed
- Risk alerts

### What to look for in logs

| Log message | Meaning |
|-------------|---------|
| `[09:15 IST] Running intraday 5-min cycle (61 symbols)` | Scan started |
| `Universe scan complete: 61 symbols` | Scan finished |
| `Signal generated: BUY RELIANCE @ ₹1431` | Strategy found an entry |
| `RiskAgent: Approved RELIANCE qty=35` | Risk checks passed |
| `[PAPER] Order PAPER-00001: BUY 35 RELIANCE MIS` | Virtual order placed |
| `KillSwitchError: daily loss 2.1%` | Trading halted for the day |

---

## 5. Backtesting

```bash
.venv/bin/python main.py backtest \
  --start 2023-01-01 \
  --end 2024-12-31 \
  --strategy momentum
```

Output: HTML tearsheet in `logs/backtest_reports/`

Promotion gate — strategy must pass before going live:
- Sharpe Ratio > 1.0
- Max Drawdown < 15%
- Win Rate > 45%
- Minimum 200 trades

---

## 6. Manual Auth Token Refresh

The KiteConnect access token expires daily at 6am IST. It auto-refreshes at 8:30am IST via the scheduler. To refresh manually:

```bash
.venv/bin/python main.py auth-refresh
```

Or directly:

```bash
.venv/bin/python -c "
import asyncio
from config.settings import get_settings
from auth.kite_auth import run_daily_auth
s = get_settings()
asyncio.run(run_daily_auth(s.kite_api_key, s.kite_api_secret, s.zerodha_user_id, s.zerodha_password, s.zerodha_totp_secret, s.token_cache_path))
"
```

---

## 7. Running Tests

```bash
# All tests
.venv/bin/pytest tests/ -v

# Unit tests only
.venv/bin/pytest tests/unit/ -v

# Specific test file
.venv/bin/pytest tests/unit/test_risk_manager.py -v
```

---

## 8. Daily Schedule (IST)

| Time | Action |
|------|--------|
| 8:30am | Playwright auto-login → fresh KiteConnect token |
| 8:31am | Refresh NSE instrument token cache |
| 8:45am | Pre-market: sentiment scan on 30 Nifty 50 stocks |
| 9:00am | Claude selects strategy weights based on market regime |
| 9:15am | Market opens — intraday 5-min scan cycle begins |
| Every 5min | Scan 61 symbols, generate signals, execute paper trades |
| 3:15pm | Close all MIS (intraday) positions |
| 3:30pm | Market closes |
| 3:35pm | Daily P&L report generated |
| 4:00pm | Swing portfolio review, update trailing stops |

---

## 9. Going Live (Real Money)

**Do NOT go live until:**
1. All 4 strategies pass the backtest promotion gate
2. Paper trading for at least 30 days with positive results
3. Reviewed all audit logs in `logs/audit/`

When ready:

```env
# In .env — change this line:
PAPER_TRADING=false
LIVE_CAPITAL_LIMIT=100000    # Start with ₹1 lakh max
```

Real orders will then flow to Zerodha. Start with a small capital limit and monitor closely.
