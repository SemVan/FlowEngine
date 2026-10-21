"""Read-only static execution checks. Never substitutes for bench verification."""

from __future__ import annotations

from flowengine.procedures.loader import lint
from flowengine.schemas import DeviceMap, Procedure, RuntimeParams
from flowengine.schemas.procedure import (
    HomeStep,
    HomeValveStep,
    MotorsStep,
    MoveMultiStep,
    PumpMultiStep,
    PumpStep,
    ReferenceTestStep,
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
        if isinstance(step, (SetParamStep, WaitPressureStep)):
            issues.append(prefix + f"{step.op} is not implemented")
        elif isinstance(step, (HomeValveStep, SetValveStep)):
            valve = next((v for v in device.valves if v.name == step.name), None)
            if not device.shared_valve_endstop.channel:
                issues.append(prefix + "shared_valve_endstop channel is not configured")
            if isinstance(step, SetValveStep) and valve and not valve.calibrated:
                issues.append(prefix + "valve A/B commissioning required")
        elif isinstance(step, PumpStep):
            pump = next((p for p in device.pumps if p.name == step.name), None)
            if pump is None or not pump.calibrated:
                issues.append(prefix + "pump is unknown or uncalibrated")
        elif isinstance(step, PumpMultiStep):
            for name in step.pumps:
                pump = next((p for p in device.pumps if p.name == name), None)
                if pump is None or not pump.calibrated:
                    issues.append(prefix + f"pump {name} is unknown or uncalibrated")
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
            axes = step.axes if step.axes is not None else list(known)
            for axis in axes:
                if any(v.axis == axis for v in device.valves):
                    if not device.shared_valve_endstop.channel:
                        issues.append(prefix + "shared_valve_endstop channel is not configured")
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
            if set(step.axes) <= known and len({device.axis(a).units for a in step.axes}) > 1:
                issues.append(prefix + "mixed axis units cannot be coordinated")
            if any(len(set(step.axes) & set(g.axes)) > 1 for g in device.endstop_inputs):
                issues.append(prefix + "coordinated axes share an endstop input")
    return issues
