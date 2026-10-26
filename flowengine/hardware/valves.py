"""краны: общий вход концевиков, двигаем только один за раз"""
from __future__ import annotations

import asyncio
import math
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass

from flowengine.errors import ControllerError, StateError
from flowengine.hardware.endstop_seek import _normal_quickstop, seek_switch


@dataclass
class ValveState:
    state: str = "UNCONFIGURED"
    current: str = "UNKNOWN"
    error: str | None = None
    commissioning: bool = False
    saved_a: bool = False
    saved_b: bool = False
    service_release: str = "NOT_REQUESTED"


class ValveSubsystem:
    def __init__(self, sender):
        self.sender = sender
        self.lock = asyncio.Lock()
        self.active_valve: str | None = None
        self.active_service: str | None = None
        self.source = "UNKNOWN"
        self.shared: bool | None = None
        self.phase = "idle"
        self.states = {v.name: ValveState() for v in sender._map.valves}

    def config(self, name):
        cfg = next((v for v in self.sender._map.valves if v.name in {name, name + "_valve"}), None)
        if cfg is None:
            raise ValueError(f"Unknown valve {name!r}")
        return cfg

    def for_axis(self, axis):
        return next((v for v in self.sender._map.valves if v.axis == axis), None)

    def reject_axes(self, axes):
        if any(self.for_axis(a) or self.sender._map.axis(a).kind == "valve" for a in axes):
            raise StateError("Valve motors require the Valves API; raw/combined motion is blocked")

    def direction(self, cfg):
        return cfg.direction_toward_home or (-1 if self.sender._map.axis(cfg.axis).home_direction == "min" else 1)

    def invalidate(self, axes=None):
        for cfg in self.sender._map.valves:
            if axes is None or cfg.axis in axes:
                st = self.states[cfg.name]
                st.current = "UNKNOWN"
                st.commissioning = False
                st.saved_a = st.saved_b = False
                if st.state != "FAULT":
                    st.state = "CALIBRATED_UNREFERENCED" if cfg.calibrated else "UNCONFIGURED"
                self.sender._motion.invalidate([cfg.axis])
                self.sender._reference_counts.pop(cfg.axis, None)
                self.sender._reference_sides.pop(cfg.axis, None)
        self.source = "UNKNOWN"

    async def read(self):
        channel = self.sender._map.shared_valve_endstop.channel
        if not channel:
            self.shared = None
            raise StateError("Configure shared_valve_endstop channel from M119 (Z input)")
        switches = await self.sender.read_endstops()
        if channel not in switches:
            self.shared = None
            self.source = "UNKNOWN"
            raise ControllerError(f"Shared valve input {channel} missing from M119")
        self.shared = switches[channel]
        if not self.shared or not self.active_valve:
            self.source = "UNKNOWN"
        return self.shared

    def snapshot(self):
        valves = []
        for cfg in self.sender._map.valves:
            st = self.states[cfg.name]
            referenced = self.sender._motion.homed.get(cfg.axis, False)
            state = st.state
            if state not in {"FAULT", "MOVING", "HOMING"}:
                state = "REFERENCED" if referenced else "CALIBRATED_UNREFERENCED" if cfg.calibrated else "UNCONFIGURED"
            valves.append({"name": cfg.name, "axis": cfg.axis, "config": cfg.model_dump(),
                           "state": state, "calibrated": cfg.calibrated, "referenced": referenced,
                           "current": st.current if referenced else "UNKNOWN",
                           "coordinate": self.sender._motion.positions[cfg.axis] if referenced else None,
                           "direction_toward_home": self.direction(cfg),
                           "direction_away_from_home": -self.direction(cfg),
                           "commissioning": st.commissioning, "saved_a": st.saved_a,
                           "saved_b": st.saved_b, "error": st.error,
                           "service_release": st.service_release})
        return {"valves": valves, "shared_valve_endstop": {
            "channel": self.sender._map.shared_valve_endstop.channel,
            "state": "UNKNOWN" if self.shared is None else "TRIGGERED" if self.shared else "OPEN",
            "source": self.source if self.shared else "UNKNOWN"},
            "active_valve": self.active_valve, "busy": self.lock.locked(), "phase": self.phase}

    @asynccontextmanager
    async def operation(self, name, state, *, allow_pressed=False, release_after=False):
        cfg = self.config(name)
        if self.lock.locked():
            raise StateError("Another valve operation is active")
        async with self.lock:
            # если вход уже нажат, ещё неизвестно, какой кран держит сигнал
            if await self.read() and not allow_pressed:
                raise StateError("Shared valve endstop TRIGGERED / SOURCE UNKNOWN; use service release")
            self.active_valve = cfg.name
            st = self.states[cfg.name]
            st.state, st.error = state, None
            if release_after:
                self.active_service = cfg.name
                st.service_release = "PENDING"
            epoch = self.sender.reference_epoch
            soft_state = None
            try:
                report = await self.sender._q.send("M211", timeout=self.sender._timeouts.diagnostics)
                text = " ".join(report.raw)
                setting = re.search(r"M211\s+S([01])", text, re.I)
                enabled = re.search(r"Soft endstops:\s*(ON|OFF)", text, re.I)
                if not setting and not enabled:
                    raise ControllerError("Cannot read M211 state before valve motion")
                soft_state = int(setting.group(1)) if setting else int(enabled.group(1).upper() == "ON")
                await self.sender._q.send("M211 S0", timeout=self.sender._timeouts.diagnostics)
                yield cfg, st
                if epoch != self.sender.reference_epoch:
                    raise StateError("Valve operation interrupted; Home required")
                if release_after:
                    await self.sender._q.send(f"M18 {cfg.axis}", timeout=self.sender._timeouts.diagnostics)
                    st.service_release = "ACKNOWLEDGED"
                if st.state in {"MOVING", "HOMING"}:
                    st.state = "REFERENCED" if self.sender._motion.homed[cfg.axis] else "CALIBRATED_UNREFERENCED" if cfg.calibrated else "UNCONFIGURED"
                await self.sender._q.send(f"M211 S{soft_state}", timeout=self.sender._timeouts.diagnostics)
            except BaseException as exc:
                cfg = self.config(self.active_valve or cfg.name)
                st = self.states[cfg.name]
                st.state, st.current, st.error = "FAULT", "UNKNOWN", str(exc)
                st.commissioning = False
                self.sender._motion.invalidate([cfg.axis])
                try:
                    await self.quickstop(cfg)
                    if soft_state is not None:
                        await self.sender._q.send(f"M211 S{soft_state}", timeout=self.sender._timeouts.diagnostics)
                except Exception:
                    pass
                await self.sender.abort()
                raise
            finally:
                self.active_valve, self.phase = None, "idle"
                self.active_service = None
                self.source = "UNKNOWN"

    async def quickstop(self, cfg):
        await _normal_quickstop(self.sender._q, axis=cfg.axis,
            steps_per_unit=self.sender._map.axis(cfg.axis).steps_per_unit,
            diagnostics_timeout=self.sender._timeouts.diagnostics,
            move_timeout=self.sender._timeouts.move)

    async def seek(self, cfg, direction, pressed, speed, phase):
        self.phase = phase
        if await self.read() == pressed:
            raise ControllerError(f"{phase}: expected initial {'OPEN' if pressed else 'TRIGGERED'}")

        async def poll(_elapsed, value):
            self.shared = value
            self.source = cfg.name if value else "UNKNOWN"

        outcome = await seek_switch(self.sender._q, axis=cfg.axis,
            channel=self.sender._map.shared_valve_endstop.channel, direction=direction,
            feedrate=speed, target_pressed=pressed,
            timeout_s=cfg.home_timeout_s,
            steps_per_unit=self.sender._map.axis(cfg.axis).steps_per_unit,
            diagnostics_timeout=self.sender._timeouts.diagnostics,
            move_timeout=self.sender._timeouts.move, on_poll=poll,
            stop_reason=lambda: "cancelled" if self.sender._home_cancel.is_set() else None)
        if not outcome.reached:
            raise ControllerError(f"Valve {phase} stopped: {outcome.stop_reason}")
        if self.sender._q.transport_name == "mock":
            await self.quickstop(cfg)
        if await self.read() != pressed:
            raise ControllerError(f"Valve {phase}: switch state was not confirmed")

    async def position(self, cfg):
        positions = await self.sender.wait_idle()
        return positions[cfg.axis]

    async def finite_move(self, cfg, target, speed, *, release=False):
        """опрашиваем вход и M114 R на ходу; M400 отправляем только после достижения цели"""
        start = await self.position(cfg)
        if not all(math.isfinite(x) for x in (target, speed)) or speed <= 0:
            raise ValueError("finite target and positive speed required")
        speed = min(speed, self.sender._motion.max_feedrate(cfg.axis))
        initially_pressed = await self.read()
        if initially_pressed and not release:
            raise ControllerError("Shared valve endstop unexpectedly TRIGGERED")
        await self.sender._q.send("G90", timeout=self.sender._timeouts.diagnostics)
        await self.sender._q.send(f"G1 {cfg.axis}{target:.6f} F{speed:.3f}", timeout=self.sender._timeouts.move)
        deadline = asyncio.get_running_loop().time() + abs(target - start) * 60 / speed + 3
        while True:
            pressed = await self.read()
            if release and not pressed:
                await self.quickstop(cfg)
                break
            if pressed and not release:
                self.source = cfg.name
                await self.quickstop(cfg)
                raise ControllerError("Unexpected shared valve endstop; REFERENCE_LOST / FAULT")
            report = await self.sender._q.send("M114 R", timeout=self.sender._timeouts.diagnostics)
            actual = (report.position or {}).get(cfg.axis)
            if actual is None:
                raise ControllerError("Real valve position unavailable from M114 R")
            tolerance = max(0.001, .5 / self.sender._map.axis(cfg.axis).steps_per_unit + .0001)
            if abs(actual - target) <= tolerance:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise ControllerError("Valve motion watchdog expired")
            await asyncio.sleep(0.04)
        actual = await self.position(cfg)
        if not release and await self.read():
            raise ControllerError("Unexpected endstop at move completion; REFERENCE_LOST")
        return actual

    async def _home(self, cfg, st):
        self.sender._motion.invalidate([cfg.axis])
        self.sender._reference_counts.pop(cfg.axis, None)
        st.current, st.commissioning = "UNKNOWN", False
        toward = self.direction(cfg)
        seek_speed = min(cfg.home_seek_speed, self.sender._motion.max_feedrate(cfg.axis))
        latch_speed = min(cfg.home_latch_speed, seek_speed)
        await self.seek(cfg, toward, True, seek_speed, "fast seek")
        await self.seek(cfg, -toward, False, seek_speed, "release")
        await self.finite_move(cfg, await self.position(cfg) - toward * cfg.backoff, latch_speed)
        await self.seek(cfg, toward, True, latch_speed, "slow latch")
        await self.position(cfg)
        home_count = self.sender._counts.get(cfg.axis)
        await self.sender._q.send(f"G92 {cfg.axis}0", timeout=self.sender._timeouts.diagnostics)
        self.phase = "pull-off release"
        await self.seek(cfg, -toward, False, seek_speed, "pull-off release")
        # после M410 возвращаем координату относительно HOME крана
        await self.position(cfg)
        if home_count is not None:
            released_count = self.sender._counts.get(cfg.axis)
            if released_count is None:
                raise ControllerError("Valve Count disappeared after latch")
            coordinate = (released_count - home_count) / self.sender._map.axis(cfg.axis).steps_per_unit
            await self.sender._q.send(f"G92 {cfg.axis}{coordinate:.6f}", timeout=self.sender._timeouts.diagnostics)
        position = await self.finite_move(cfg, await self.position(cfg) - toward * cfg.pull_off, latch_speed)
        if await self.read():
            raise ControllerError("Shared valve endstop did not return to OPEN")
        self.sender._motion.mark_referenced(cfg.axis, position)
        if home_count is not None:
            self.sender._reference_counts[cfg.axis] = home_count
        st.state = "REFERENCED"
        return {"name": cfg.name, "position": position, "shared_endstop": "OPEN"}

    async def home_valve(self, name):
        async with self.operation(name, "HOMING") as (cfg, st):
            return await self._home(cfg, st)

    async def test_home(self, name):
        """пробный Home: в конце снимаем удержание и сбрасываем reference"""
        cfg = self.config(name)
        if len(cfg.axis) != 1 or not "A" <= cfg.axis <= "Z":
            raise ValueError("Test Home release requires a single-letter motor axis")
        async with self.operation(name, "HOMING", release_after=True) as (cfg, st):
            try:
                result = await self._home(cfg, st)
            finally:
                self.invalidate([cfg.axis])
        return {**result, "referenced": False, "release": st.service_release}

    async def home_all(self):
        if not self.sender._map.valves:
            return []
        first = self.sender._map.valves[0].name
        async with self.operation(first, "HOMING"):
            results = []
            for cfg in self.sender._map.valves:
                self.active_valve = cfg.name
                st = self.states[cfg.name]
                st.state = "HOMING"
                try:
                    if await self.read():
                        raise ControllerError("Shared input must be OPEN between valves")
                    results.append(await self._home(cfg, st))
                except BaseException as exc:
                    st.state, st.error = "FAULT", str(exc)
                    self.sender._motion.invalidate([cfg.axis])
                    raise
            return results

    async def set_valve(self, name, position, feedrate=None):
        cfg = self.config(name)
        position = cfg.position_slot(position)
        speed = self.sender._map.axis(cfg.axis).feedrate_default if feedrate is None else feedrate
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("finite positive valve feedrate required")
        if not cfg.calibrated:
            raise StateError("Valve calibration A/B required")
        if not self.sender._motion.homed[cfg.axis]:
            raise StateError("Valve Home required before Go A/B")
        if await self.read():
            st = self.states[cfg.name]
            st.state, st.current, st.error = "FAULT", "UNKNOWN", "Unexpected shared endstop; REFERENCE_LOST"
            self.sender._motion.invalidate([cfg.axis])
            raise ControllerError(st.error)
        async with self.operation(name, "MOVING") as (cfg, st):
            await self.finite_move(cfg, cfg.position_a if position == "A" else cfg.position_b,
                                    speed)
            st.state, st.current = "REFERENCED", position

    async def calibrate(self, name):
        async with self.operation(name, "HOMING") as (cfg, st):
            await self._home(cfg, st)
            st.commissioning = True
            st.saved_a = st.saved_b = False

    async def move_referenced(self, name, delta, feedrate):
        cfg = self.config(name)
        if not self.sender._motion.homed[cfg.axis]:
            raise StateError("Valve Home required before positioning")
        if not math.isfinite(delta) or not 0 < abs(delta) <= 20:
            raise ValueError("Use a nonzero move of at most 20 axis units")
        if not math.isfinite(feedrate) or feedrate <= 0:
            raise ValueError("Positive finite feedrate required")
        async with self.operation(name, "MOVING") as (cfg, st):
            target = await self.position(cfg) + delta
            if target * self.direction(cfg) >= 0:
                raise ValueError("Working position must remain away from HOME")
            position = await self.finite_move(cfg, target, feedrate)
            st.current = "UNKNOWN"
        return {"ok": True, "axis": cfg.axis, "position": position, "referenced": True}

    async def service_move(self, name, delta, feedrate):
        cfg = self.config(name)
        if not math.isfinite(delta) or not 0 < abs(delta) <= 20:
            raise ValueError("Use a nonzero service move of at most 20 axis units")
        if not math.isfinite(feedrate) or not 0 < feedrate <= min(120, self.sender._motion.max_feedrate(cfg.axis)):
            raise ValueError("Service feedrate must be positive and at most 120 units/min")
        if len(cfg.axis) != 1:
            raise ValueError("Service release requires a single-letter motor axis")
        async with self.operation(name, "MOVING", allow_pressed=True, release_after=True) as (cfg, st):
            if self.shared and delta * self.direction(cfg) >= 0:
                raise StateError("Pressed shared input: service motion is permitted only away from HOME")
            self.invalidate()
            start = await self.position(cfg)
            await self.finite_move(cfg, start + delta, feedrate, release=bool(self.shared))
            st.current = "UNKNOWN"
        return {"ok": True, "device": name, "axis": cfg.axis, "delta": delta,
                "motor_released": st.service_release == "ACKNOWLEDGED", "position_trusted": False}

    def validate_jog(self, name, delta, *, service=False):
        cfg = self.config(name)
        st = self.states[cfg.name]
        if not math.isfinite(delta) or not 0 < abs(delta) <= 0.5:
            raise ValueError("Use a short nonzero jog of at most 0.5 axis units")
        if service and (len(cfg.axis) != 1 or not "A" <= cfg.axis <= "Z"):
            raise ValueError("Service release requires a single-letter motor axis; verify the firmware mapping")
        if not service and (not st.commissioning or not self.sender._motion.homed[cfg.axis]):
            raise StateError("Start valve calibration with Home first")
        if not service and delta * self.direction(cfg) >= 0:
            raise StateError("Commissioning jog is permitted only AWAY from HOME")
        return cfg

    async def jog(self, name, delta, *, service=False):
        self.validate_jog(name, delta, service=service)
        async with self.operation(name, "MOVING", allow_pressed=service, release_after=service) as (cfg, st):
            if service:
                self.invalidate()
            start = await self.position(cfg)
            await self.finite_move(cfg, start + delta, min(cfg.home_latch_speed, 30),
                                   release=service and bool(self.shared))
            st.current = "UNKNOWN"
            st.state = "REFERENCED" if not service else "CALIBRATED_UNREFERENCED" if cfg.calibrated else "UNCONFIGURED"

    async def capture(self, name, position):
        cfg = self.config(name)
        st = self.states[cfg.name]
        if not st.commissioning or not self.sender._motion.homed[cfg.axis]:
            raise StateError("Start valve commissioning with Home first")
        async with self.operation(name, "MOVING"):
            coordinate = await self.position(cfg)
            count = self.sender._counts.get(cfg.axis)
            origin = self.sender._reference_counts.get(cfg.axis)
            if count is not None and origin is not None:
                coordinate = (count - origin) / self.sender._map.axis(cfg.axis).steps_per_unit
            if coordinate * self.direction(cfg) >= 0:
                raise StateError("A/B must be away from HOME and the switch")
            return coordinate
