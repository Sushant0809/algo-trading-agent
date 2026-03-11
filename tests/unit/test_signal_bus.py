"""
Unit tests for the signal bus.
"""
import asyncio
import pytest

from signals.signal_bus import SignalBus
from signals.signal_model import (
    ApprovedSignal, ExitSignal, Signal,
    SignalAction, SignalStrength, TradingMode, Product
)


def make_signal() -> Signal:
    return Signal(
        symbol="INFY",
        action=SignalAction.BUY,
        strategy="momentum",
        mode=TradingMode.INTRADAY,
        product=Product.MIS,
        entry_price=1500.0,
        stop_loss=1470.0,
        target=1560.0,
    )


class TestSignalBus:
    @pytest.mark.asyncio
    async def test_publish_and_consume_signal(self):
        bus = SignalBus()
        sig = make_signal()
        await bus.publish_signal(sig)
        assert bus.raw_queue_size == 1

        received = await asyncio.wait_for(bus.consume_signal(), timeout=1.0)
        assert received.symbol == "INFY"
        bus.signal_done()

    @pytest.mark.asyncio
    async def test_publish_approved(self):
        bus = SignalBus()
        sig = make_signal()
        approved = ApprovedSignal(signal=sig, approved_qty=5, approved_capital=7500.0)

        await bus.publish_approved(approved)
        assert bus.approved_queue_size == 1

        received = await asyncio.wait_for(bus.consume_approved(), timeout=1.0)
        assert received.approved_qty == 5

    @pytest.mark.asyncio
    async def test_publish_exit(self):
        bus = SignalBus()
        exit_sig = ExitSignal(
            symbol="INFY",
            action=SignalAction.SELL,
            strategy="momentum",
            mode=TradingMode.INTRADAY,
            product=Product.MIS,
            reason="stop_loss",
            exit_price=1470.0,
        )
        await bus.publish_exit(exit_sig)
        assert bus.exit_queue_size == 1

        received = await asyncio.wait_for(bus.consume_exit(), timeout=1.0)
        assert received.reason == "stop_loss"

    @pytest.mark.asyncio
    async def test_multiple_signals(self):
        bus = SignalBus()
        symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK"]

        for sym in symbols:
            sig = Signal(
                symbol=sym,
                action=SignalAction.BUY,
                strategy="momentum",
                mode=TradingMode.SWING,
                product=Product.CNC,
                entry_price=1000.0,
            )
            await bus.publish_signal(sig)

        assert bus.raw_queue_size == 4

        received = []
        for _ in range(4):
            sig = await asyncio.wait_for(bus.consume_signal(), timeout=1.0)
            received.append(sig.symbol)
            bus.signal_done()

        assert set(received) == set(symbols)
