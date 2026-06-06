"""High-level G-code sender.

Wraps the command queue with semantic operations: jog, move, home, read state.
Issues `M400` before any `M114` we actually care about.
"""

from __future__ import annotations

import logging

from flowengine.hardware.homing.base import HomingStrategy
from flowengine.hardware.motion import MotionModel
from flowengine.hardware.queue import CommandQueue
from flowengine.schemas.device import DeviceMap
from flowengine.schemas.runtime import Timeouts

log = logging.getLogger(__name__)


class GcodeSender:
    def __init__(
        self,
        queue: CommandQueue,
        motion: MotionModel,
        device_map: DeviceMap,
        homing: HomingStrategy,
        timeouts: Timeouts,
    ) -> None:
        self._q = queue
        self._motion = motion
        self._map = device_map
        self._homing = homing
        self._timeouts = timeouts
        self._absolute = True

    async def configure(self) -> None:
        """Push baseline config to the controller (steps/unit, absolute mode)."""
        await self._q.send("G90", timeout=self._timeouts.diagnostics)
        for axis in self._map.axes:
            await self._q.send(
                f"M92 {axis.marlin_axis}{axis.steps_per_unit:.4f}",
                timeout=self._timeouts.diagnostics,
            )
            await self._q.send(
                f"M203 {axis.marlin_axis}{axis.feedrate_max / 60:.3f}",
                timeout=self._timeouts.diagnostics,
            )

    async def jog(self, axis: str, delta: float, feedrate: float | None = None) -> None:
        p = self._motion.plan_relative(axis, delta, feedrate)
        # Marlin's E uses absolute/relative independent of XYZ; keep simple by switching
        # to absolute moves with computed targets.
        await self._q.send(
            f"G1 {axis}{p.target:.4f} F{p.feedrate:.2f}",
            timeout=self._timeouts.move,
        )

    async def move_to(self, axis: str, target: float, feedrate: float | None = None) -> None:
        p = self._motion.plan_absolute(axis, target, feedrate)
        await self._q.send(
            f"G1 {axis}{p.target:.4f} F{p.feedrate:.2f}",
            timeout=self._timeouts.move,
        )

    async def home(self, axes: list[str] | None = None) -> None:
        homed = await self._homing.home(self._q, axes)
        self._motion.mark_homed(homed)

    async def wait_idle(self) -> dict[str, float]:
        """Block until queued motion completes, then read settled position."""
        await self._q.send("M400", timeout=self._timeouts.move)
        result = await self._q.send("M114", timeout=self._timeouts.diagnostics)
        if result.position:
            self._motion.update_position(result.position)
        return self._motion.positions

    async def read_endstops(self) -> dict[str, bool]:
        result = await self._q.send("M119", timeout=self._timeouts.diagnostics)
        return result.endstops or {}

    async def read_firmware(self) -> tuple[str, frozenset[str]]:
        result = await self._q.send("M115", timeout=self._timeouts.diagnostics)
        return (result.firmware_raw or "", result.firmware_caps or frozenset())
