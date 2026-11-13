from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import flowengine.hardware.endstop_seek as seeking


class ProbeQueue:
    transport_name = "marlin"

    def __init__(self, clock, target_pressed=True):
        self.clock = clock
        self.target_pressed = target_pressed
        self.polls = 0
        self.commands = []

    async def send(self, command, *, timeout):
        self.commands.append(command)
        if command == "M119":
            self.polls += 1
            self.clock.now += 16
        return SimpleNamespace(endstops={"z_min": (self.polls >= 5) == self.target_pressed},
                               position={"C": 0}, raw=["C:0 Count C:0", "ok"])


@pytest.mark.parametrize("timeout,expected", [(None, None), (30, "watchdog_timeout")])
@pytest.mark.parametrize("target_pressed", [True, False])
async def test_unlimited_seek_extends_motion_and_still_stops_on_switch(monkeypatch, timeout, expected, target_pressed):
    clock = SimpleNamespace(now=0)
    clock.time = lambda: clock.now
    monkeypatch.setattr(seeking, "asyncio", SimpleNamespace(get_running_loop=lambda: clock, sleep=asyncio.sleep))
    queue = ProbeQueue(clock, target_pressed)
    result = await seeking.seek_switch(queue, axis="C", channel="z_min", direction=-1,
        feedrate=600, target_pressed=target_pressed, timeout_s=timeout, steps_per_unit=80,
        diagnostics_timeout=1, move_timeout=1, poll_interval_s=0)
    assert result.stop_reason == expected
    assert result.reached is (timeout is None)
    assert "M410" in queue.commands
    assert queue.commands[-1] == "G90"
    if timeout is None:
        assert result.elapsed_s > 30
        assert len([c for c in queue.commands if c.startswith("G1 C-")]) == 2


async def test_unlimited_seek_obeys_operator_stop(monkeypatch):
    clock = SimpleNamespace(now=0)
    clock.time = lambda: clock.now
    monkeypatch.setattr(seeking, "asyncio", SimpleNamespace(get_running_loop=lambda: clock, sleep=asyncio.sleep))
    queue = ProbeQueue(clock)
    result = await seeking.seek_switch(queue, axis="C", channel="z_min", direction=-1,
        feedrate=600, target_pressed=True, timeout_s=None, steps_per_unit=80,
        diagnostics_timeout=1, move_timeout=1, poll_interval_s=0,
        stop_reason=lambda: "cancelled" if clock.now >= 32 else None)
    assert not result.reached and result.stop_reason == "cancelled"
    assert "M410" in queue.commands
    assert queue.commands[-1] == "G90"
