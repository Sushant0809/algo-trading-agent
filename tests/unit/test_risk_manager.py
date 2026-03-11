"""
Unit tests for risk manager and position sizer.
"""
import asyncio
import pytest
import pytest_asyncio

from risk.portfolio_state import PortfolioState, Position
from risk.position_sizer import PositionSizer
from risk.risk_manager import KillSwitchError, RiskManager
from monitoring.audit_trail import AuditTrail
from signals.signal_model import Signal, SignalAction, SignalStrength, TradingMode, Product
from pathlib import Path


def make_signal(
    symbol: str = "RELIANCE",
    mode: TradingMode = TradingMode.SWING,
    entry_price: float = 2500.0,
    stop_loss: float = 2450.0,
    position_size_pct: float = 0.02,
) -> Signal:
    return Signal(
        symbol=symbol,
        action=SignalAction.BUY,
        strategy="momentum",
        mode=mode,
        product=Product.CNC,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target=2600.0,
        position_size_pct=position_size_pct,
        strength=SignalStrength.MODERATE,
        confidence=0.65,
        indicators={"atr": 30.0},
    )


@pytest.fixture
def portfolio():
    return PortfolioState(initial_capital=1_000_000)


@pytest.fixture
def audit(tmp_path):
    return AuditTrail(tmp_path / "audit")


@pytest.fixture
def risk_mgr(portfolio, audit):
    return RiskManager(portfolio, audit)


class TestPositionSizer:
    def test_fixed_fraction(self):
        sizer = PositionSizer(max_position_pct=0.05)
        qty = sizer.fixed_fraction(1_000_000, 0.02, 500.0)
        assert qty == 40  # ₹20,000 / ₹500 = 40 shares

    def test_fixed_fraction_capped_by_max(self):
        sizer = PositionSizer(max_position_pct=0.05)
        qty = sizer.fixed_fraction(1_000_000, 0.10, 100.0)
        # 10% = ₹100,000 but max is 5% = ₹50,000 → 500 shares
        assert qty == 500

    def test_volatility_atr(self):
        sizer = PositionSizer(max_position_pct=0.05, max_risk_per_trade_pct=0.01)
        # risk_capital = 1M * 1% = ₹10,000
        # stop_distance = 1.5 * 50 = ₹75
        # risk_qty = 10,000 / 75 = 133
        # max_qty = 1M * 5% / ₹100 = 500 → risk_qty(133) wins
        qty = sizer.volatility_atr(1_000_000, 100.0, atr=50.0, stop_mult=1.5)
        assert qty == 133

    def test_zero_atr_returns_zero(self):
        sizer = PositionSizer()
        qty = sizer.volatility_atr(1_000_000, 2500.0, atr=0.0)
        assert qty == 0


class TestRiskManager:
    @pytest.mark.asyncio
    async def test_approves_valid_signal(self, risk_mgr, portfolio):
        signal = make_signal()
        approved = await risk_mgr.evaluate(signal)
        assert approved is not None
        assert approved.approved_qty > 0

    @pytest.mark.asyncio
    async def test_rejects_duplicate_position(self, risk_mgr, portfolio):
        # Open a position first
        pos = Position(
            symbol="RELIANCE", product="CNC", qty=10, avg_price=2500,
            strategy="momentum", mode="swing"
        )
        await portfolio.open_position(pos)

        signal = make_signal(symbol="RELIANCE")
        approved = await risk_mgr.evaluate(signal)
        assert approved is None

    @pytest.mark.asyncio
    async def test_kill_switch_daily_loss(self, risk_mgr, portfolio):
        # Simulate 3% daily loss by reducing cash (daily_loss_pct = (initial - total) / initial)
        portfolio.cash = 970_000  # ₹30k loss on ₹1M initial = 3% > 2% limit

        with pytest.raises(KillSwitchError):
            risk_mgr.check_kill_switches()

    @pytest.mark.asyncio
    async def test_rejects_below_min_price(self, risk_mgr):
        signal = make_signal(entry_price=5.0, stop_loss=4.0)  # Below ₹10 min
        approved = await risk_mgr.evaluate(signal)
        assert approved is None

    @pytest.mark.asyncio
    async def test_max_swing_positions(self, risk_mgr, portfolio):
        # Fill up to max swing positions
        for i in range(15):
            pos = Position(
                symbol=f"SYM{i}", product="CNC", qty=5, avg_price=100,
                strategy="test", mode="swing"
            )
            await portfolio.open_position(pos)

        signal = make_signal(symbol="NEWSTOCK")
        approved = await risk_mgr.evaluate(signal)
        assert approved is None


class TestPortfolioState:
    @pytest.mark.asyncio
    async def test_open_close_position(self):
        portfolio = PortfolioState(1_000_000)
        pos = Position(
            symbol="TCS", product="CNC", qty=10, avg_price=3500,
            strategy="momentum", mode="swing"
        )
        await portfolio.open_position(pos)
        assert portfolio.has_position("TCS")
        assert portfolio.cash == 1_000_000 - 35_000

        trade = await portfolio.close_position("TCS", exit_price=3600, exit_reason="target")
        assert trade is not None
        assert trade.realized_pnl == 1000  # (3600 - 3500) * 10
        assert not portfolio.has_position("TCS")
        assert portfolio.cash == 1_000_000 + 36_000 - 35_000

    @pytest.mark.asyncio
    async def test_drawdown_calculation(self):
        portfolio = PortfolioState(1_000_000)
        portfolio.peak_capital = 1_100_000
        portfolio.cash = 950_000
        # Drawdown = (1.1M - 950k) / 1.1M ≈ 13.6%
        assert portfolio.drawdown_pct > 0.13

    def test_summary(self):
        portfolio = PortfolioState(500_000)
        summary = portfolio.summary()
        assert "cash" in summary
        assert "total_capital" in summary
        assert summary["open_positions"] == 0
