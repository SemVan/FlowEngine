"""High-level G-code sender.

Wraps the command queue with semantic operations: jog, move, home, read state.
Issues `M400` before any `M114` we actually care about.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from flowengine.errors import ControllerError, StateError
from flowengine.hardware.coordinated import independent_segments, timed_pump_moves
from flowengine.hardware.endstop_seek import seek_switch
from flowengine.hardware.group_seek import find_group_edges, move_group_away
from flowengine.hardware.homing.base import HomingStrategy
from flowengine.hardware.motion import MotionModel
from flowengine.hardware.queue import CommandQueue, CommandResult
from flowengine.hardware.valve_commissioning import ValveCommissioning
from flowengine.schemas.device import AxisCalibration, DeviceMap
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
        from flowengine.parking_store import discard_on_command
        queue.before_command = lambda command: discard_on_command(queue.transport_name, command)
        self._motion = motion
        self._map = device_map
        self._homing = homing
        self._timeouts = timeouts
        self._absolute = True
        self._firmware = firmware or FirmwareExpectation()
        self._accel_cap = accel_cap
        self._probe_verified = self._firmware.probe_target_verified
        self._counts: dict[str, int] = {}
        self._reference_counts: dict[str, int] = {}
        self._reference_sides: dict[str, str] = {}
        self._expected: dict[str, float] = {}
        self.manual_session: dict[str, Any] | None = None
        self.manual_lock = asyncio.Lock()
        self.reference_epoch = 0
        self._home_cancel = asyncio.Event()
        self._home_active = False
        self.reference_hold_active = False
        self.auto_calibration_job: dict[str, Any] | None = None
        self.step_check_job: dict[str, Any] | None = None
        from flowengine.hardware.valves import ValveSubsystem
        self.valves = ValveSubsystem(self)
        self.valve_commissioning = ValveCommissioning(queue, motion, device_map, timeouts)

    async def set_valve(self, name: str, position: str, feedrate: float | None = None) -> None:
        await self.valves.set_valve(name, position, feedrate)

    async def home_valve(self, name: str):
        await self.hold_references()
        return await self.valves.home_valve(name)

    async def hold_references(self) -> None:
        if not self.reference_hold_active:
            # иначе прошивка может сама отпустить моторы между процедурами
            await self._q.send("M84 S0", timeout=self._timeouts.diagnostics)
            self.reference_hold_active = True

    async def finish_reference_session(self) -> None:
        if not self.reference_hold_active:
            return
        try:
            await self.motors(False)
        except Exception:
            await self.abort()
            for axis in self._map.axes:
                if len(axis.marlin_axis) == 1:
                    await self._q.request_motor_release(axis.marlin_axis)

    async def _check_manual_endstops(self, axes: list[str]) -> None:
        guarded = [a for a in axes if self._map.axis(a).axis_calibration is not None]
        if not guarded:
            return
        switches = await self.read_endstops()
        for axis in guarded:
            if switches.get(f"{axis.lower()}_min") is not False:
                self._motion.invalidate(guarded)
                raise ControllerError(
                    f"{axis}: концевик нажат или недоступен. Обычное движение запрещено; "
                    "восстановите reference через штатный Home/Reference."
                )

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
        axis_feedrates: dict[str, NumberValue] | None = None,
    ) -> dict[str, Any]:
        axes = {a: float(v) for a, v in axes.items()}
        self.valves.reject_axes(list(axes))
        if axis_feedrates is not None:
            if feedrate is not None or duration_s is not None:
                raise ValueError("choose individual speeds OR common feedrate/duration")
            return await self.move_independent(axes, axis_feedrates, relative)
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
        await self._check_manual_endstops(list(axes))
        await self._q.send("G90", timeout=self._timeouts.diagnostics)
        await self._q.send(
            "G1 " + " ".join(f"{a}{p.target:.4f}" for a, p in plans.items()) + f" F{speed:.3f}",
            timeout=self._timeouts.move,
        )
        for a, p in plans.items():
            self._motion.accept_target(a, p.target)
            self._expected[a] = p.target
        return {
            "nominal_duration_s": 60 * length / speed,
            "feedrate": speed,
            "warning": "duration excludes acceleration/deceleration; not independent planners",
        }

    async def move_multi_for(self, axes, seconds):
        seconds = float(seconds)
        distances, speeds = timed_pump_moves(self._map, axes, seconds, self._motion._feedrate_cap)
        result = await self.move_independent(distances, speeds, relative=True, common_duration_s=seconds)
        return {**result, "common_interval": True,
                "warning": "Насосы работают одним совместным ходом. Время номинальное: разгон и торможение добавляют время; объём и расход в мкл требуют измерения."}

    async def move_independent(self, axes, axis_feedrates, relative=False, *, common_duration_s=None):
        self.valves.reject_axes(list(axes))
        if not axes or set(axes) != set(axis_feedrates):
            raise ValueError("Задайте скорость для каждой выбранной оси")
        if any(len(a) != 1 or a == "E" for a in axes):
            raise ValueError("Совместный ход требует отдельных буквенных осей")
        if len({self._map.axis(a).units for a in axes}) != 1:
            raise ValueError("Для совместного хода нужны одинаковые единицы осей")
        if any(len(set(axes) & set(g.axes)) > 1 for g in self._map.endstop_inputs):
            raise ValueError("Оси используют общий вход концевиков")
        speeds = {a: float(v) for a, v in axis_feedrates.items()}
        for axis, speed in speeds.items():
            if not math.isfinite(speed) or not 0 < speed <= self._motion.max_feedrate(axis):
                raise ValueError(f"{axis}: скорость должна быть больше 0 и не выше {self._motion.max_feedrate(axis):g} ед/мин")
            if relative:
                self._motion.plan_relative(axis, float(axes[axis]), speed)
            else:
                self._motion.plan_absolute(axis, float(axes[axis]), speed)
        await self._check_manual_endstops(list(axes))
        starts = await self.wait_idle()
        starts = {a: starts[a] for a in axes}
        targets = {a: starts[a] + float(v) if relative else float(v) for a, v in axes.items()}
        tolerance = {a: max(.001, .5 / self._map.axis(a).steps_per_unit + .0001) for a in axes}
        for a in axes:
            if abs(targets[a] - starts[a]) <= tolerance[a]:
                starts[a] = targets[a]
        segments, durations = independent_segments(starts, targets, speeds, common_duration_s)
        deadline = asyncio.get_running_loop().time() + max(durations.values()) + max(5, self._timeouts.move)
        try:
            if segments:
                await self._q.send("G90", timeout=self._timeouts.diagnostics)
            for segment in segments:
                await self._check_manual_endstops(list(axes))
                await self._q.send("G1 " + " ".join(f"{a}{v:.6f}" for a, v in segment["targets"].items())
                                   + f" F{segment['feedrate']:.3f}", timeout=self._timeouts.move)
                for a, value in segment["targets"].items():
                    self._motion.accept_target(a, value)
                    self._expected[a] = value
            while segments:
                await self._check_manual_endstops(list(axes))
                report = await self._q.send("M114 R", timeout=self._timeouts.diagnostics)
                actual = report.position or {}
                if not targets.keys() <= actual.keys():
                    raise ControllerError("Плата не сообщила координаты всех насосов")
                if all(abs(actual[a] - targets[a]) <= tolerance[a] for a in targets):
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise ControllerError("Совместный ход насосов не завершился за отведённое время")
                await asyncio.sleep(.04)
            await self._check_manual_endstops(list(axes))
            actual = await self.wait_idle()
        except BaseException:
            await self.abort()
            raise
        return {"nominal_duration_s": max(durations.values()), "segments": segments,
                "axes": {a: {"target": targets[a], "feedrate": speeds[a],
                               "nominal_duration_s": durations[a], "position": actual[a]} for a in axes},
                "warning": "У каждого насоса своя скорость. Разгон, торможение и переходы между участками влияют на время."}

    async def move_for(self, axis: str, direction: int, feedrate: float, seconds: float,
                       speed_units: str = "axis/min") -> dict[str, Any]:
        self.valves.reject_axes([axis])
        _, speed = self.convert_move(axis, 0, feedrate, speed_units=speed_units)
        seconds = float(seconds)
        if direction not in {-1, 1} or speed is None or not math.isfinite(speed) or speed <= 0:
            raise ValueError("positive finite speed and direction +1/-1 required")
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("seconds must be finite and positive")
        distance = direction * speed * seconds / 60
        plan = self._motion.plan_relative(axis, distance, speed)
        if speed > plan.feedrate + 1e-9:
            raise ValueError(f"{axis}: заданная скорость выше предела; уменьшите скорость")
        await self.jog(axis, distance, speed)
        return {"axis": axis, "distance": distance, "feedrate": speed,
                "nominal_duration_s": seconds, "warning": "время без учёта разгона и торможения"}

    def _check_pump_route(self, pump, direction: str) -> None:
        if not pump.allowed_routes:
            return
        required = pump.allowed_routes.get(direction)
        if not required:
            raise ValueError(f"pump {pump.name}: no verified route for {direction}")
        for name, positions in required.items():
            valve = next((v for v in self._map.valves if v.name == name), None)
            if valve is None or not valve.calibrated or not self._motion.homed[valve.axis]:
                raise ValueError(f"pump {pump.name}: valve {name} is not calibrated/referenced")
            if not any(abs(self._motion.positions[valve.axis] - valve.position(p)) <= 1e-3
                       for p in positions):
                raise ValueError(f"pump {pump.name}: valve {name} is outside its allowed route")

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
        self._check_pump_route(pump, direction)
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
        if self.valves.lock.locked():
            raise StateError("Valve motion active; motor configuration blocked")
        if not selected:
            raise ValueError("select at least one axis")
        for a in selected:
            self._map.axis(a)
            if len(a) != 1:
                raise ValueError("motor control requires single-letter firmware axes")
        if not enabled:
            self.valves.invalidate(selected)
            self._motion.invalidate(selected)
            for axis in selected:
                self._reference_counts.pop(axis, None)
                self._reference_sides.pop(axis, None)
        await self.wait_idle()
        await self._q.send(
            ("M17" if enabled else "M18") + " " + " ".join(selected),
            timeout=self._timeouts.diagnostics,
        )
        if not enabled and not any(self._motion.homed.values()) and self.reference_hold_active:
            await self._q.send("M84 S120", timeout=self._timeouts.diagnostics)
            self.reference_hold_active = False

    async def pump_multi(self, doses: dict[str, PumpDose]) -> dict[str, Any]:
        if len(doses) < 2:
            raise ValueError("coordinated pump step requires at least two pumps")
        axes = {}
        durations = []
        for name, dose in doses.items():
            pump = next((p for p in self._map.pumps if p.name == name), None)
            if pump is None or not pump.calibrated:
                raise ValueError(f"pump {name} is unknown or uncalibrated")
            self._check_pump_route(pump, dose.direction)
            if pump.axis in axes:
                raise ValueError("two pumps cannot use the same axis in one coordinated step")
            volume, flow = float(dose.volume_ul), float(dose.flow_ul_min)
            if not all(math.isfinite(v) and v > 0 for v in (volume, flow)):
                raise ValueError("positive finite volume/flow required")
            sign = pump.dispense_direction * (1 if dose.direction == "dispense" else -1)
            axes[pump.axis] = sign * volume / pump.volume_per_unit_ul
            durations.append(60 * volume / flow)
        # Validate complete travel and individual speeds before sending ANY segment.
        if any(len(set(axes) & set(group.axes)) > 1 for group in self._map.endstop_inputs):
            raise ValueError("coordinated pumps share an endstop input")
        for (axis, distance), duration in zip(axes.items(), durations, strict=True):
            speed = 60 * abs(distance) / duration
            plan = self._motion.plan_relative(axis, distance, speed)
            if speed > plan.feedrate + 1e-9:
                raise ValueError(f"requested pump flow exceeds configured speed limit on {axis}")
        speeds = {axis: 60 * abs(distance) / duration for (axis, distance), duration in zip(axes.items(), durations, strict=True)}
        result = await self.move_independent(axes, speeds, relative=True)
        return {**result, "motion_complete": True,
                "pumps": {name: {"volume_ul": float(dose.volume_ul), "flow_ul_min": float(dose.flow_ul_min),
                                  "direction": dose.direction,
                                  "nominal_duration_s": 60 * float(dose.volume_ul) / float(dose.flow_ul_min)}
                          for name, dose in doses.items()}}

    @property
    def reference_counts(self) -> dict[str, int]:
        homed = self._motion.homed
        return {axis: count for axis, count in self._reference_counts.items() if homed.get(axis)}

    @property
    def reference_sides(self) -> dict[str, str]:
        homed = self._motion.homed
        return {axis: side for axis, side in self._reference_sides.items() if homed.get(axis)}

    async def abort(self) -> None:
        prepared_session = self.reference_hold_active
        release_valve = self.valves.active_service or self.valves.active_valve
        self.reference_epoch += 1
        self.manual_session = None
        self._expected.clear()
        self._reference_counts.clear()
        self._reference_sides.clear()
        self._motion.invalidate()
        self.valves.invalidate()
        try:
            await self._q.abort()
        finally:
            if release_valve:
                cfg = self.valves.config(release_valve)
                st = self.valves.states[release_valve]
                st.service_release = "UNKNOWN"
                # для E0/E3 нельзя безопасно выбрать один драйвер через M18
                written = False
                if len(cfg.axis) == 1:
                    written = await self._q.request_motor_release(cfg.axis)
                st.service_release = "REQUESTED_UNCONFIRMED" if written else "UNKNOWN"
            if prepared_session:
                for axis in self._map.axes:
                    if len(axis.marlin_axis) == 1:
                        await self._q.request_motor_release(axis.marlin_axis)
                try:
                    await self._q.request_stepper_timeout(120)
                except Exception:
                    log.warning("таймер отключения драйверов после Stop не подтверждён")
                self.reference_hold_active = False

    def invalidate_positions(self) -> None:
        self.valves.invalidate()
        self._reference_counts.clear()
        self._reference_sides.clear()
        self._motion.invalidate()

    def validate_raw(self, command: str, confirmed: bool = False) -> tuple[str, bool]:
        command = command.strip()
        if not command or len(command) > 120 or any(not 32 <= ord(c) < 127 for c in command):
            raise ValueError("send one printable line, maximum 120 characters")
        if not re.match(r"^[GMT]\d+(?:\.\d+)?(?:\s|$)", command, re.I) or "*" in command:
            raise ValueError("unframed G/M/T command required")
        token = command.split()[0].upper()
        if self.valves.lock.locked():
            raise StateError("Valve operation active; console blocked")
        valve_axes = {v.axis for v in self._map.valves}
        # сырые команды не должны обходить управление кранами
        if token.startswith("G"):
            args = command[len(token):].upper()
            mentioned = {a.marlin_axis for a in self._map.axes
                         if re.search(rf"{re.escape(a.marlin_axis)}(?=[+\-.\d\s]|$)", args)}
            if (float(token[1:]) == 28 and not mentioned) or mentioned & valve_axes:
                raise StateError("Raw valve movement/G28 blocked; use Valves API")
        readonly = token in {"M105", "M114", "M115", "M119", "M122", "M503"} or command.upper() == "M906"
        readonly = readonly or command.upper() == "M211"
        if not readonly and not confirmed:
            raise ValueError("confirm raw command: it may move hardware or change settings")
        return command, readonly

    async def raw(self, command: str, confirmed: bool = False) -> CommandResult:
        command, readonly = self.validate_raw(command, confirmed)
        if not readonly:
            self.invalidate_positions()
        return await self._q.send(command, timeout=self._timeouts.move)

    async def sensors(self, names: list[str] | None = None, require_available: bool = False) -> dict[str, Any]:
        from flowengine.hardware.sensors import parse_sensor_report

        selected = [s for s in self._map.analog_inputs if names is None or s.name in names]
        unknown = set(names or []) - {s.name for s in selected}
        if unknown:
            raise ValueError("Unknown configured sensors: " + ", ".join(sorted(unknown)))
        if require_available and not selected:
            raise ValueError("Configure analog_inputs before reading required sensors")
        result = await self._q.send("M105", timeout=self._timeouts.diagnostics)
        found = parse_sensor_report(result.raw)
        readings = []
        for sensor in selected:
            raw = found.get(sensor.source)
            value = raw * sensor.scale + sensor.offset if raw is not None else None
            available = value is not None and math.isfinite(value)
            reason = None if available else (
                "Source absent from M105; check firmware support and source name"
                if raw is None else "Non-finite calibrated value; check scale and offset"
            )
            readings.append({"name": sensor.name, "source": sensor.source,
                             "units": sensor.units, "quantity": sensor.quantity,
                             "raw_value": raw, "value": value if available else None,
                             "available": available, "reason": reason})
        missing = [r["name"] for r in readings if not r["available"]]
        if require_available and missing:
            raise ValueError("Required sensors unavailable: " + ", ".join(missing))
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "raw": result.raw,
            "reported_sources": found,
            "values": readings,
        }

    async def seek(
        self, axis: str, channel: str, delta: NumberValue, feedrate: NumberValue, zero: bool = False
    ) -> dict[str, Any]:
        delta, feedrate = float(delta), float(feedrate)
        self.valves.reject_axes([axis])
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
        if abs(delta) > cfg.mechanical_travel or feedrate > self._motion.max_feedrate(axis):
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
        if not math.isfinite(delta) or not 0 < abs(delta) <= cfg.mechanical_travel:
            raise ValueError("invalid bounded backoff")
        position = (await self.wait_idle())[axis]
        target = position + delta
        lo, hi = (0, cfg.mechanical_travel) if cfg.home_direction == "min" else (-cfg.mechanical_travel, 0)
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
        self.valves.reject_axes([axis])
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
            or abs(delta) + backoff > cfg.mechanical_travel
            or not 0 < backoff < abs(delta)
        ):
            raise ValueError(
                "round trip must go away from home, inside travel; positive backoff < displacement"
            )
        if not math.isfinite(tolerance_pulses) or tolerance_pulses < 0:
            raise ValueError("nonnegative finite tolerance required")
        await self.seek(axis, channel, sign * cfg.mechanical_travel, reference_feedrate, True)
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
                axis, channel, sign * min(cfg.mechanical_travel, backoff * 3), reference_feedrate, True
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
        valve = self.valves.for_axis(axis)
        if valve:
            return await self.valves.jog(valve.name, delta)
        self.valves.reject_axes([axis])
        p = self._motion.plan_relative(axis, delta, feedrate)
        await self._check_manual_endstops([axis])
        # Never trust the modal state left by another host/session.
        await self._q.send("G90", timeout=self._timeouts.diagnostics)
        await self._q.send(
            f"G1 {axis}{p.target:.4f} F{p.feedrate:.2f}",
            timeout=self._timeouts.move,
        )
        self._motion.accept_target(axis, p.target)
        self._expected[axis] = p.target

    async def move_to(self, axis: str, target: float, feedrate: float | None = None) -> None:
        valve = self.valves.for_axis(axis)
        if valve:
            for position, coordinate in (("A", valve.position_a), ("B", valve.position_b)):
                if abs(target - coordinate) < 1e-6:
                    return await self.set_valve(valve.name, position)
            raise StateError("Valve absolute motion requires calibrated Go A/B")
        self.valves.reject_axes([axis])
        p = self._motion.plan_absolute(axis, target, feedrate)
        await self._check_manual_endstops([axis])
        await self._q.send("G90", timeout=self._timeouts.diagnostics)
        await self._q.send(
            f"G1 {axis}{p.target:.4f} F{p.feedrate:.2f}",
            timeout=self._timeouts.move,
        )
        self._motion.accept_target(axis, p.target)
        self._expected[axis] = p.target

    async def _axis_snapshot(self, axis: str) -> tuple[float, bool, int | None]:
        result = await self._q.send("M114", timeout=self._timeouts.diagnostics)
        if axis not in (result.position or {}):
            raise ControllerError(f"{axis}: контроллер не сообщил позицию")
        counts: dict[str, int] = {}
        for line in result.raw:
            if "Count " not in line:
                continue
            counts.update({a: int(v) for a, v in re.findall(r"([A-Z]):(-?\d+)", line.split("Count ", 1)[1])})
        self._counts.update(counts)
        switches = await self.read_endstops()
        channel = f"{axis.lower()}_min"
        if channel not in switches:
            raise ControllerError(f"{axis}: вход {channel} не найден в M119")
        return result.position[axis], switches[channel], counts.get(axis)

    async def _parallel_move_segment(
        self, axis: str, target: float, feedrate: float
    ) -> bool:
        await self._q.send(
            f"G1 {axis}{target:.4f} F{feedrate:.2f}", timeout=self._timeouts.move
        )
        await self._q.send("M400", timeout=self._timeouts.move)
        switches = await self.read_endstops()
        channel = f"{axis.lower()}_min"
        if channel not in switches:
            raise ControllerError(f"{axis}: вход {channel} не найден в M119")
        return switches[channel]

    async def _home_progress(
        self,
        callback: Callable[[str], Awaitable[None]] | None,
        detail: str,
    ) -> None:
        if callback is not None:
            await callback(detail)

    async def cancel_home(self) -> bool:
        if not self._home_active:
            return False
        self._home_cancel.set()
        return True

    async def _home_parallel_axis(
        self,
        axis: str,
        progress: Callable[[str], Awaitable[None]] | None = None,
        calibration: AxisCalibration | None = None,
        *,
        verify_baseline_count: int | None = None,
        tolerance_steps: float | None = None,
        update_reference_baseline: bool = True,
    ) -> dict[str, Any]:
        """ищем микрик X/Y; длину хода задаёт тайм-аут, границу показывает сам микрик"""
        cfg = self._map.axis(axis)
        cal = calibration or cfg.axis_calibration
        if cal is None:
            raise ValueError(f"{axis}: сначала выполните физическую калибровку X/Y")
        if axis not in {"X", "Y"} or not cfg.endstop_channels_shared:
            raise ValueError(f"{axis}: parallel-endstop Home предназначен только для откалиброванных X/Y")

        position, pressed, _ = await self._axis_snapshot(axis)
        if pressed:
            raise ValueError(
                f"{axis}: общий концевик уже нажат, а его сторона после запуска неизвестна. "
                "Сначала освободите его через калибровочный ручной проход, затем повторите Home."
            )

        approach = cal.home_approach_direction
        away = -approach
        seek = min(cal.seek_feedrate, self._motion.max_feedrate(axis))
        latch = min(cal.latch_feedrate, seek)
        expected_seek_s = 60.0 * cal.travel / max(seek, 1e-9)
        watchdog_s = max(self._timeouts.homing, min(300.0, expected_seek_s * 2.0 + 20.0))
        channel = f"{axis.lower()}_min"
        soft_endstops_disabled = False

        def cancelled() -> str | None:
            return "cancelled" if self._home_cancel.is_set() else None

        async def poll_progress(phase: str, elapsed: float, switch: bool) -> None:
            await self._home_progress(
                progress,
                f"Home {axis}: {phase} · {elapsed:.1f} с · концевик {'НАЖАТ' if switch else 'open'}",
            )

        async def fixed_move(direction: int, distance: float, feedrate: float) -> None:
            current, _, _ = await self._axis_snapshot(axis)
            target = current + direction * distance
            await self._q.send("G90", timeout=self._timeouts.diagnostics)
            await self._q.send(
                f"G1 {axis}{target:.4f} F{feedrate:.2f}", timeout=self._timeouts.move
            )
            await self._q.send("M400", timeout=self._timeouts.move)

        self._motion.invalidate([axis])
        await self.hold_references()
        try:
            await self._q.send("M211 S0", timeout=self._timeouts.diagnostics)
            soft_endstops_disabled = True

            await self._home_progress(progress, f"Home {axis}: быстрый поиск края «{cal.home_side}»")
            coarse = await seek_switch(
                self._q,
                axis=axis,
                channel=channel,
                direction=approach,
                feedrate=seek,
                target_pressed=True,
                timeout_s=watchdog_s,
                steps_per_unit=cfg.steps_per_unit,
                diagnostics_timeout=self._timeouts.diagnostics,
                move_timeout=self._timeouts.move,
                stop_reason=cancelled,
                on_poll=lambda e, sw: poll_progress("быстрый поиск", e, sw),
            )
            if not coarse.reached:
                if coarse.stop_reason == "cancelled":
                    raise StateError(f"{axis}: Home остановлен оператором")
                raise ValueError(f"{axis}: Home watchdog истёк — физический концевик не найден")

            await self._home_progress(progress, f"Home {axis}: освобождаем концевик")
            released = await seek_switch(
                self._q,
                axis=axis,
                channel=channel,
                direction=away,
                feedrate=latch,
                target_pressed=False,
                timeout_s=max(5.0, 60.0 * (cal.backoff_mm + 2.0) / max(latch, 1e-9)),
                steps_per_unit=cfg.steps_per_unit,
                diagnostics_timeout=self._timeouts.diagnostics,
                move_timeout=self._timeouts.move,
                stop_reason=cancelled,
                on_poll=lambda e, sw: poll_progress("освобождение", e, sw),
            )
            if not released.reached:
                if released.stop_reason == "cancelled":
                    raise StateError(f"{axis}: Home остановлен оператором")
                raise ControllerError(f"{axis}: концевик не освободился при отходе")

            await self._home_progress(progress, f"Home {axis}: отход {cal.backoff_mm:.2f} мм")
            await fixed_move(away, cal.backoff_mm, min(seek, 600.0))
            if (await self.read_endstops()).get(channel) is not False:
                raise ControllerError(f"{axis}: концевик повторно сработал во время Home backoff")

            await self._home_progress(progress, f"Home {axis}: точный медленный подход")
            precise = await seek_switch(
                self._q,
                axis=axis,
                channel=channel,
                direction=approach,
                feedrate=latch,
                target_pressed=True,
                timeout_s=max(10.0, 60.0 * (cal.backoff_mm + 3.0) / max(latch, 1e-9)),
                steps_per_unit=cfg.steps_per_unit,
                diagnostics_timeout=self._timeouts.diagnostics,
                move_timeout=self._timeouts.move,
                stop_reason=cancelled,
                on_poll=lambda e, sw: poll_progress("точный подход", e, sw),
            )
            if not precise.reached:
                if precise.stop_reason == "cancelled":
                    raise StateError(f"{axis}: Home остановлен оператором")
                raise ControllerError(f"{axis}: точный Home-контакт не найден")

            _, confirmed_pressed, latch_count = await self._axis_snapshot(axis)
            if not confirmed_pressed:
                raise ControllerError(f"{axis}: точный Home-контакт не подтвердился")

            check_result: dict[str, Any] | None = None
            if verify_baseline_count is not None:
                if latch_count is None:
                    raise ControllerError(
                        f"{axis}: Marlin Count недоступен — проверка возврата по шагам невозможна"
                    )
                if tolerance_steps is None or not math.isfinite(tolerance_steps) or tolerance_steps < 0:
                    raise ValueError("tolerance_steps must be a nonnegative finite number")
                delta_steps = latch_count - verify_baseline_count
                suspected = abs(delta_steps) > tolerance_steps
                check_result = {
                    "baseline_count": verify_baseline_count,
                    "measured_count": latch_count,
                    "delta_steps": delta_steps,
                    "abs_delta_steps": abs(delta_steps),
                    "tolerance_steps": tolerance_steps,
                    "status": "STEP_LOSS" if suspected else "OK",
                    "suspected_position_error": suspected,
                    "warning": (
                        "Отклонение возврата к физическому концевику не доказывает пропуск шагов: "
                        "в него также входят повторяемость концевика, люфт и механическая упругость."
                    ),
                }
                await self._home_progress(
                    progress,
                    f"Check {axis}: Δ={delta_steps:+d} шагов · допуск ±{tolerance_steps:g} · "
                    + ("STEP_LOSS" if suspected else "OK"),
                )
                if suspected:
                    # после неудачной проверки отходим от микрика, но reference не восстанавливаем
                    await fixed_move(away, cal.backoff_mm, latch)
                    _, final_pressed, _ = await self._axis_snapshot(axis)
                    if final_pressed:
                        raise ControllerError(f"{axis}: концевик остался нажат после проверки шагов")
                    self._motion.invalidate([axis])
                    self._reference_counts.pop(axis, None)
                    self._reference_sides.pop(axis, None)
                    return {
                        "axis": axis,
                        "home_side": cal.home_side,
                        "reference_coordinate": cal.home_coordinate,
                        "position": None,
                        "reference_count": None,
                        "seek_feedrate": seek,
                        "latch_feedrate": latch,
                        "backoff_mm": cal.backoff_mm,
                        "continuous_seek": self._q.transport_name != "mock",
                        "step_check": check_result,
                        "referenced": False,
                    }

            reference_coordinate = cal.home_coordinate
            await self._home_progress(progress, f"Home {axis}: фиксируем reference {reference_coordinate:.3f}")
            await self._q.send(
                f"G92 {axis}{reference_coordinate:.4f}", timeout=self._timeouts.diagnostics
            )

            # от точного касания отходим только на заданный backoff
            await self._home_progress(progress, f"Home {axis}: финальный отход {cal.backoff_mm:.2f} мм")
            if self._home_cancel.is_set():
                raise StateError(f"{axis}: Home остановлен оператором")
            await fixed_move(away, cal.backoff_mm, latch)

            final_position, final_pressed, _ = await self._axis_snapshot(axis)
            if final_pressed:
                raise ControllerError(f"{axis}: концевик остался нажат после Home")
            self._motion.mark_referenced(axis, final_position)
            if update_reference_baseline:
                if latch_count is not None:
                    self._reference_counts[axis] = latch_count
                else:
                    self._reference_counts.pop(axis, None)
            self._reference_sides[axis] = cal.home_side
            await self._home_progress(progress, f"Home {axis}: готово · REFERENCED")
            result = {
                "axis": axis,
                "home_side": cal.home_side,
                "reference_coordinate": reference_coordinate,
                "position": final_position,
                "reference_count": self._reference_counts.get(axis),
                "seek_feedrate": seek,
                "latch_feedrate": latch,
                "backoff_mm": cal.backoff_mm,
                "continuous_seek": self._q.transport_name != "mock",
                "referenced": True,
            }
            if check_result is not None:
                result["step_check"] = check_result
            return result
        finally:
            if soft_endstops_disabled:
                await self._q.send("M211 S1", timeout=self._timeouts.diagnostics)

    async def _home_syringes(self, axes, progress=None):
        epoch = self.reference_epoch
        calibrations = {a: self._map.axis(a).axis_calibration for a in axes}
        if any(cal is None for cal in calibrations.values()):
            raise ValueError("Сначала сохраните калибровку X/Y")
        if any(len(set(axes) & set(group.axes)) > 1 for group in self._map.endstop_inputs):
            raise ValueError("Совместный HOME требует раздельные входы X/Y")
        directions = {a: calibrations[a].home_approach_direction for a in axes}
        seek = {a: min(calibrations[a].seek_feedrate, self._motion.max_feedrate(a)) for a in axes}
        latch = {a: min(calibrations[a].latch_feedrate, seek[a]) for a in axes}
        backoff = {a: calibrations[a].backoff_mm for a in axes}
        timeouts = {a: max(self._timeouts.homing, min(300, 120 * calibrations[a].travel / seek[a] + 20)) for a in axes}
        cancelled = self._home_cancel.is_set
        await self.hold_references()
        await self._q.send("M211 S0", timeout=self._timeouts.diagnostics)
        try:
            edges = await find_group_edges(self, axes, directions, seek, latch, backoff, timeouts,
                                          cancelled=cancelled, progress=progress)
            await self._home_progress(progress, "X/Y одновременно: финальный отход от микриков")
            await move_group_away(self, axes, {a: -directions[a] for a in axes}, backoff, latch,
                                  cancelled=cancelled)
            await self.wait_idle()
            details = {}
            for axis in axes:
                cal = calibrations[axis]
                home_count = edges[axis]["step_count"]
                coordinate = cal.home_coordinate + (self._counts[axis] - home_count) / self._map.axis(axis).steps_per_unit
                await self._q.send(f"G92 {axis}{coordinate:.6f}", timeout=self._timeouts.diagnostics)
                details[axis] = {"axis": axis, "home_side": cal.home_side,
                                 "reference_coordinate": cal.home_coordinate, "position": coordinate,
                                 "reference_count": home_count, "backoff_mm": backoff[axis],
                                 "referenced": True, "simultaneous": True, "endstop": "OPEN"}
            if cancelled() or epoch != self.reference_epoch:
                raise StateError("HOME X/Y остановлен оператором")
            for axis in axes:
                self._motion.mark_referenced(axis, details[axis]["position"])
                self._reference_counts[axis] = edges[axis]["step_count"]
                self._reference_sides[axis] = calibrations[axis].home_side
            return details
        except BaseException:
            self._motion.invalidate(axes)
            raise
        finally:
            await self._q.send("M211 S1", timeout=self._timeouts.diagnostics)

    async def home(
        self,
        axes: list[str] | None = None,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        epoch = self.reference_epoch
        self.manual_session = None
        self._expected.clear()
        targets = axes if axes is not None else [a.marlin_axis for a in self._map.axes if a.kind != "peristaltic_pump"]
        if not targets:
            raise ValueError("select at least one axis to home")
        if any(self.valves.for_axis(a) for a in targets):
            if self.valves.lock.locked():
                raise StateError("Valve motion active")
            if await self.valves.read():
                raise StateError("Shared valve endstop TRIGGERED / SOURCE UNKNOWN; service release required")
        for a in targets:
            cfg = self._map.axis(a)
            if a in {"X", "Y"} and cfg.kind == "syringe_pump" and cfg.endstop_channels_shared and cfg.axis_calibration is None:
                raise ValueError(f"{a}: сначала сохраните калибровку двух концевиков; G28 запрещён")
            if cfg.kind == "peristaltic_pump":
                raise ValueError(f"{a}: для вращающегося насоса подтвердите исходную координату в Наладке")
            if cfg.homing_strategy != "endstop":
                raise ValueError("sensorless/crash homing is not implemented")
            if not self.valves.for_axis(a) and not (
                a in {"X", "Y"} and cfg.axis_calibration is not None and cfg.endstop_channels_shared
            ) and any(a in group.axes and len(group.axes) > 1 for group in self._map.endstop_inputs):
                raise ValueError("shared-input home requires a verified sequential seek procedure, not G28")

        simultaneous = [a for a in targets if a in {"X", "Y"}
                        and self._map.axis(a).axis_calibration is not None
                        and self._map.axis(a).endstop_channels_shared]
        if len(simultaneous) > 1 and any(
            len(set(simultaneous) & set(group.axes)) > 1 for group in self._map.endstop_inputs
        ):
            raise ValueError("Совместный HOME требует раздельные входы X/Y")
        if simultaneous:
            switches = await self.read_endstops()
            for axis in simultaneous:
                if switches.get(f"{axis.lower()}_min") is True:
                    raise ValueError(
                        f"{axis}: общий концевик уже нажат, а его сторона после запуска неизвестна. "
                        "Сначала освободите его через калибровочный ручной проход, затем повторите Home."
                    )

        await self.hold_references()
        self._motion.invalidate(targets)
        for a in targets:
            self._reference_counts.pop(a, None)
            self._reference_sides.pop(a, None)

        self._home_cancel.clear()
        self._home_active = True
        details: dict[str, Any] = {}
        try:
            if len(simultaneous) > 1:
                details.update(await self._home_syringes(simultaneous, progress))
            for axis in targets:
                if self._home_cancel.is_set():
                    raise StateError("Home остановлен оператором")
                if axis in details:
                    continue
                cfg = self._map.axis(axis)
                valve = self.valves.for_axis(axis)
                if valve:
                    await self._home_progress(progress, f"Home {axis}: поиск нуля крана {valve.name}")
                    details[axis] = await self.home_valve(valve.name)
                elif cfg.kind == "valve":
                    raise StateError("Valve axis has no ValveConfig; G28 blocked")
                elif cfg.axis_calibration is not None and axis in {"X", "Y"} and cfg.endstop_channels_shared:
                    details[axis] = await self._home_parallel_axis(axis, progress)
                else:
                    if any(axis in g.axes and len(g.axes) > 1 for g in self._map.endstop_inputs):
                        raise ValueError(
                            "shared-input home requires a verified sequential seek procedure, not G28"
                        )
                    await self._home_progress(progress, f"Home {axis}: firmware G28")
                    homed = await self._homing.home(self._q, [axis])
                    self._motion.mark_homed(homed)
                    details[axis] = {"axis": axis, "strategy": "g28", "position": 0.0}

                if epoch != self.reference_epoch:
                    self._motion.invalidate(targets)
                    raise ControllerError("Home прерван; привязка потеряна")
            return details
        except StateError:
            await self.motors(False, targets)
            raise
        except BaseException:
            await self.abort()
            raise
        finally:
            self._home_active = False
            self._home_cancel.clear()


    async def check_reference(
        self,
        axes: list[str],
        *,
        tolerance_steps: float,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """сравниваем Count при касании HOME; разница бывает из-за люфта, микрика или пропуска шагов"""
        if not axes:
            raise ValueError("select at least one axis to check")
        if not math.isfinite(tolerance_steps) or tolerance_steps < 0:
            raise ValueError("tolerance_steps must be a nonnegative finite number")

        epoch = self.reference_epoch
        baselines: dict[str, int] = {}
        for axis in axes:
            cfg = self._map.axis(axis)
            if axis not in {"X", "Y"} or cfg.axis_calibration is None or not cfg.endstop_channels_shared:
                raise ValueError(f"{axis}: проверка шагов поддерживается только для откалиброванных X/Y")
            if not self._motion.homed.get(axis, False):
                raise ValueError(f"{axis}: сначала выполните Home — reference отсутствует")
            baseline = self._reference_counts.get(axis)
            if baseline is None:
                raise ValueError(
                    f"{axis}: эталонный Marlin Count отсутствует. Повторите Home и затем проверку шагов."
                )
            baselines[axis] = baseline

        self.manual_session = None
        self._expected.clear()
        self._home_cancel.clear()
        self._home_active = True
        details: dict[str, Any] = {}
        try:
            for axis in axes:
                if self._home_cancel.is_set():
                    raise StateError("Проверка шагов остановлена оператором")
                details[axis] = await self._home_parallel_axis(
                    axis,
                    progress,
                    verify_baseline_count=baselines[axis],
                    tolerance_steps=tolerance_steps,
                    update_reference_baseline=False,
                )
                if epoch != self.reference_epoch:
                    self._motion.invalidate(axes)
                    raise ControllerError("Проверка шагов прервана; привязка потеряна")
            return details
        finally:
            self._home_active = False
            self._home_cancel.clear()

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
        expected, self._expected = self._expected, {}
        mismatched = [a for a, target in expected.items()
                      if a not in (result.position or {})
                      or not math.isfinite(result.position[a])
                      or abs(result.position[a] - target) > 0.02]
        if mismatched:
            self.manual_session = None
            self._motion.invalidate(list(expected))
            raise ControllerError(
                "Контроллер не подтвердил целевую координату: "
                + "; ".join(f"{a}: задано {expected[a]:.3f}, ответ {(result.position or {}).get(a)}" for a in mismatched)
                + ". Проверьте ограничения прошивки; движение не считается выполненным."
            )
        if expected and not self.manual_session:
            await self._check_manual_endstops(list(expected))
        return self._motion.positions

    async def read_endstops(self) -> dict[str, bool]:
        result = await self._q.send("M119", timeout=self._timeouts.diagnostics)
        return result.endstops or {}

    async def read_diagnostic(self, command: str) -> CommandResult:
        """Run an allow-listed read-only Marlin diagnostic command."""
        if command not in {"M114", "M119", "M503", "M122", "M105", "M211"}:
            raise ValueError(f"unsafe diagnostic command: {command}")
        result = await self._q.send(command, timeout=self._timeouts.diagnostics)
        if result.position:
            self._motion.update_position(result.position)
        return result

    async def read_firmware(self) -> tuple[str, frozenset[str]]:
        result = await self._q.send("M115", timeout=self._timeouts.diagnostics)
        return (result.firmware_raw or "", result.firmware_caps or frozenset())
