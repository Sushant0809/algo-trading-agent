"""
Unit tests for risk/portfolio_allocator.py

Coverage:
  - Ranking: higher-score signals allocated first
  - Cash reserve: signals dropped when < 10% cash floor remains
  - Slot limits: max swing (3) and intraday (5) per cycle
  - Deduplication: duplicate symbols → highest score wins
  - Already-held symbols dropped
  - Empty signal list → empty result
  - No cash available → all dropped
  - cash_reserve_breached() and available_cash() helpers
"""
import pytest

from risk.portfolio_allocator import PortfolioAllocator, CASH_RESERVE_PCT
from risk.portfolio_state import PortfolioState, Position
from signals.signal_model import Signal, SignalAction, SignalStrength, TradingMode, Product


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    symbol: str,
    strategy: str = "momentum",
    mode: TradingMode = TradingMode.SWING,
    confidence: float = 0.70,
    strength: SignalStrength = SignalStrength.MODERATE,
    position_size_pct: float = 0.02,
    action: SignalAction = SignalAction.BUY,
) -> Signal:
    product = Product.MIS if mode == TradingMode.INTRADAY else Product.CNC
    return Signal(
        symbol=symbol,
        action=action,
        strategy=strategy,
        mode=mode,
        product=product,
        entry_price=1000.0,
        stop_loss=950.0,
        target=1100.0,
        position_size_pct=position_size_pct,
        strength=strength,
        confidence=confidence,
        reasoning="test",
    )


def _risk_params(max_pos_pct=0.05, max_swing=10, max_intraday=10) -> dict:
    """Minimal risk params dict accepted by PortfolioAllocator."""
    return {
        "position_limits": {
            "max_position_size_pct": max_pos_pct,
            "max_swing_positions": max_swing,
            "max_intraday_positions": max_intraday,
        }
    }


@pytest.fixture
def portfolio():
    return PortfolioState(initial_capital=1_000_000)


@pytest.fixture
def allocator(portfolio):
    return PortfolioAllocator(portfolio, params=_risk_params())


# ---------------------------------------------------------------------------
# Basic allocation
# ---------------------------------------------------------------------------

class TestPortfolioAllocatorBasic:
    def test_empty_signals_returns_empty(self, allocator):
        assert allocator.allocate([]) == []

    def test_single_signal_allocated(self, allocator):
        sigs = [_make_signal("RELIANCE")]
        result = allocator.allocate(sigs)
        assert len(result) == 1
        assert result[0].symbol == "RELIANCE"

    def test_all_signals_fit(self, allocator):
        sigs = [_make_signal(f"SYM{i}") for i in range(3)]
        result = allocator.allocate(sigs)
        assert len(result) == 3

    def test_returns_signals_sorted_by_score_descending(self, allocator):
        """Higher confidence → higher score → allocated first."""
        low  = _make_signal("LOW",  confidence=0.50, strength=SignalStrength.WEAK)
        high = _make_signal("HIGH", confidence=0.95, strength=SignalStrength.STRONG)
        mid  = _make_signal("MID",  confidence=0.70, strength=SignalStrength.MODERATE)

        result = allocator.allocate([low, high, mid])
        symbols = [s.symbol for s in result]
        assert symbols.index("HIGH") < symbols.index("MID")
        assert symbols.index("MID") < symbols.index("LOW")


# ---------------------------------------------------------------------------
# Cash reserve
# ---------------------------------------------------------------------------

def _fill_positions(portfolio: PortfolioState, cash_to_leave: float = 40_000) -> None:
    """Directly inject a position to eat up capital, keeping total_capital ~1M."""
    spent = portfolio.cash - cash_to_leave
    portfolio.positions["FAKESTOCK"] = Position(
        symbol="FAKESTOCK", product="CNC",
        qty=int(spent / 1000), avg_price=1000.0,
        strategy="test", mode="swing",
    )
    portfolio.cash = cash_to_leave


class TestCashReserve:
    def test_no_cash_after_reserve_drops_all(self):
        portfolio = PortfolioState(initial_capital=1_000_000)
        # Lock 960k in positions, leaving 40k cash.
        # total_capital ≈ 1M, spendable = 900k, already_deployed = 960k → available = 0
        _fill_positions(portfolio, cash_to_leave=40_000)
        allocator = PortfolioAllocator(portfolio, params=_risk_params())

        sigs = [_make_signal("RELIANCE")]
        result = allocator.allocate(sigs)
        assert result == []

    def test_cash_reserve_not_breached_when_enough_cash(self, portfolio, allocator):
        assert not allocator.cash_reserve_breached()

    def test_cash_reserve_breached_when_low(self):
        portfolio = PortfolioState(initial_capital=1_000_000)
        # Lock 960k in positions → cash = 40k < floor (100k of 1M)
        _fill_positions(portfolio, cash_to_leave=40_000)
        allocator = PortfolioAllocator(portfolio, params=_risk_params())
        assert allocator.cash_reserve_breached()

    def test_available_cash_respects_floor(self, portfolio, allocator):
        """available_cash = cash - 10% floor."""
        # cash = 1M (no positions), floor = 100k, available = 900k
        avail = allocator.available_cash()
        floor = portfolio.total_capital * CASH_RESERVE_PCT
        assert avail == pytest.approx(portfolio.cash - floor, abs=1.0)

    def test_available_cash_zero_when_below_floor(self):
        portfolio = PortfolioState(initial_capital=1_000_000)
        # Lock 960k in positions → cash (40k) < floor (100k)
        _fill_positions(portfolio, cash_to_leave=40_000)
        allocator = PortfolioAllocator(portfolio, params=_risk_params())
        assert allocator.available_cash() == 0.0


