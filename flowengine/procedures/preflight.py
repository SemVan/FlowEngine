"""Read-only static execution checks. Never substitutes for bench verification."""

from __future__ import annotations

from flowengine.procedures.loader import lint
from flowengine.hardware.coordinated import timed_pump_moves
from flowengine.schemas import DeviceMap, Procedure, RuntimeParams
from flowengine.schemas.procedure import (
    HomeStep,
    HomeValveStep,
    MotorsStep,
    MoveMultiStep,
    MoveStep,
    MoveForStep,
    MoveMultiForStep,
    PumpMultiStep,
    PumpStep,
    ReferenceTestStep,
    RequireSetupStep,
    RequireReferenceStep,
    SeekStep,
    SetParamStep,
    SetValveStep,
    ValveCalibrationStep,
    WaitPressureStep,
)


def execution_issues(procedure: Procedure, device: DeviceMap, runtime: RuntimeParams) -> list[str]:
    issues = lint(procedure, device)
    known = {a.marlin_axis for a in device.axes}
    for index, step in enumerate(procedure.steps, 1):
        prefix = f"step {index}: "
        if isinstance(step, RequireSetupStep):
            issues.append(prefix + step.message)
        elif isinstance(step, (SetParamStep, WaitPressureStep)):
            issues.append(prefix + f"{step.op} is not implemented")
        elif isinstance(step, (HomeValveStep, SetValveStep)):
            valve = next((v for v in device.valves if v.name == step.name), None)
            if not device.shared_valve_endstop.channel:
                issues.append(prefix + "shared_valve_endstop channel is not configured")
            if isinstance(step, SetValveStep) and valve and not valve.calibrated:
                issues.append(prefix + "valve A/B commissioning required")
            if isinstance(step, HomeValveStep) and valve and valve.direction_toward_home is None:
                issues.append(prefix + "valve HOME direction is not configured in the profile")
        elif isinstance(step, RequireReferenceStep):
            if not set(step.axes) <= known:
                issues.append(prefix + "unknown reference axis")
        elif isinstance(step, MoveMultiForStep):
            try:
                timed_pump_moves(device, step.axes, step.seconds, runtime.motion.feedrate_cap)
            except ValueError as exc:
                issues.append(prefix + str(exc))
        elif isinstance(step, (MoveStep, MoveForStep)):
            if any(v.axis == step.axis for v in device.valves) and (
                isinstance(step, MoveForStep) or step.duration_s is not None
            ):
                issues.append(prefix + "Для крана выберите действие «Положение крана»")
            if isinstance(step, MoveForStep) and step.axis in known:
                cfg = device.axis(step.axis)
                speed = float(step.feedrate)
                if step.speed_units == "axis/s":
                    speed *= 60
                elif step.speed_units == "steps/s":
                    speed *= 60 / cfg.steps_per_unit
                cap = min(cfg.feedrate_max, runtime.motion.feedrate_cap)
                if speed > cap + 1e-9:
                    issues.append(prefix + f"{step.axis}: скорость {speed:g} выше предела {cap:g} ед/мин")
        elif isinstance(step, PumpStep):
            pump = next((p for p in device.pumps if p.name == step.name), None)
            if pump is None or not pump.calibrated:
                issues.append(prefix + "pump is unknown or uncalibrated")
        elif isinstance(step, PumpMultiStep):
            pump_axes = []
            for name in step.pumps:
                pump = next((p for p in device.pumps if p.name == name), None)
                if pump is None or not pump.calibrated:
                    issues.append(prefix + f"pump {name} is unknown or uncalibrated")
                else:
                    pump_axes.append(pump.axis)
                    speed = float(step.pumps[name].flow_ul_min) / pump.volume_per_unit_ul
                    cap = min(device.axis(pump.axis).feedrate_max, runtime.motion.feedrate_cap)
                    if speed > cap:
                        issues.append(prefix + f"{name}: расход требует скорости {speed:g} выше предела {cap:g} ед/мин")
            if len(pump_axes) != len(set(pump_axes)):
                issues.append(prefix + "два насоса используют один привод")
        elif isinstance(step, (SeekStep, ValveCalibrationStep, ReferenceTestStep)):
            if any(v.axis == step.axis for v in device.valves):
                issues.append(prefix + "single-switch valves require Valves commissioning; two-edge/probe search blocked")
            if step.axis not in known:
                issues.append(prefix + "unknown probe axis")
            if (
                not runtime.firmware.probe_target_verified
                or step.axis not in runtime.firmware.verified_probe_axes
                or step.channel != runtime.firmware.probe_channel
            ):
                issues.append(
                    prefix + "firmware-stopped probe search is not bench-verified for axis/channel"
                )
        elif isinstance(step, HomeStep):
            axes = step.axes if step.axes is not None else [a.marlin_axis for a in device.axes if a.kind != "peristaltic_pump"]
            for axis in axes:
                if any(v.axis == axis for v in device.valves):
                    if not device.shared_valve_endstop.channel:
                        issues.append(prefix + "shared_valve_endstop channel is not configured")
                    valve = next(v for v in device.valves if v.axis == axis)
                    if valve.direction_toward_home is None:
                        issues.append(prefix + "valve HOME direction is not configured in the profile")
                    continue
                if axis in known:
                    cfg = device.axis(axis)
                    if axis in {"X", "Y"} and cfg.kind == "syringe_pump" and cfg.endstop_channels_shared:
                        if cfg.axis_calibration is None:
                            issues.append(prefix + f"{axis}: калибровка двух концевиков не сохранена; G28 запрещён")
                        continue
                    if cfg.kind == "peristaltic_pump":
                        issues.append(prefix + f"{axis}: подтвердите исходную координату вращающегося насоса в Наладке")
                        continue
                if any(
                    axis in group.axes and len(group.axes) > 1 for group in device.endstop_inputs
                ):
                    issues.append(
                        prefix
                        + f"{axis}: shared-input G28 is blocked; use sequential verified seek"
                    )
                if axis in known and device.axis(axis).homing_strategy != "endstop":
                    issues.append(prefix + f"{axis}: homing strategy is not implemented")
        elif isinstance(step, MotorsStep):
            if step.axes is not None and not set(step.axes) <= known:
                issues.append(prefix + "unknown motor axis")
        elif isinstance(step, MoveMultiStep):
            if any(v.axis in step.axes for v in device.valves):
                issues.append(prefix + "Краны переключаются действием «Положение крана»")
            if step.axis_feedrates is not None:
                for axis, speed in step.axis_feedrates.items():
                    if axis in known:
                        cap = min(device.axis(axis).feedrate_max, runtime.motion.feedrate_cap)
                        if float(speed) > cap:
                            issues.append(prefix + f"{axis}: скорость {float(speed):g} выше предела {cap:g} ед/мин")
            if set(step.axes) <= known and len({device.axis(a).units for a in step.axes}) > 1:
                issues.append(prefix + "mixed axis units cannot be coordinated")
            if any(len(set(step.axes) & set(g.axes)) > 1 for g in device.endstop_inputs):
                issues.append(prefix + "coordinated axes share an endstop input")
    return issues


