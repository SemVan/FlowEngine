"""Developer workbench. All hardware writes share the existing sender/queue."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from flowengine.api.deps import AppContext, get_ctx
from flowengine.api.router_diagnostics import state as state_snapshot
from flowengine.api.router_motion import _require_motion_enabled, _wrap_errors
from flowengine.calibration import PumpMeasurement, calibrate_pump, reference_error
from flowengine.logging_setup import audit
from flowengine.state import State

router = APIRouter(prefix="/api/workbench", tags=["workbench"])
Context = Annotated[AppContext, Depends(get_ctx)]


@router.get("/report")
async def report(ctx: Context):
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "state": (await state_snapshot(ctx)).model_dump(),
        "config": {"device_map": ctx.device_map.model_dump(), "runtime": ctx.runtime.model_dump()},
        "procedure_status": ctx.runner.status,
        "serial_trace": list(ctx.queue.trace),
        "note": "In-memory session only, last 1000 TX/RX entries. No proof of physical position or stop.",
    }


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RawRequest(Request):
    command: str
    confirmed: bool = False


class MotorsRequest(Request):
    enabled: bool
    axes: list[str] | None = None


class MotionRequest(Request):
    axis: str
    by: float
    feedrate: float = Field(gt=0)
    units: Literal["axis", "steps"] = "axis"
    speed_units: Literal["axis/min", "axis/s", "steps/s"] = "axis/min"
    acceleration: float | None = Field(default=None, gt=0)


class PumpCalibration(Request):
    name: str
    measurements: list[PumpMeasurement] = Field(min_length=1)
    steps_per_unit: float | None = Field(default=None, gt=0)


class ReferenceComparison(Request):
    baseline_pulses: float
    measured_pulses: list[float] = Field(min_length=1)
    tolerance_pulses: float = Field(ge=0)


class CurrentRequest(Request):
    axis: str
    current_ma: int = Field(gt=0)


@router.get("/capabilities")
async def capabilities(ctx: Context):
    return {
        "raw_console": True,
        "coordinated_motion": True,
        "probe_axes_configured": ctx.runtime.firmware.verified_probe_axes,
        "probe_channel": ctx.runtime.firmware.probe_channel,
        "probe_target_verified": ctx.runtime.firmware.probe_target_verified,
        "current_axes_verified": ctx.runtime.firmware.verified_current_axes,
        "holding_current": False,
        "warnings": [
            "Configured capabilities still require bench verification.",
            "Shared-input G28 is blocked; use verified firmware-stopped seek.",
            "Position counters are not encoder feedback.",
            "Serial Stop is not a physical emergency stop; restart after Stop.",
            "Raw ADC and per-axis enable depend on firmware and wiring.",
        ],
    }


@router.post("/console")
@_wrap_errors
async def console(body: RawRequest, ctx: Context):
    ctx.state.require(State.CONNECTED_IDLE)
    if ctx.runner.status["running"]:
        raise HTTPException(409, "procedure is running; console is blocked")
    _command, readonly = ctx.sender.validate_raw(body.command, body.confirmed)
    if not readonly:
        _require_motion_enabled(ctx)
    await ctx.state.transition(State.MOVING, detail="developer console")
    try:
        result = await ctx.sender.raw(body.command, body.confirmed)
        if not readonly:
            await ctx.sender.wait_idle()
    except Exception:
        ctx.motion.invalidate()
        await ctx.state.transition(
            State.ERRORED, detail="console failed; controller state unverified"
        )
        raise
    else:
        await ctx.state.transition(State.CONNECTED_IDLE, detail="console complete")
    audit("console", command=body.command, confirmed=body.confirmed)
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "command": body.command,
        "raw": result.raw,
        "ok": result.ok,
        "homed": ctx.motion.homed,
    }


@router.post("/motors")
@_wrap_errors
async def motors(body: MotorsRequest, ctx: Context):
    _require_motion_enabled(ctx)
    ctx.state.require(State.CONNECTED_IDLE)
    await ctx.state.transition(State.MOVING, detail="motor enable/disable")
    try:
        await ctx.sender.motors(body.enabled, body.axes)
    except Exception:
        ctx.motion.invalidate()
        await ctx.state.transition(State.ERRORED, detail="motor control failed")
        raise
    await ctx.state.transition(State.CONNECTED_IDLE, detail="motor control complete")
    audit("motors", enabled=body.enabled, axes=body.axes)
    return {
        "ok": True,
        "homed": ctx.motion.homed,
        "warning": "driver state is commanded, not electrically measured",
    }


@router.post("/jog")
@_wrap_errors
async def jog(body: MotionRequest, ctx: Context):
    _require_motion_enabled(ctx)
    ctx.state.require(State.CONNECTED_IDLE)
    value, speed = ctx.sender.convert_move(
        body.axis, body.by, body.feedrate, body.units, body.speed_units
    )
    plan = ctx.motion.plan_relative(body.axis, value, speed)
    await ctx.state.transition(State.MOVING, detail="workbench jog")
    try:
        async with ctx.sender.acceleration(body.acceleration, [body.axis]):
            await ctx.sender.jog(body.axis, value, speed)
            positions = await ctx.sender.wait_idle()
    except Exception:
        ctx.motion.invalidate([body.axis])
        await ctx.state.transition(State.ERRORED, detail="jog failed; position unverified")
        raise
    await ctx.state.transition(State.CONNECTED_IDLE, detail="workbench jog complete")
    return {"ok": True, "positions": positions, "effective_feedrate_axis_min": plan.feedrate}


@router.get("/sensors")
@_wrap_errors
async def sensors(ctx: Context):
    ctx.state.require(State.CONNECTED_IDLE)
    if ctx.runner.status["running"]:
        raise HTTPException(409, "procedure is running")
    return {"timestamp": datetime.now(UTC).isoformat(), **await ctx.sender.sensors()}


@router.post("/calibrate-pump")
@_wrap_errors
async def pump_calibration(body: PumpCalibration, ctx: Context):
    pump = next((p for p in ctx.device_map.pumps if p.name == body.name), None)
    if pump is None:
        raise ValueError("unknown pump")
    steps = body.steps_per_unit or ctx.device_map.axis(pump.axis).steps_per_unit
    return {
        "name": pump.name,
        "steps_per_unit": steps,
        **calibrate_pump(steps, body.measurements),
        "applied": False,
    }


@router.post("/reference-error")
@_wrap_errors
async def compare(body: ReferenceComparison):
    return reference_error(body.baseline_pulses, body.measured_pulses, body.tolerance_pulses)


@router.post("/current")
@_wrap_errors
async def current(body: CurrentRequest, ctx: Context):
    _require_motion_enabled(ctx)
    ctx.state.require(State.CONNECTED_IDLE)
    cfg = ctx.device_map.axis(body.axis)
    if (
        body.axis not in ctx.runtime.firmware.verified_current_axes
        or cfg.driver_current_max_ma is None
    ):
        raise ValueError("driver current control and current limit are not verified")
    if len(body.axis) != 1 or body.current_ma > cfg.driver_current_max_ma:
        raise ValueError("unsupported axis or current above configured motor/driver limit")
    await ctx.state.transition(State.MOVING, detail="current configuration")
    try:
        await ctx.queue.send(
            f"M906 {body.axis}{body.current_ma}", timeout=ctx.runtime.timeouts.diagnostics
        )
        result = await ctx.queue.send("M906", timeout=ctx.runtime.timeouts.diagnostics)
    except Exception:
        ctx.motion.invalidate()
        await ctx.state.transition(State.ERRORED, detail="current setting failed")
        raise
    await ctx.state.transition(
        State.CONNECTED_IDLE, detail="current requested; check driver report"
    )
    audit("current", axis=body.axis, current_ma=body.current_ma)
    return {
        "raw": result.raw,
        "requested_ma": body.current_ma,
        "warning": "running RMS current, NOT a separate holding-current setting; check report",
    }