# ---------------------------------------------------------------------------
# Slot limits
# ---------------------------------------------------------------------------

class TestSlotLimits:
    @pytest.mark.asyncio
    async def test_swing_slot_limit_enforced(self):
        """Max 3 swing signals per cycle (MAX_SIGNALS_SWING = 3)."""
        portfolio = PortfolioState(initial_capital=10_000_000)  # Plenty of cash
        # Use tight swing limit = 3, relaxed intraday
        params = _risk_params(max_swing=3, max_intraday=10)
        allocator = PortfolioAllocator(portfolio, params=params)

        sigs = [_make_signal(f"SW{i}", mode=TradingMode.SWING) for i in range(6)]
        result = allocator.allocate(sigs)
        swing_count = sum(1 for s in result if s.mode == TradingMode.SWING)
        assert swing_count <= 3

    @pytest.mark.asyncio
    async def test_intraday_slot_limit_enforced(self):
        """Max 5 intraday signals per cycle (MAX_SIGNALS_INTRA = 5)."""
        portfolio = PortfolioState(initial_capital=10_000_000)
        params = _risk_params(max_swing=10, max_intraday=5)
        allocator = PortfolioAllocator(portfolio, params=params)

        sigs = [_make_signal(f"IT{i}", mode=TradingMode.INTRADAY) for i in range(8)]
        result = allocator.allocate(sigs)
        intra_count = sum(1 for s in result if s.mode == TradingMode.INTRADAY)
        assert intra_count <= 5

    @pytest.mark.asyncio
    async def test_existing_positions_count_against_slots(self):
        """Existing swing positions reduce available swing slots."""
        portfolio = PortfolioState(initial_capital=10_000_000)
        params = _risk_params(max_swing=3, max_intraday=10)

        # Already hold 3 swing positions
        for i in range(3):
            pos = Position(
                symbol=f"HELD{i}", product="CNC", qty=5, avg_price=100,
                strategy="test", mode="swing",
            )
            await portfolio.open_position(pos)

        allocator = PortfolioAllocator(portfolio, params=params)
        sigs = [_make_signal(f"NEW{i}", mode=TradingMode.SWING) for i in range(3)]
        result = allocator.allocate(sigs)
        # No swing slots left → all dropped
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Deduplication and existing positions
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_duplicate_symbols_keeps_highest_score(self, allocator):
        """Two signals for same symbol → only the higher-scoring one is kept."""
        low  = _make_signal("RELIANCE", confidence=0.50, strength=SignalStrength.WEAK)
        high = _make_signal("RELIANCE", confidence=0.90, strength=SignalStrength.STRONG)
        result = allocator.allocate([low, high])

        assert len(result) == 1
        # Higher confidence signal should be chosen
        assert result[0].confidence == 0.90

    @pytest.mark.asyncio
    async def test_already_held_symbol_dropped(self, portfolio, allocator):
        """If we already have a position in the symbol, skip it."""
        pos = Position(
            symbol="TCS", product="CNC", qty=10, avg_price=3500,
            strategy="test", mode="swing",
        )
        await portfolio.open_position(pos)

        sigs = [_make_signal("TCS"), _make_signal("INFY")]
        result = allocator.allocate(sigs)

        symbols = [s.symbol for s in result]
        assert "TCS" not in symbols
        assert "INFY" in symbols


# ---------------------------------------------------------------------------
# Strategy weights affect ranking
# ---------------------------------------------------------------------------

class TestStrategyWeights:
    def test_higher_strategy_weight_ranks_first(self, portfolio):
        params = _risk_params(max_swing=10, max_intraday=10)
        allocator = PortfolioAllocator(portfolio, params=params)

        # Equal confidence + strength, different strategies
        s_low  = _make_signal("A", strategy="weak_strat",   confidence=0.70)
        s_high = _make_signal("B", strategy="strong_strat", confidence=0.70)

        weights = {"weak_strat": 0.20, "strong_strat": 0.90}
        result = allocator.allocate([s_low, s_high], strategy_weights=weights)

        assert result[0].symbol == "B"

    def test_no_weights_uses_default(self, allocator):
        """Passing no weights should not crash (uses default 0.25)."""
        sigs = [_make_signal("RELIANCE"), _make_signal("TCS")]
        result = allocator.allocate(sigs, strategy_weights=None)
        assert len(result) == 2

    def test_unknown_strategy_uses_default_weight(self, allocator):
        """Strategy not in weights dict → defaults to 0.25."""
        sig = _make_signal("XYZ", strategy="nonexistent_strategy", confidence=0.80)
        result = allocator.allocate([sig], strategy_weights={"other": 0.5})
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Mixed swing + intraday
# ---------------------------------------------------------------------------

class TestMixedAllocation:
    def test_swing_and_intraday_allocated_independently(self):
        portfolio = PortfolioState(initial_capital=10_000_000)
        params = _risk_params(max_swing=3, max_intraday=5)
        allocator = PortfolioAllocator(portfolio, params=params)

        swings   = [_make_signal(f"SW{i}", mode=TradingMode.SWING)    for i in range(3)]
        intraday = [_make_signal(f"IT{i}", mode=TradingMode.INTRADAY) for i in range(5)]

        result = allocator.allocate(swings + intraday)
        swing_out = sum(1 for s in result if s.mode == TradingMode.SWING)
        intra_out = sum(1 for s in result if s.mode == TradingMode.INTRADAY)
        assert swing_out <= 3
        assert intra_out <= 5
