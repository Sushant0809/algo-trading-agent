# LLM Portfolio Manager - Multi-Provider Support

The portfolio manager supports 3 LLM providers. Choose based on your needs:

## Providers Comparison

| Provider | Model | Speed | Cost | Rate Limit | Best For |
|----------|-------|-------|------|-----------|----------|
| **Claude** | Haiku 4.5 | ⭐⭐⭐⭐ Fast | $0.80/$4.00 per 1M tokens | None | Production, reliability |
| **Groq** | Llama 3.1-70B | ⭐⭐⭐⭐⭐ Fastest | ~$0.10 per 1M tokens* | None (paid tier) | Backtesting, cost-efficient |
| **NVIDIA** | Llama 3.1-70B | ⭐⭐⭐ Medium | Free (tier limited) | 429 errors on free | Development only |

*Groq has free tier but with daily token limits. Paid tier recommended for backtesting.

---

## Setup Instructions

### 1. Claude (Default)
Already configured. Uses Anthropic API key from `.env`.

```bash
export LLM_PROVIDER=claude
python run_llm_backtest.py --short
```

**Pros:**
- No setup needed (if you have ANTHROPIC_API_KEY)
- Reliable, battle-tested
- No rate limits

**Cons:**
- More expensive than Groq

---

### 2. Groq (Recommended for Backtesting)

#### Setup:
1. Create account: https://console.groq.com
2. Get API key from console
3. Add to `.env`:
```bash
GROQ_API_KEY=your_key_here
```

#### Run:
```bash
export LLM_PROVIDER=groq
python run_llm_backtest.py --short
```

**Pros:**
- ~10x cheaper than Claude
- Ultra-fast inference
- No rate limits on paid tier

**Cons:**
- Requires signup + payment
- Free tier has token limits

---

### 3. NVIDIA (Development Only)

#### Setup:
1. Create account: https://build.nvidia.com
2. Get API key
3. Add to `.env`:
```bash
NVIDIA_API_KEY=your_key_here
```

#### Run:
```bash
export LLM_PROVIDER=nvidia
python run_llm_backtest.py --short
```

**Pros:**
- Free tier available
- Llama 3.1-70B (good quality)

**Cons:**
- Free tier rate-limited (~1 req/30s) — breaks backtesting
- Returns 429 Too Many Requests errors
- Not suitable for production backtesting

---

## Usage Examples

### Use Claude (default)
```bash
python run_llm_backtest.py --short
# or explicitly:
LLM_PROVIDER=claude python run_llm_backtest.py --short
```

### Use Groq
```bash
LLM_PROVIDER=groq python run_llm_backtest.py --medium
```

### Use NVIDIA
```bash
LLM_PROVIDER=nvidia python run_llm_backtest.py --short
# Warning: May hit rate limits
```

### In Python Code
```python
from agents.llm_base import create_llm_manager

# Use default (Claude)
mgr = create_llm_manager()

# Use specific provider
mgr = create_llm_manager(provider="groq")
mgr = create_llm_manager(provider="nvidia")
```

---

## Cost Estimation

### Short backtest (3 periods, 15 symbols)
- **Claude:** ~$0.36
- **Groq:** ~$0.05 (paid tier)
- **NVIDIA:** Free (but rate-limited)

### Medium backtest (5 periods, 15 symbols)
- **Claude:** ~$0.80
- **Groq:** ~$0.12 (paid tier)
- **NVIDIA:** Free (will hit rate limits)

### Full backtest (7 years, 15 symbols)
- **Claude:** ~$7-10
- **Groq:** ~$1-2 (paid tier)
- **NVIDIA:** Free (will fail due to rate limits)

---

## Recommendation

- **For Production Trading:** Claude (reliable, proven)
- **For Backtesting/Development:** Groq (10x cheaper, no rate limits on paid tier)
- **For Experiments:** Claude (fastest, no setup)
- **Not Recommended:** NVIDIA free tier (rate-limited, breaks on sustained API calls)

---

## Troubleshooting

### "Could not resolve authentication method" (Claude)
```bash
# Make sure ANTHROPIC_API_KEY is in .env
export ANTHROPIC_API_KEY=your_key
```

### "GROQ_API_KEY not set" (Groq)
```bash
# Add to .env
GROQ_API_KEY=your_key_here
```

### "HTTP 429 Too Many Requests" (NVIDIA)
- Free tier has strict rate limits
- Upgrade to paid tier or use Claude/Groq instead

---

## Architecture

All providers implement `BaseLLMPortfolioManager` from `agents/llm_base.py`.

Provider-specific implementations:
- `agents/llm_portfolio_manager.py` — Claude
- `agents/groq_portfolio_manager.py` — Groq
- `agents/nvidia_portfolio_manager.py` — NVIDIA

Factory: `create_llm_manager(provider="...")` — selects implementation at runtime.
