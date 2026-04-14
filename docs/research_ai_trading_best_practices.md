# Research: AI/ML/LLM in Algo Trading — Best Practices & Findings

*Research conducted April 2026 for production roadmap planning.*

---

## 1. Current State of LLM-Based Trading (2024-2025)

### What's Working in Industry

**Hybrid LLM + Reinforcement Learning** is the dominant winning approach:
- LLMs provide **context** (news, sentiment, analyst reports, regime awareness)
- RL agents **optimize policy** for risk-adjusted returns (Sharpe, CVaR, drawdown)
- Pure LLM trading underperforms hybrid systems
- Source: [Top 3 LLM+RL Advances in Equity Trading (2025)](https://www.slavanesterov.com/2025/05/3-llmrl-advances-in-equity-trading-2025.html)

**Multi-Agent Specialized Systems** outperform monolithic LLMs:
- Specialized agents: Technical Analyst, Sentiment Analyst, Risk Manager, News Analyst
- Our current architecture (orchestrator + sentiment_agent + risk_agent + market_analyst) already follows this pattern ✓
- Source: [TradingAgents Multi-Agent Framework](https://tradingagents-ai.github.io/)

**Small, Domain-Fine-Tuned Models Beat Large General Models:**
- A fine-tuned Llama-70B on financial data > GPT-4 on general text
- Claude Haiku focused on financial tasks > Opus on generic reasoning for our use case
- Source: [From Deep Learning to LLMs: A Survey](https://arxiv.org/html/2503.21422v1)

**Shift: Abandon Raw Alpha Chasing → Maximize Risk-Adjusted Metrics:**
- Optimize for: Sharpe Ratio, CVaR (tail risk), Max Drawdown, regime consistency
- Our current Sharpe (2.1-2.6) is good; need to maintain while improving alpha
- Source: [Large Language Models in Equity Markets](https://pmc.ncbi.nlm.nih.gov/articles/PMC12421730/)

---

## 2. Critical Pitfalls and Limitations

### LLM-Specific Limitations

**1. Bull Market Underperformance (Our Problem)**
> "LLM strategies are overly conservative in bull markets, underperforming passive benchmarks, and overly aggressive in bear markets, incurring heavy losses. No active strategy surpasses passive Sharpe ratios in bull regimes."

- LLMs ask "should I sell?" instead of "should I hold?" → generates false sell signals
- Fix: Make LLM a stock PICKER, not a daily TRADER. In STRONG_BULL, code enforces "hold everything" — LLM only called for stock selection.
- Source: [Can LLM Strategies Outperform Long-Term?](https://arxiv.org/html/2505.07078v1)

**2. Overfitting to Narrow Timeframes**
> "Most evaluations of LLM timing-based investing strategies are conducted on narrow timeframes and limited stock universes, overstating effectiveness due to survivorship and data-snooping biases."
> "Systematic backtests over two decades and 100+ symbols reveal that previously reported LLM advantages deteriorate significantly under broader cross-section and over longer-term evaluation."

- Our 3-period backtest is NOT sufficient validation
- Need: 17-period backtest, walk-forward, out-of-sample testing
- Source: [Can LLM Strategies Outperform Long-Term?](https://arxiv.org/html/2505.07078v1)

**3. Numerical Reasoning Challenges**
> "LLMs often struggle with numerical reasoning when analyzing plain-text financial data, frequently overfitting to local patterns and recent values."

- Don't ask LLM to predict exact price targets or % moves
- Use LLM for: relative ranking, directional bias, context interpretation
- Keep LLM away from: exact entry prices, stop calculations (these should be code-computed)

**4. Prediction Horizon Limitations**
> "LLM performance declines with longer prediction horizons. LLMs work best for identifying immediate patterns and trends, not predicting the distant future."

- Our current 5-day holding period may be too long for LLM decisions
- Better: LLM decides which stocks to hold for the period; code decides when to exit based on price action

**5. Regime Detection Lag**
> "Sudden market regime changes not captured in historical training data can cause model failure."

- EMA-based regime detection has 1-3 week lag
- ROC-based crash detection (what we added) helps significantly
- FII/DII flows are the best leading indicator for Indian markets (not implemented yet)

---

## 3. Sentiment Analysis in Trading

### What Research Shows

**Performance Impact:**
- Sentiment-augmented strategies improve risk-adjusted returns by 0.5-2% alpha
- Reduces volatility (confirming signals reduces false positives)
- Works best as a CONFIRMATORY signal, not a primary driver
- Source: [How Sentiment Indicators Improve Algo Trading Performance](https://journals.sagepub.com/doi/10.1177/21582440251369559)

**Production Challenges:**
1. **Data quality:** News can be manipulated, bots can inflate social sentiment
2. **Processing latency:** By the time sentiment is processed, market may have moved
3. **Drift:** Sentiment patterns change; needs constant monitoring
4. **Infrastructure:** Real-time sentiment needs stream processing (Apache Flink/Spark)
- Source: [Algorithmic Trading with Real-Time Sentiment](https://easychair.org/publications/preprint/z4xQ/open)

### Our Correct Implementation Strategy

**DO:**
- Use END-OF-DAY sentiment aggregation (not real-time — too noisy)
- Aggregate 5-10 sources: NSE announcements, ET, Moneycontrol, Reddit (currently done ✓)
- Apply time-decay weighting (currently done ✓)
- Use sentiment as CONFIRMATION of technical signals only
- A/B test: measure actual alpha gain with vs without sentiment

**DON'T:**
- Allow sentiment alone to trigger buys/sells
- Use Nitter (community-operated, unreliable) as primary source
- Trust sentiment from < 3 news items (too low volume)
- Use sentiment signals older than 24 hours

**Expected Impact on Backtest:**
- Sentiment data not available historically → can't backtest
- Accept this gap: sentiment adds alpha in production but can't be validated historically
- Validate separately: 30-day paper trading comparison with/without sentiment signals

---

## 4. Benchmark-Beating Requirements

### Is Our Philosophy Correct?

**YES — your thinking is 100% correct:**

If an algo trading model doesn't beat the benchmark with significant margin, it has zero value.
- Anyone can buy NIFTY BeES ETF for 0.04% annual fee
- Every rupee of underperformance = direct loss to customer
- Every rupee of outperformance above 2-3% = real value created

**Industry Benchmarks for "Success":**
| Trader Type | Required Alpha | Our Current |
|-------------|---------------|-------------|
| Retail algo trader | +2-5% annually | +11.52% avg (3 periods) ✓ |
| Institutional fund | +5-15% annually | Marginal in bull (+1.4%) ⚠️ |
| Top quant hedge fund | +15-30% bull, +5-15% bear | Not there yet |
| **Our target** | **+5-10% every regime** | **Need work in bull** |

**Key insight from research:**
> "Two priorities for future LLM-based investors are: (1) enhancing uptrend detection to match or exceed passive exposure, and (2) including regime-aware risk controls to dynamically adjust aggression."

This is exactly what Phase 2 of our roadmap addresses.

---

## 5. Production Deployment Considerations

### The Backtest-to-Production Gap

The biggest failure mode in algo trading: model works in backtest, fails in production.

**Common causes:**
1. **Slippage:** Backtest assumes perfect fills. Reality: 0.1-0.4% worse fills
2. **Market impact:** Larger orders move prices. Affects position sizing
3. **Regime shifts:** Production encounters regimes not in training period
4. **Latency:** LLM API delay (2-5s) means price has moved
5. **Overfitting:** Model learned noise from historical periods

**Our specific gaps (found in code audit):**
- Cash yield of 6% credited in backtest but not in production → returns inflated 1-2%
- Position sizing: backtest allows 20% per position in bull; production caps at 5%
- FillTracker not wired → production P&L tracking is inaccurate
- SL-M not cancelled on exit → double-exit risk in live trading

**What must be validated BEFORE live trading:**
- Paper trading for 30+ days matches backtest alpha direction
- No critical bugs (double executions, missed exits)
- Auth refresh works reliably (daily 8:30am)
- LLM cost stays within budget

### Key Production Risk Controls
1. Daily loss limit: 5% of capital → halt all trading
2. LLM budget: $2/day hard ceiling → don't go over
3. Reconciliation: Compare code's view vs broker's view daily
4. Sharpe drift: Alert if rolling Sharpe < 1.0 (sign of model decay)
5. Win rate monitor: Pause if win rate < 35% for 5 days (regime change)

---

## 6. Indian Market Specifics

### Why FII/DII Flows Are Critical for India

India's market has a unique characteristic: **Foreign Institutional Investors (FIIs) drive >60% of large-cap price movement** at the margin. When FIIs are net buyers, even weak stocks go up. When FIIs sell, even fundamentally strong stocks fall.

This creates a genuine alpha opportunity: FII data is published daily by NSE at ~7pm (after close). Using yesterday's FII data to weight tomorrow's buys is not look-ahead bias — it's a real signal.

**FII data source:** `https://www.nseindia.com/api/fiidiiTradeReact` (no API key, scraping)

**Signal logic:**
- 5-day FII net buy > ₹5,000 crore → regime is BULLISH, increase deployment
- 5-day FII net sell > ₹5,000 crore → regime is BEARISH, reduce deployment
- FII/DII divergence (FII selling, DII buying) → uncertain, stay neutral

### India VIX as Regime Indicator
- India VIX > 25: High fear, reduce position sizes
- India VIX < 12: Complacency, potential reversal — don't add
- India VIX 12-20: Normal range, full deployment OK

### NSE Data Quirks
- NSE API requires session cookie from homepage first (scraping workaround)
- Corporate announcement API is reliable (official endpoint)
- BSE API is slower and sometimes unavailable
- Always have yfinance as fallback

---

## 7. Cost Optimization Strategy

### Claude API (Monthly Budget: ~₹1,000-1,500/month)

| Use Case | Model | Cost per Call | Calls/Day | Monthly Cost |
|----------|-------|--------------|-----------|-------------|
| Portfolio decision (50 symbols) | Haiku 4.5 | ~$0.002 | 0-1 (regime-gated) | ~$3 |
| Sentiment scoring (per symbol) | Haiku 4.5 | ~$0.001 | 5-10 | ~$2 |
| Risk LLM review (high-stakes only) | Haiku 4.5 | ~$0.002 | 0-3 | ~$1 |
| **Total Production** | | | | **~$6/month** |
| **Backtesting (use Groq)** | Groq Llama-3.3 | ~$0.0002 | N/A (batch) | ~$1-2/run |

**Key savings:**
1. Regime-gating saves ~70% of LLM calls (no calls in BEAR/CRASH)
2. Decision reuse saves ~60% more (same decision for 2-5 days)
3. Groq for backtesting = 10x cheaper than Claude (paid tier required)
4. Hard daily ceiling at $2/day prevents runaway costs

---

## 8. Sources

- [Advancing Algorithmic Trading with LLMs: RL Approach](https://openreview.net/forum?id=w7BGq6ozOL)
- [Top 3 LLM+RL Advances in Equity Trading (2025)](https://www.slavanesterov.com/2025/05/3-llmrl-advances-in-equity-trading-2025.html)
- [Can LLM Strategies Outperform Long-Term?](https://arxiv.org/html/2505.07078v1)
- [From Deep Learning to LLMs: Survey](https://arxiv.org/html/2503.21422v1)
- [TradingAgents Multi-Agent Framework](https://tradingagents-ai.github.io/)
- [LLMs in Equity Markets: Applications & Insights](https://pmc.ncbi.nlm.nih.gov/articles/PMC12421730/)
- [How Sentiment Indicators Improve Algo Trading](https://journals.sagepub.com/doi/10.1177/21582440251369559)
- [Sentiment Analysis for Trading (QuantInsti)](https://blog.quantinsti.com/sentiment-analysis-trading/)
- [Real-Time Sentiment Deployment Challenges](https://easychair.org/publications/preprint/z4xQ/open)
- [ML Pitfalls in Trading Strategies (Resonanz Capital)](https://resonanzcapital.com/insights/benefits-pitfalls-and-mitigation-strategies-of-applying-ml-to-financial-modelling)
- [Essential Performance Metrics for Algo Trading](https://bluechipalgos.com/blog/essential-performance-metrics-for-algorithmic-trading/)
- [Alpha Definition & Success Metrics](https://rcademy.com/alpha-in-finance/)
- [Why LLM Backtests Are Expensive](https://medium.com/@kojott/why-llm-trading-backtests-need-two-weeks-and-twenty-dollars-5a19a525a095)
