"""High-level G-code sender.

Wraps the command queue with semantic operations: jog, move, home, read state.
Issues `M400` before any `M114` we actually care about.
"""

from __future__ import annotations

import logging
import math
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from flowengine.hardware.homing.base import HomingStrategy
from flowengine.hardware.motion import MotionModel
from flowengine.hardware.queue import CommandQueue, CommandResult
from flowengine.schemas.device import DeviceMap
from flowengine.schemas.procedure import NumberValue, PumpDose
from flowengine.schemas.runtime import FirmwareExpectation, Timeouts

log = logging.getLogger(__name__)


class GcodeSender:
    def __init__(
        self,
        queue: CommandQueue,
        motion: MotionModel,
        device_map: DeviceMap,
        homing: HomingStrategy,
        timeouts: Timeouts,
        firmware: FirmwareExpectation | None = None,
        accel_cap: float | None = None,
    ) -> None:
        self._q = queue
        self._motion = motion
        self._map = device_map
        self._homing = homing
        self._timeouts = timeouts
        self._absolute = True
        self._firmware = firmware or FirmwareExpectation()
        self._accel_cap = accel_cap
        self._probe_verified = self._firmware.probe_target_verified
        self._counts: dict[str, int] = {}

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
            if axis.accel_max is not None:
                cap = min(axis.accel_max, self._accel_cap or axis.accel_max)
                await self._q.send(
                    f"M201 {axis.marlin_axis}{cap:.3f}", timeout=self._timeouts.diagnostics
                )

    def convert_move(
        self,
        axis: str,
        value: NumberValue | None,
        feedrate: NumberValue | None = None,
        units: str = "axis",
        speed_units: str = "axis/min",
    ) -> tuple[float, float | None]:
        if value is None:
            raise ValueError("movement value required")
        value = float(value)
        cfg = self._map.axis(axis)
        target = value / cfg.steps_per_unit if units == "steps" else value
        speed = float(feedrate) if feedrate is not None else None
        if speed is not None:
            if speed_units == "steps/s":
                speed = 60 * speed / cfg.steps_per_unit
            elif speed_units == "axis/s":
                speed *= 60
        return target, speed

    @asynccontextmanager
    async def acceleration(self, value: NumberValue | None, axes: list[str]) -> AsyncIterator[None]:
        """Scoped planner setting. Never persist EEPROM; read and restore P/R/T."""
        if value is None:
            yield
            return
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("acceleration must be finite and positive")
        caps = [self._map.axis(a).accel_max for a in axes]
        cap = min(
            [value]
            + [c for c in caps if c is not None]
            + ([self._accel_cap] if self._accel_cap is not None else [])
        )
        settings = await self._q.send("M503", timeout=self._timeouts.diagnostics)
        line = next((s for s in settings.raw if re.search(r"\bM204\b", s)), "")
        saved = {k: v for k, v in re.findall(r"([PRT])([0-9.]+)", line)}
        if set(saved) != {"P", "R", "T"}:
            raise ValueError("cannot read M204 P/R/T; acceleration not changed")
        await self._q.send(f"M204 P{cap:.3f} T{cap:.3f}", timeout=self._timeouts.diagnostics)
        try:
            yield
        finally:
            original_error = sys.exception()
            try:
                await self._q.send(
                    "M204 " + " ".join(k + v for k, v in saved.items()),
                    timeout=self._timeouts.diagnostics,
                )
            except Exception:
                self._motion.invalidate()
                log.exception(
                    "could not restore acceleration; reconnect/read settings before further motion"
                )
                if original_error is None:
                    raise

    async def move_multi(
        self,
        axes: dict[str, NumberValue],
        feedrate: NumberValue | None = None,
        relative: bool = False,
        duration_s: NumberValue | None = None,
    ) -> dict[str, Any]:
        axes = {a: float(v) for a, v in axes.items()}
        feedrate = float(feedrate) if feedrate is not None else None
        duration_s = float(duration_s) if duration_s is not None else None
        if not axes:
            raise ValueError("no axes selected")
        if len({self._map.axis(a).units for a in axes}) != 1:
            raise ValueError("coordinated movement requires matching axis units")
        if any(len(a) != 1 or a == "E" for a in axes):
            raise ValueError(
                "coordinated movement requires verified single-letter non-extruder axes"
            )
        for group in self._map.endstop_inputs:
            if len(set(axes) & set(group.axes)) > 1:
                raise ValueError(f"axes share input {group.channel}; simultaneous movement refused")
        positions = self._motion.positions
        plans = {
            a: (
                self._motion.plan_relative(a, v, None)
                if relative
                else self._motion.plan_absolute(a, v, None)
            )
            for a, v in axes.items()
        }
        deltas = {a: p.target - positions[a] for a, p in plans.items()}
        length = math.sqrt(sum(d * d for d in deltas.values()))
        if not length:
            raise ValueError("coordinated movement has zero length")
        if duration_s is not None:
            if not math.isfinite(duration_s) or duration_s <= 0:
                raise ValueError("duration_s must be finite and positive")
            speed = 60 * length / duration_s
        else:
            speed = feedrate if feedrate is not None else min(p.feedrate for p in plans.values())
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("feedrate must be finite and positive")
        for a, d in deltas.items():
            component = speed * abs(d) / length
            checked = self._motion.plan_absolute(a, plans[a].target, component or None)
            if component > checked.feedrate + 1e-9:
                raise ValueError(
                    f"requested component speed exceeds limit on {a}; increase duration"
                )
        await self._q.send("G90", timeout=self._timeouts.diagnostics)
        await self._q.send(
            "G1 " + " ".join(f"{a}{p.target:.4f}" for a, p in plans.items()) + f" F{speed:.3f}",
            timeout=self._timeouts.move,
        )
        for a, p in plans.items():
            self._motion.accept_target(a, p.target)
        return {
            "nominal_duration_s": 60 * length / speed,
            "feedrate": speed,
            "warning": "duration excludes acceleration/deceleration; not independent planners",
        }

    async def pump(
        self,
        name: str,
        volume_ul: NumberValue,
        flow_ul_min: NumberValue,
        direction: str = "dispense",
    ) -> None:
        volume_ul, flow_ul_min = float(volume_ul), float(flow_ul_min)
        pump = next((p for p in self._map.pumps if p.name == name), None)
        if pump is None or not pump.calibrated:
            raise ValueError("pump is unknown or not calibrated")
        if not all(math.isfinite(x) and x > 0 for x in (volume_ul, flow_ul_min)):
            raise ValueError("volume and flow must be finite and positive")
        sign = pump.dispense_direction * (1 if direction == "dispense" else -1)
        requested = flow_ul_min / pump.volume_per_unit_ul
        checked = self._motion.plan_relative(
            pump.axis, sign * volume_ul / pump.volume_per_unit_ul, requested
        )
        if checked.feedrate < requested:
            raise ValueError("requested pump flow exceeds configured speed limit")
        await self.jog(
            pump.axis,
            sign * volume_ul / pump.volume_per_unit_ul,
            flow_ul_min / pump.volume_per_unit_ul,
        )

    async def motors(self, enabled: bool, axes: list[str] | None = None) -> None:
        selected = axes if axes is not None else [a.marlin_axis for a in self._map.axes]
        if not selected:
            raise ValueError("select at least one axis")
        for a in selected:
            self._map.axis(a)
            if len(a) != 1:
                raise ValueError("motor control requires single-letter firmware axes")
        if not enabled:
            self._motion.invalidate(selected)
        await self.wait_idle()
        await self._q.send(
            ("M17" if enabled else "M18") + " " + " ".join(selected),
            timeout=self._timeouts.diagnostics,
        )

    async def pump_multi(self, doses: dict[str, PumpDose]) -> dict[str, Any]:
        if len(doses) < 2:
            raise ValueError("coordinated pump step requires at least two pumps")
        axes = {}
        durations = []
        for name, dose in doses.items():
            pump = next((p for p in self._map.pumps if p.name == name), None)
            if pump is None or not pump.calibrated:
                raise ValueError(f"pump {name} is unknown or uncalibrated")
            if pump.axis in axes:
                raise ValueError("two pumps cannot use the same axis in one coordinated step")
            volume, flow = float(dose.volume_ul), float(dose.flow_ul_min)
            if not all(math.isfinite(v) and v > 0 for v in (volume, flow)):
                raise ValueError("positive finite volume/flow required")
            sign = pump.dispense_direction * (1 if dose.direction == "dispense" else -1)
            axes[pump.axis] = sign * volume / pump.volume_per_unit_ul
            durations.append(60 * volume / flow)
        if max(durations) - min(durations) > max(durations) * 1e-6:
            raise ValueError(
                "pump volumes/flows imply different finish times; split into coordinated segments"
            )
        return await self.move_multi(axes, relative=True, duration_s=durations[0])

    async def abort(self) -> None:
        self._motion.invalidate()
        await self._q.abort()

    def invalidate_positions(self) -> None:
        self._motion.invalidate()

    def validate_raw(self, command: str, confirmed: bool = False) -> tuple[str, bool]:
        command = command.strip()
        if not command or len(command) > 120 or any(not 32 <= ord(c) < 127 for c in command):
            raise ValueError("send one printable line, maximum 120 characters")
        if not re.match(r"^[GMT]\d+(?:\.\d+)?(?:\s|$)", command, re.I) or "*" in command:
            raise ValueError("unframed G/M/T command required")
        token = command.split()[0].upper()
        readonly = token in {"M105", "M114", "M115", "M119", "M122", "M503"}
        if not readonly and not confirmed:
            raise ValueError("confirm raw command: it may move hardware or change settings")
        return command, readonly

    async def raw(self, command: str, confirmed: bool = False) -> CommandResult:
        command, readonly = self.validate_raw(command, confirmed)
        if not readonly:
            self._motion.invalidate()
        return await self._q.send(command, timeout=self._timeouts.move)

    async def sensors(self) -> dict[str, Any]:
        result = await self._q.send("M105", timeout=self._timeouts.diagnostics)
        found = {
            k: float(v)
            for line in result.raw
            for k, v in re.findall(r"\b(T\d*|B|C|ADC\d+):\s*(-?\d+(?:\.\d+)?)", line)
        }
        return {
            "raw": result.raw,
            "values": [
                {
                    "name": s.name,
                    "source": s.source,
                    "units": s.units,
                    "value": found[s.source] * s.scale + s.offset if s.source in found else None,
                    "available": s.source in found,
                }
                for s in self._map.analog_inputs
            ],
        }

    async def seek(
        self, axis: str, channel: str, delta: NumberValue, feedrate: NumberValue, zero: bool = False
    ) -> dict[str, Any]:
        delta, feedrate = float(delta), float(feedrate)
        """Firmware-stopped contact search; never substitute host M119 polling."""
        if (
            not self._probe_verified
            or axis not in self._firmware.verified_probe_axes
            or channel != self._firmware.probe_channel
        ):
            raise ValueError("G38 probe mapping is not verified for this axis/channel")
        self._map.axis(axis)
        if len(axis) != 1 or axis == "E":
            raise ValueError("unsupported probe axis")
        if not all(math.isfinite(x) for x in (delta, feedrate)) or delta == 0 or feedrate <= 0:
            raise ValueError("seek delta must be nonzero; feedrate positive and finite")
        cfg = self._map.axis(axis)
        if abs(delta) > cfg.travel or feedrate > self._motion.max_feedrate(axis):
            raise ValueError("seek exceeds configured travel/speed")
        if zero and ((delta < 0) != (cfg.home_direction == "min")):
            raise ValueError("zero search direction must match configured home direction")
        switches = await self.read_endstops()
        if channel not in switches or switches[channel]:
            raise ValueError(
                "probe input unavailable or already triggered; release all shared switches"
            )
        start = (await self.wait_idle())[axis]
        start_count = self._counts.get(axis)
        self._motion.invalidate([axis])
        await self._q.send("G90", timeout=self._timeouts.diagnostics)
        await self._q.send(
            f"G38.2 {axis}{start + delta:.4f} F{feedrate:.3f}", timeout=self._timeouts.homing
        )
        end = (await self.wait_idle())[axis]
        end_count = self._counts.get(axis)
        switches = await self.read_endstops()
        if not switches.get(channel) or end == start:
            raise ValueError("contact was not confirmed; firmware may have ignored G38")
        if zero:
            await self._q.send(f"G92 {axis}0", timeout=self._timeouts.diagnostics)
            self._motion.mark_homed([axis])
        return {
            "axis": axis,
            "travel_units": end - start,
            "step_pulses": end_count - start_count
            if end_count is not None and start_count is not None
            else (end - start) * cfg.steps_per_unit,
            "end_step_counter": end_count,
            "position": end,
            "measurement_source": "firmware STEP counter"
            if end_count is not None and start_count is not None
            else "rounded M114 position x configured steps/unit",
        }

    async def _probe_backoff(self, axis: str, delta: float, channel: str) -> None:
        """Bounded commissioning motion away from the just-confirmed switch."""
        cfg = self._map.axis(axis)
        if not math.isfinite(delta) or not 0 < abs(delta) <= cfg.travel:
            raise ValueError("invalid bounded backoff")
        position = (await self.wait_idle())[axis]
        target = position + delta
        lo, hi = (0, cfg.travel) if cfg.home_direction == "min" else (-cfg.travel, 0)
        if not lo <= target <= hi:
            raise ValueError("backoff outside configured bounds")
        await self._q.send("G90", timeout=self._timeouts.diagnostics)
        await self._q.send(
            f"G1 {axis}{target:.4f} F{min(cfg.feedrate_default, self._motion.max_feedrate(axis), 60):.3f}",
            timeout=self._timeouts.move,
        )
        await self.wait_idle()
        switches = await self.read_endstops()
        if channel not in switches or switches[channel]:
            raise ValueError("shared probe did not release after backoff")

    async def calibrate_valve(
        self,
        axis: str,
        channel: str,
        search_distance: float,
        feedrate: float,
        backoff: float,
        repeats: int = 3,
    ) -> dict[str, Any]:
        cfg = self._map.axis(axis)
        if (
            not all(math.isfinite(v) and v > 0 for v in (search_distance, feedrate, backoff))
            or backoff >= search_distance
        ):
            raise ValueError("positive distances/speed required; backoff < search distance")
        sign = -1 if cfg.home_direction == "min" else 1
        origin = await self.seek(axis, channel, sign * search_distance, feedrate, True)
        measurements = []
        for _ in range(repeats):
            await self._probe_backoff(axis, -sign * backoff, channel)
            far = await self.seek(axis, channel, -sign * search_distance, feedrate)
            span = (
                abs(far["end_step_counter"] - origin["end_step_counter"])
                if far["end_step_counter"] is not None and origin["end_step_counter"] is not None
                else abs(far["position"]) * cfg.steps_per_unit
            )
            measurements.append(span)
            await self._probe_backoff(axis, sign * backoff, channel)
            origin = await self.seek(axis, channel, sign * search_distance, feedrate, True)
        await self._probe_backoff(axis, -sign * backoff, channel)
        return {
            "axis": axis,
            "spans_step_pulses": measurements,
            "mean_step_pulses": sum(measurements) / len(measurements),
            "spread_step_pulses": max(measurements) - min(measurements),
            "suggested_travel_units": sum(measurements) / len(measurements) / cfg.steps_per_unit,
            "applied": False,
            "warning": "Confirm endpoints individually; firmware must stop on both. No automatic profile change.",
            "measurement_source": origin["measurement_source"],
        }

    async def test_reference(
        self,
        axis: str,
        channel: str,
        delta: float,
        feedrate: float,
        reference_feedrate: float,
        backoff: float,
        tolerance_pulses: float,
        repeats: int = 3,
        acceleration: float | None = None,
    ) -> dict[str, Any]:
        from flowengine.calibration import reference_error

        cfg = self._map.axis(axis)
        sign = -1 if cfg.home_direction == "min" else 1
        if (
            not math.isfinite(delta)
            or delta * sign >= 0
            or abs(delta) + backoff > cfg.travel
            or not 0 < backoff < abs(delta)
        ):
            raise ValueError(
                "round trip must go away from home, inside travel; positive backoff < displacement"
            )
        if not math.isfinite(tolerance_pulses) or tolerance_pulses < 0:
            raise ValueError("nonnegative finite tolerance required")
        await self.seek(axis, channel, sign * cfg.travel, reference_feedrate, True)
        corrections = []
        for i in range(repeats + 1):
            await self._probe_backoff(axis, -sign * backoff, channel)
            if i:
                async with self.acceleration(acceleration, [axis]):
                    await self.jog(axis, delta, feedrate)
                    await self.wait_idle()
                    await self.jog(axis, -delta, feedrate)
                    await self.wait_idle()
            result = await self.seek(
                axis, channel, sign * min(cfg.travel, backoff * 3), reference_feedrate, True
            )
            corrections.append(abs(result["step_pulses"]))
        await self._probe_backoff(axis, -sign * backoff, channel)
        return {
            "axis": axis,
            "baseline_step_pulses": corrections[0],
            "corrections_step_pulses": corrections[1:],
            **reference_error(corrections[0], corrections[1:], tolerance_pulses),
        }

    async def jog(self, axis: str, delta: float, feedrate: float | None = None) -> None:
        p = self._motion.plan_relative(axis, delta, feedrate)
        # Never trust the modal state left by another host/session.
        await self._q.send("G90", timeout=self._timeouts.diagnostics)
        await self._q.send(
            f"G1 {axis}{p.target:.4f} F{p.feedrate:.2f}",
            timeout=self._timeouts.move,
        )
        self._motion.accept_target(axis, p.target)

    async def move_to(self, axis: str, target: float, feedrate: float | None = None) -> None:
        p = self._motion.plan_absolute(axis, target, feedrate)
        await self._q.send("G90", timeout=self._timeouts.diagnostics)
        await self._q.send(
            f"G1 {axis}{p.target:.4f} F{p.feedrate:.2f}",
            timeout=self._timeouts.move,
        )
        self._motion.accept_target(axis, p.target)

    async def home(self, axes: list[str] | None = None) -> None:
        targets = axes if axes is not None else [a.marlin_axis for a in self._map.axes]
        for a in targets:
            if self._map.axis(a).homing_strategy != "endstop":
                raise ValueError("sensorless/crash homing is not implemented")
            if any(a in g.axes and len(g.axes) > 1 for g in self._map.endstop_inputs):
                raise ValueError(
                    "shared-input home requires a verified sequential seek procedure, not G28"
                )
        self._motion.invalidate(targets)
        homed = await self._homing.home(self._q, axes)
        self._motion.mark_homed(homed)

    async def wait_idle(self) -> dict[str, float]:
        """Block until queued motion completes, then read settled position."""
        await self._q.send("M400", timeout=self._timeouts.move)
        result = await self._q.send("M114", timeout=self._timeouts.diagnostics)
        self._counts = {
            a: int(v)
            for line in result.raw
            if "Count " in line
            for a, v in re.findall(r"([A-Z]):(-?\d+)", line.split("Count ", 1)[1])
        }
        if result.position:
            self._motion.update_position(result.position)
        return self._motion.positions

    async def read_endstops(self) -> dict[str, bool]:
        result = await self._q.send("M119", timeout=self._timeouts.diagnostics)
        return result.endstops or {}

    async def read_diagnostic(self, command: str) -> CommandResult:
        """Run an allow-listed read-only Marlin diagnostic command."""
        if command not in {"M114", "M119", "M503", "M122", "M105"}:
            raise ValueError(f"unsafe diagnostic command: {command}")
        result = await self._q.send(command, timeout=self._timeouts.diagnostics)
        if result.position:
            self._motion.update_position(result.position)
        return result

    async def read_firmware(self) -> tuple[str, frozenset[str]]:
        result = await self._q.send("M115", timeout=self._timeouts.diagnostics)
        return (result.firmware_raw or "", result.firmware_caps or frozenset())
