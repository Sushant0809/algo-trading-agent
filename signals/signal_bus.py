"""
Asyncio-based signal bus: strategies → risk manager → execution agent.
Thread-safe with asyncio.Queue.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Union

from signals.signal_model import ApprovedSignal, ExitSignal, Signal

logger = logging.getLogger(__name__)

# Typed alias for anything that can be queued
AnySignal = Union[Signal, ExitSignal, ApprovedSignal]


class SignalBus:
    """Central message bus for signal routing between agents."""

    def __init__(self, maxsize: int = 500):
        self._raw_queue: asyncio.Queue[Signal | ExitSignal] = asyncio.Queue(maxsize=maxsize)
        self._approved_queue: asyncio.Queue[ApprovedSignal] = asyncio.Queue(maxsize=500)
        self._exit_queue: asyncio.Queue[ExitSignal] = asyncio.Queue(maxsize=500)

    # --- Strategy → Risk ---
    async def publish_signal(self, signal: Signal) -> None:
        """Strategy publishes a new entry signal."""
        await self._raw_queue.put(signal)
        logger.debug(f"Signal queued: {signal.symbol} {signal.action.value} [{signal.strategy}]")

    async def consume_signal(self) -> Signal | ExitSignal:
        """Risk manager consumes raw signals."""
        return await self._raw_queue.get()

    def signal_done(self) -> None:
        self._raw_queue.task_done()

    # --- Risk → Execution ---
    async def publish_approved(self, signal: ApprovedSignal) -> None:
        """Risk manager publishes approved signal for execution."""
        await self._approved_queue.put(signal)
        logger.debug(f"Approved signal queued: {signal.signal.symbol} qty={signal.approved_qty}")

    async def consume_approved(self) -> ApprovedSignal:
        """Execution agent consumes approved signals."""
        return await self._approved_queue.get()

    def approved_done(self) -> None:
        self._approved_queue.task_done()

    # --- Portfolio Agent Exit Signals ---
    async def publish_exit(self, signal: ExitSignal) -> None:
        """Portfolio agent publishes exit signals."""
        await self._exit_queue.put(signal)
        logger.debug(f"Exit signal queued: {signal.symbol} reason={signal.reason}")

    async def consume_exit(self) -> ExitSignal:
        """Execution agent consumes exit signals."""
        return await self._exit_queue.get()

    def exit_done(self) -> None:
        self._exit_queue.task_done()

    # --- Status ---
    @property
    def raw_queue_size(self) -> int:
        return self._raw_queue.qsize()

    @property
    def approved_queue_size(self) -> int:
        return self._approved_queue.qsize()

    @property
    def exit_queue_size(self) -> int:
        return self._exit_queue.qsize()
