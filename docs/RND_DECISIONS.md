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

## 7. Why 4 Strategies Instead of More?

**Design principle:** Fewer, well-understood strategies beat many poorly-understood ones.

The 4 strategies cover different market regimes:
- **Momentum** → trending market
- **Mean Reversion** → sideways market
- **Breakout** → transitional market
- **Sentiment** → event-driven

Adding more strategies increases:
- Correlation between signals (diminishing diversification benefit)
- Overfitting risk during backtesting
- Operational complexity

Each strategy must pass the promotion gate (Sharpe > 1.0, MaxDD < 15%, WinRate > 45%, ≥ 200 trades) before being enabled.

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
