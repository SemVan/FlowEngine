from __future__ import annotations

import asyncio

import pytest

from flowengine.hardware.endstop_seek import seek_switch
from flowengine.hardware.queue import CommandQueue
from flowengine.transport.base import Transport


class StreamingMarlin(Transport):
    """команда G1 сразу отвечает ok; показания микрика меняются по ходу движения"""

    name = "marlin"

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.commands: list[str] = []
        self.closed = True
        self.polls = 0

    async def open(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def is_connected(self) -> bool:
        return not self.closed

    async def send_line(self, line: str) -> None:
        self.commands.append(line)
        command = line.upper().strip()
        if command.startswith("M119"):
            self.polls += 1
            pressed = self.polls >= 3
            await self.inbox.put(f"x_min: {'TRIGGERED' if pressed else 'open'}")
            await self.inbox.put("ok")
        elif command.startswith("M114"):
            await self.inbox.put("X:1.5000 Y:0.0000 Z:0.0000 E:0.0000 Count X:120")
            await self.inbox.put("ok")
        else:
            await self.inbox.put("ok")

    async def read_line(self, timeout: float | None = None) -> str:
        if timeout is None:
            return await self.inbox.get()
        return await asyncio.wait_for(self.inbox.get(), timeout)


@pytest.mark.asyncio
async def test_real_seek_is_continuous_and_m410_does_not_fault_queue():
    transport = StreamingMarlin()
    await transport.open()
    queue = CommandQueue(transport)

    result = await seek_switch(
        queue,
        axis="X",
        channel="x_min",
        direction=1,
        feedrate=1200,
        target_pressed=True,
        timeout_s=2,
        steps_per_unit=80,
        diagnostics_timeout=0.5,
        move_timeout=1.0,
        poll_interval_s=0,
    )

    assert result.reached is True
    assert sum(command.startswith("G1 X") for command in transport.commands) == 1
    assert sum(command.startswith("M119") for command in transport.commands) >= 3
    assert "M410" in transport.commands
    assert any(command.startswith("G92 X1.500000") for command in transport.commands)

    followup = await queue.send("M119", timeout=0.5)
    assert followup.endstops == {"x_min": True}


class SlowQuickstopMarlin(StreamingMarlin):

    async def send_line(self, line: str) -> None:
        self.commands.append(line)
        command = line.upper().strip()
        if command.startswith("M410"):
            async def delayed_ack() -> None:
                await asyncio.sleep(0.65)
                await self.inbox.put("ok")
            asyncio.create_task(delayed_ack())
            return
        if command.startswith("M119"):
            self.polls += 1
            pressed = self.polls >= 3
            await self.inbox.put(f"x_min: {'TRIGGERED' if pressed else 'open'}")
            await self.inbox.put("ok")
        elif command.startswith("M114"):
            await self.inbox.put("X:1.5000 Y:0.0000 Z:0.0000 E:0.0000 Count X:120")
            await self.inbox.put("ok")
        else:
            await self.inbox.put("ok")


@pytest.mark.asyncio
async def test_slow_m410_ack_does_not_turn_normal_endstop_hit_into_reconnect_fault():
    transport = SlowQuickstopMarlin()
    await transport.open()
    queue = CommandQueue(transport)

    result = await seek_switch(
        queue,
        axis="X",
        channel="x_min",
        direction=1,
        feedrate=1200,
        target_pressed=True,
        timeout_s=3,
        steps_per_unit=80,
        diagnostics_timeout=1.0,
        move_timeout=2.0,
        poll_interval_s=0,
    )

    assert result.reached is True
    assert "M410" in transport.commands
    followup = await queue.send("M119", timeout=1.0)
    assert followup.endstops == {"x_min": True}
