"""
Integration test: full paper trading pipeline from signal generation to paper execution.
Uses synthetic data — no KiteConnect connection required.
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from execution.order_manager import OrderManager
from execution.paper_simulator import PaperSimulator
from monitoring.audit_trail import AuditTrail
from monitoring.alerting import TelegramAlerter
from risk.portfolio_state import PortfolioState
from risk.risk_manager import RiskManager
from signals.signal_bus import SignalBus
from signals.signal_model import ApprovedSignal, Signal, SignalAction, SignalStrength, TradingMode, Product
from strategies.momentum import MomentumStrategy
from signals.indicators import compute_all_indicators


def make_bullish_df(n: int = 300) -> pd.DataFrame:
    np.random.seed(0)
    prices = [100.0]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + np.random.normal(0.0015, 0.01)))

    dates = pd.date_range("2023-01-01", periods=n, freq="B", tz="Asia/Kolkata")
    df = pd.DataFrame({
        "open": prices,
        "high": [p * 1.005 for p in prices],
        "low": [p * 0.995 for p in prices],
        "close": prices,
        "volume": [np.random.randint(1_000_000, 3_000_000) for _ in range(n)],
    }, index=dates)
    return compute_all_indicators(df)


class TestFullPipeline:
    @pytest.fixture
    def portfolio(self):
        return PortfolioState(initial_capital=1_000_000)

    @pytest.fixture
    def audit(self, tmp_path):
        return AuditTrail(tmp_path)

    @pytest.fixture
    def signal_bus(self):
        return SignalBus()

    @pytest.mark.asyncio
    async def test_paper_trade_lifecycle(self, portfolio, audit, tmp_path):
        """Test full lifecycle: signal → risk → paper execution → exit."""
        # Patch settings for paper mode
        with patch("config.settings.get_settings") as mock_settings:
            settings = MagicMock()
            settings.is_paper = True
            settings.paper_trading_capital = 1_000_000
            settings.telegram_bot_token = ""
            settings.telegram_chat_id = ""
            mock_settings.return_value = settings

            alerter = TelegramAlerter("", "")
            risk_mgr = RiskManager(portfolio, audit)
            order_mgr = OrderManager(portfolio, audit, alerter)

            # Create an approved signal
            signal = Signal(
                symbol="RELIANCE",
                action=SignalAction.BUY,
                strategy="momentum",
                mode=TradingMode.SWING,
                product=Product.CNC,
                entry_price=2500.0,
                stop_loss=2450.0,
                target=2600.0,
                position_size_pct=0.02,
                strength=SignalStrength.STRONG,
                confidence=0.75,
                indicators={"atr": 30.0},
            )

            approved = await risk_mgr.evaluate(signal)
            assert approved is not None
            assert approved.approved_qty > 0

            # Execute paper entry
            order_id = await order_mgr.place_entry(approved)
            assert order_id is not None
            assert order_id.startswith("PAPER-")

            # Verify position opened
            assert portfolio.has_position("RELIANCE")
            pos = portfolio.get_position("RELIANCE")
            assert pos.qty == approved.approved_qty

            # Execute paper exit
            from signals.signal_model import ExitSignal
            exit_sig = ExitSignal(
                symbol="RELIANCE",
                action=SignalAction.SELL,
                strategy="momentum",
                mode=TradingMode.SWING,
                product=Product.CNC,
                reason="target",
                exit_price=2600.0,
            )
            exit_id = await order_mgr.place_exit(exit_sig, fill_price=2600.0)
            assert exit_id is not None
            assert not portfolio.has_position("RELIANCE")
            assert portfolio.daily_realized_pnl > 0

    @pytest.mark.asyncio
    async def test_risk_gate_prevents_duplicate(self, portfolio, audit):
        """Test that risk manager prevents opening duplicate positions."""
        risk_mgr = RiskManager(portfolio, audit)

        signal = Signal(
            symbol="TCS",
            action=SignalAction.BUY,
            strategy="momentum",
            mode=TradingMode.SWING,
            product=Product.CNC,
            entry_price=3500.0,
            stop_loss=3450.0,
            target=3600.0,
            indicators={"atr": 40.0},
        )

        # First signal should pass
        approved1 = await risk_mgr.evaluate(signal)
        if approved1:
            from risk.portfolio_state import Position
            pos = Position(
                symbol="TCS", product="CNC", qty=approved1.approved_qty,
                avg_price=3500.0, strategy="momentum", mode="swing"
            )
            await portfolio.open_position(pos)

        # Second signal for same symbol should be rejected
        approved2 = await risk_mgr.evaluate(signal)
        assert approved2 is None

    @pytest.mark.asyncio
    async def test_kill_switch_stops_trading(self, portfolio, audit):
        """Test that kill switch halts new trades."""
        risk_mgr = RiskManager(portfolio, audit)

        # Simulate 3% loss (exceeds 2% limit)
        portfolio.initial_capital = 1_000_000
        portfolio.cash = 970_000
        portfolio.daily_realized_pnl = -30_000

        signal = Signal(
            symbol="WIPRO",
            action=SignalAction.BUY,
            strategy="momentum",
            mode=TradingMode.SWING,
            product=Product.CNC,
            entry_price=400.0,
            stop_loss=390.0,
            target=420.0,
            indicators={"atr": 5.0},
        )

        approved = await risk_mgr.evaluate(signal)
        assert approved is None  # Kill switch blocks new entries
