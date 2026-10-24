"""Conservative, one-valve-at-a-time commissioning operations.

These moves deliberately avoid the normal position model: a new valve is not
referenced yet. Every search move is finite and followed by an M119 check.
This is a bench tool, not a substitute for firmware-stopped production homing.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from flowengine.hardware.motion import MotionModel
from flowengine.hardware.queue import CommandQueue
from flowengine.schemas.device import DeviceMap, ValveConfig
from flowengine.schemas.runtime import Timeouts


class ValveCommissioning:
    def __init__(
        self,
        queue: CommandQueue,
        motion: MotionModel,
        device_map: DeviceMap,
        timeouts: Timeouts,
    ) -> None:
        self._q = queue
        self._motion = motion
        self._map = device_map
        self._timeouts = timeouts
        self._lock = asyncio.Lock()

    def valve(self, name: str) -> ValveConfig:
        valve = next((item for item in self._map.valves if item.name == name), None)
        if valve is None:
            raise ValueError(f"unknown valve {name!r}")
        if len(valve.axis) != 1 or valve.axis == "E":
            raise ValueError("commissioning supports single-letter non-extruder axes only")
        return valve

    async def endstop(self, channel: str) -> bool:
        result = await self._q.send("M119", timeout=self._timeouts.diagnostics)
        states = result.endstops or {}
        if channel not in states:
            available = ", ".join(sorted(states)) or "none"
            raise ValueError(f"endstop {channel!r} not reported by M119; available: {available}")
        return states[channel]

    async def _relative(self, axis: str, delta: float, feedrate: float) -> None:
        await self._q.send("G91", timeout=self._timeouts.diagnostics)
        try:
            await self._q.send(
                f"G1 {axis}{delta:.4f} F{feedrate:.3f}", timeout=self._timeouts.move
            )
            await self._q.send("M400", timeout=self._timeouts.move)
        finally:
            await self._q.send("G90", timeout=self._timeouts.diagnostics)

    @asynccontextmanager
    async def _soft_endstops_disabled(self) -> AsyncIterator[None]:
        result = await self._q.send("M211", timeout=self._timeouts.diagnostics)
        text = " ".join(result.raw).upper()
        words = re.search(r"SOFT ENDSTOPS:\s*(ON|OFF)", text)
        command = re.search(r"\bM211\s+S([01])\b", text)
        if words:
            was_enabled = words.group(1) == "ON"
        elif command:
            was_enabled = command.group(1) == "1"
        else:
            raise ValueError("cannot determine M211 soft-endstop state; no motion was attempted")
        await self._q.send("M211 S0", timeout=self._timeouts.diagnostics)
        try:
            yield
        finally:
            await self._q.send(
                f"M211 S{1 if was_enabled else 0}", timeout=self._timeouts.diagnostics
            )

    def _validate_motion(self, valve: ValveConfig, distance: float, feedrate: float) -> None:
        axis = self._map.axis(valve.axis)
        if not math.isfinite(distance) or distance == 0:
            raise ValueError("distance must be finite and non-zero")
        if not math.isfinite(feedrate) or feedrate <= 0:
            raise ValueError("feedrate must be finite and positive")
        if feedrate > min(axis.feedrate_max, 120.0):
            raise ValueError("commissioning feedrate exceeds the 120 units/min safety cap")

    async def service_jog(
        self, name: str, delta: float, feedrate: float, channel: str
    ) -> dict[str, Any]:
        valve = self.valve(name)
        return await self.service_jog_axis(valve.axis, delta, feedrate, channel, name)

    async def service_jog_axis(
        self,
        axis: str,
        delta: float,
        feedrate: float,
        channel: str,
        label: str | None = None,
        release_motor: bool = True,
    ) -> dict[str, Any]:
        axis_cfg = self._map.axis(axis)
        if len(axis) != 1 or axis == "E":
            raise ValueError("commissioning supports single-letter non-extruder axes only")
        if not math.isfinite(delta) or delta == 0:
            raise ValueError("distance must be finite and non-zero")
        if not math.isfinite(feedrate) or not 0 < feedrate <= min(axis_cfg.feedrate_max, 120.0):
            raise ValueError("commissioning feedrate must be positive and at most 120 units/min")
        if abs(delta) > 20.0:
            raise ValueError("service jog is limited to 20 axis units per confirmed command")
        async with self._lock:
            before = await self.endstop(channel)
            completed = False
            try:
                async with self._soft_endstops_disabled():
                    await self._relative(axis, delta, feedrate)
                after = await self.endstop(channel)
                completed = True
                return {
                    "ok": True,
                    "device": label or axis_cfg.name,
                    "axis": axis,
                    "delta": delta,
                    "endstop_before": before,
                    "endstop_after": after,
                    "motor_released": release_motor,
                    "position_trusted": False,
                }
            finally:
                if release_motor or not completed:
                    await self._q.send(f"M18 {axis}", timeout=self._timeouts.diagnostics)
                self._motion.invalidate([axis])

    async def measure_period(self, axis: str, channel: str, direction: int,
                             step: float, feedrate: float, max_distance: float):
        cfg = self._map.axis(axis)
        if cfg.kind != "peristaltic_pump" or len(axis) != 1:
            raise ValueError("period measurement requires a single-letter peristaltic pump axis")
        if (direction not in {-1, 1} or not math.isfinite(step) or not 0 < step <= 1
                or not math.isfinite(max_distance) or not 0 < max_distance <= cfg.travel
                or not math.isfinite(feedrate) or not 0 < feedrate <= min(cfg.feedrate_max, 120)):
            raise ValueError("check direction, step (0..1), travel and feedrate (max 120)")
        async with self._lock:
            last = await self.endstop(channel)
            first = None
            travelled = 0.0
            try:
                async with self._soft_endstops_disabled():
                    while travelled + step <= max_distance + 1e-9:
                        await self._relative(axis, direction * step, feedrate)
                        travelled += step
                        now = await self.endstop(channel)
                        if now and not last:
                            if first is not None:
                                distance = travelled - first
                                return {"ok": True, "axis": axis, "channel": channel,
                                        "direction": direction, "period_axis_units": distance,
                                        "commanded_pulses_per_period": distance * cfg.steps_per_unit,
                                        "sampling_step_units": step, "total_travel": travelled,
                                        "warning": "Commanded pulses, not encoder feedback; uncertainty up to one sampling step. Sensor is not a hard stop."}
                            first = travelled
                        last = now
                raise ValueError("two rising edges were not reached within the configured travel")
            finally:
                self._motion.invalidate([axis])
                await self._q.send(f"M18 {axis}", timeout=self._timeouts.diagnostics)

    async def home_stepwise(
        self,
        name: str,
        channel: str,
        direction: int,
        step: float,
        feedrate: float,
        search_distance: float,
        pull_off: float,
        release_motor: bool,
        release_distance: float | None = None,
    ) -> dict[str, Any]:
        valve = self.valve(name)
        axis_cfg = self._map.axis(valve.axis)
        self._validate_motion(valve, direction * step, feedrate)
        if direction not in {-1, 1}:
            raise ValueError("direction must be -1 or 1")
        if not 0 < step <= 1.0:
            raise ValueError("search step must be in (0, 1] axis units")
        if not 0 < search_distance <= axis_cfg.travel:
            raise ValueError("search distance must fit configured axis travel")
        if not 0 < pull_off < axis_cfg.travel:
            raise ValueError("pull-off must be positive and smaller than configured axis travel")
        release_limit = (min(search_distance, axis_cfg.travel - pull_off)
                         if release_distance is None else release_distance)
        if not 0 < release_limit <= 20 - pull_off:
            raise ValueError("release distance plus pull-off must be positive and no more than 20 axis units")

        async with self._lock:
            travelled = 0.0
            released = 0.0
            initial_release = 0.0
            completed = False
            try:
                async with self._soft_endstops_disabled():
                    # A previous interrupted attempt may leave this same valve on
                    # its switch. Recover automatically by moving only the selected
                    # valve in the known away direction. If another parallel switch
                    # is responsible, the bounded attempt fails without moving any
                    # other device.
                    while await self.endstop(channel):
                        if initial_release + step > release_limit + 1e-9:
                            raise ValueError(
                                "shared endstop stayed triggered during automatic recovery"
                            )
                        await self._relative(valve.axis, -direction * step, feedrate)
                        initial_release += step

                    while travelled + step <= search_distance + 1e-9:
                        await self._relative(valve.axis, direction * step, feedrate)
                        travelled += step
                        if await self.endstop(channel):
                            break
                    else:
                        raise ValueError("endstop was not reached within the configured search distance")

                    await self._q.send(f"G92 {valve.axis}0", timeout=self._timeouts.diagnostics)
                    # A real microswitch may remain pressed for much more than two
                    # configured axis units.  Keep backing away automatically up to
                    # the separately configured release distance. The direction
                    # is known to be away from the switch, soft endstops are restored
                    # by the surrounding context manager, and any failure releases
                    # the motor in ``finally`` below.
                    while await self.endstop(channel):
                        if released + step > release_limit + 1e-9:
                            raise ValueError(
                                "endstop did not release within the configured release distance"
                            )
                        await self._relative(valve.axis, -direction * step, feedrate)
                        released += step
                    await self._relative(valve.axis, -direction * pull_off, feedrate)
                    released += pull_off

                position = -direction * released
                await self._q.send(
                    f"G92 {valve.axis}{position:.4f}", timeout=self._timeouts.diagnostics
                )
                self._motion.mark_referenced(valve.axis, position)
                completed = True
                return {
                    "ok": True,
                    "valve": name,
                    "axis": valve.axis,
                    "initial_release": -direction * initial_release,
                    "search_travel": direction * travelled,
                    "position": position,
                    "endstop": await self.endstop(channel),
                    "motor_released": release_motor,
                }
            finally:
                if release_motor or not completed:
                    await self._q.send(f"M18 {valve.axis}", timeout=self._timeouts.diagnostics)
                    self._motion.invalidate([valve.axis])