def reference_issues(procedure: Procedure, device: DeviceMap, homed: dict[str, bool]) -> list[str]:
    referenced = {axis for axis, value in homed.items() if value}
    issues = []
    for index, step in enumerate(procedure.steps, 1):
        required = []
        if isinstance(step, HomeStep):
            referenced.update(step.axes if step.axes is not None else [
                axis.marlin_axis for axis in device.axes if axis.kind != "peristaltic_pump"
            ])
        elif isinstance(step, HomeValveStep):
            valve = next((v for v in device.valves if v.name == step.name), None)
            if valve:
                referenced.add(valve.axis)
        elif isinstance(step, MotorsStep) and not step.enabled:
            referenced.difference_update(step.axes if step.axes is not None else referenced.copy())
        elif isinstance(step, (MoveStep, MoveForStep)):
            required = [step.axis]
        elif isinstance(step, (MoveMultiStep, MoveMultiForStep)):
            required = list(step.axes)
        elif isinstance(step, SetValveStep):
            required = [v.axis for v in device.valves if v.name == step.name]
        elif isinstance(step, (PumpStep, PumpMultiStep)):
            names = [step.name] if isinstance(step, PumpStep) else step.pumps
            required = [p.axis for p in device.pumps if p.name in names]
        elif isinstance(step, RequireReferenceStep):
            required = step.axes
        elif isinstance(step, SeekStep) and step.zero:
            referenced.add(step.axis)
        missing = sorted(set(required) - referenced)
        if missing:
            issues.append(f"Шаг {index}: нужен HOME для {', '.join(missing)} до этого шага")
    return issues


def session_warnings(procedure: Procedure) -> list[str]:
    warnings = []
    if any(isinstance(step, MotorsStep) and not step.enabled for step in procedure.steps):
        warnings.append("В процедуре есть снятие удержания: эти оси потеряют нули. Для серии запусков уберите этот шаг и отпустите моторы после всей работы отдельной кнопкой.")
    if any(isinstance(step, (HomeStep, HomeValveStep)) and not step.only_if_needed for step in procedure.steps):
        warnings.append("Есть повторный HOME. Чтобы использовать уже найденные нули, выберите в шаге «Только если привязка потеряна».")
    return warnings
