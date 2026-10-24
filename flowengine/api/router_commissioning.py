"""Developer commissioning: save observations, apply profiles, export handoff."""

from __future__ import annotations

from contextlib import suppress
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from flowengine.api.deps import AppContext, get_ctx
from flowengine.api.router_motion import _require_motion_enabled
from flowengine.api.router_procedures import _load_named
from flowengine.calibration import PumpMeasurement, calibrate_pump
from flowengine.commissioning import CHECKS, fingerprint, procedures_fingerprint, record
from flowengine.config import user_overlay_dir
from flowengine.config_store import find_saved, load_yaml, save_yaml
from flowengine.errors import ConfigError, FlowEngineError
from flowengine.hardware import GcodeSender, MotionModel
from flowengine.hardware.homing import EndstopHoming
from flowengine.loaders import list_procedures, load_procedure
from flowengine.procedures import expand_procedure
from flowengine.procedures.preflight import execution_issues
from flowengine.procedures.runner import ProcedureRunner
from flowengine.schemas import ConfigurationProfile, Procedure
from flowengine.state import State

router = APIRouter(prefix="/api/commissioning", tags=["commissioning"])
Context = Annotated[AppContext, Depends(get_ctx)]


def saved(name):
    try:
        return load_yaml(find_saved("profiles", name), ConfigurationProfile)
    except ConfigError as exc:
        raise HTTPException(404, str(exc)) from exc


def idle(ctx):
    if (
        ctx.state.state != State.CONNECTED_IDLE
        or ctx.runner.status["running"]
        or ctx.queue.depth
        or ctx.queue.faulted
    ):
        raise HTTPException(409, "controller must be connected and idle")


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Observation(Request):
    kind: str
    device: str = "instrument"
    passed: bool = False
    notes: str = Field(min_length=1)
    data: dict = Field(default_factory=dict)


class PumpResult(Request):
    name: str
    measurements: list[PumpMeasurement] = Field(min_length=3)
    dispense_direction: Literal[-1, 1]
    notes: str = Field(min_length=1)
    confirmed: bool = False


class ValveResult(Request):
    name: str
    position_a: float
    position_b: float
    notes: str = Field(min_length=1)
    confirmed: bool = False


class Reference(Request):
    axis: str
    position: float
    notes: str = Field(min_length=1)
    confirmed: bool = False


class Bundle(Request):
    format: Literal["flowengine-handoff-v1"]
    profile: ConfigurationProfile
    procedures: list[Procedure] = Field(max_length=500)


class Period(Request):
    channel: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    direction: Literal[-1, 1]
    step: float = Field(gt=0, le=1)
    feedrate: float = Field(gt=0, le=120)
    max_distance: float = Field(gt=0)
    confirmed: bool = False


@router.post("/period")
async def period(body: Period, ctx: Context):
    idle(ctx)
    _require_motion_enabled(ctx)
    if not body.confirmed:
        raise HTTPException(400, "confirm sensor channel, free rotation and direction")
    await ctx.state.transition(State.MOVING, detail="wash pump sensor period measurement")
    try:
        result = await ctx.sender.valve_commissioning.measure_period(
            "U", body.channel, body.direction, body.step, body.feedrate, body.max_distance
        )
    except ValueError as exc:
        await ctx.state.transition(
            State.CONNECTED_IDLE, detail=f"period measurement stopped: {exc}"
        )
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        await ctx.state.transition(
            State.ERRORED, detail="period measurement lost controller response"
        )
        raise HTTPException(502, str(exc)) from exc
    await ctx.state.transition(State.CONNECTED_IDLE, detail="period measured; U motor released")
    return result


@router.get("/active")
async def active(ctx: Context):
    previous = None
    if ctx.profile_name != "default":
        with suppress(HTTPException):
            previous = saved(ctx.profile_name)
    return {
        "profile": ConfigurationProfile(
            name=ctx.profile_name if ctx.profile_name != "default" else "bench_instrument",
            description=previous.description if previous else "",
            device_map=ctx.device_map,
            runtime=ctx.runtime,
            modes=ctx.modes,
            rack=previous.rack if previous else None,
            commissioning=previous.commissioning if previous else [],
        ).model_dump(mode="json"),
        "storage_directory": str(user_overlay_dir()),
        "checks": CHECKS,
    }


@router.post("/profiles/{name}/apply")
async def apply_profile(name: str, ctx: Context):
    idle(ctx)
    profile = saved(name)
    if (
        profile.runtime.transport != ctx.runtime.transport
        or profile.runtime.firmware != ctx.runtime.firmware
    ):
        raise HTTPException(
            409, "transport/firmware settings changed; restart with --profile " + name
        )
    if profile.runtime.motion.configure_firmware:
        raise HTTPException(
            409, "live apply requires configure_firmware=false; apply firmware settings explicitly"
        )
    # No G-code and no persisted homed flags. Keep the existing serial connection.
    motion = MotionModel(profile.device_map, profile.runtime.motion.feedrate_cap)
    sender = GcodeSender(
        ctx.queue,
        motion,
        profile.device_map,
        EndstopHoming(profile.device_map, timeout_s=profile.runtime.timeouts.homing),
        profile.runtime.timeouts,
        profile.runtime.firmware,
        profile.runtime.motion.accel_cap,
    )
    ctx.device_map, ctx.runtime, ctx.modes = profile.device_map, profile.runtime, profile.modes
    ctx.queue.configure_default_timeout(profile.runtime.timeouts.ok_default)
    ctx.motion, ctx.sender = motion, sender
    recorder = ctx.runner.recorder
    ctx.runner = ProcedureRunner(sender, profile.device_map, ctx.state, ctx.events)
    ctx.runner.recorder = recorder
    ctx.profile_name = name
    await ctx.state.transition(
        State.CONNECTED_IDLE, detail=f"profile {name} applied; re-reference axes"
    )
    return {
        "ok": True,
        "profile": name,
        "homed": motion.homed,
        "warning": "All axes require re-reference. M92/M203 are not changed by applying a profile.",
    }


@router.post("/reference")
async def reference(body: Reference, ctx: Context):
    idle(ctx)
    _require_motion_enabled(ctx)
    if not body.confirmed:
        raise HTTPException(400, "confirm the physically established coordinate")
    try:
        cfg = ctx.device_map.axis(body.axis)
        lo, hi = (0, cfg.travel) if cfg.home_direction == "min" else (-cfg.travel, 0)
        if len(body.axis) != 1 or not lo <= body.position <= hi:
            raise ValueError("coordinate must be inside the configured single-letter axis range")
        await ctx.state.transition(State.HOMING, detail=f"manual reference {body.axis}")
        await ctx.sender.wait_idle()
        await ctx.queue.send(
            f"G92 {body.axis}{body.position:.4f}", timeout=ctx.runtime.timeouts.diagnostics
        )
        await ctx.queue.send(f"M17 {body.axis}", timeout=ctx.runtime.timeouts.diagnostics)
        ctx.motion.mark_referenced(body.axis, body.position)
        await ctx.state.transition(
            State.CONNECTED_IDLE, detail=f"manual reference {body.axis}; operator confirmed"
        )
        return {
            "ok": True,
            "axis": body.axis,
            "position": body.position,
            "notes": body.notes,
            "persisted_reference": False,
        }
    except Exception as exc:
        ctx.motion.invalidate([body.axis] if body.axis in ctx.motion.homed else None)
        if ctx.state.state == State.HOMING:
            await ctx.state.transition(State.ERRORED, detail="manual reference failed")
        raise HTTPException(400, str(exc)) from exc


@router.post("/profiles/{name}/observations")
async def observation(name: str, body: Observation):
    if body.kind not in {*CHECKS, "reference_result", "motion_result", "period_result"}:
        raise HTTPException(400, "unknown check; use a key from /api/commissioning/active")
    return record(saved(name), body.kind, body.device, body.notes, body.data, body.passed)


@router.post("/profiles/{name}/pump")
async def pump_result(name: str, body: PumpResult):
    profile = saved(name)
    pump = next((p for p in profile.device_map.pumps if p.name == body.name), None)
    if not body.confirmed or pump is None:
        raise HTTPException(400, "known pump and explicit measurement confirmation required")
    steps = profile.device_map.axis(pump.axis).steps_per_unit
    result = calibrate_pump(steps, body.measurements)
    replacement = pump.model_copy(
        update={
            "volume_per_unit_ul": result["volume_per_unit_ul"],
            "dispense_direction": body.dispense_direction,
            "calibrated": True,
            "calibration_notes": body.notes,
        }
    )
    device = profile.device_map.model_copy(
        update={
            "pumps": [replacement if p.name == pump.name else p for p in profile.device_map.pumps]
        }
    )
    profile = profile.model_copy(update={"device_map": device})
    return record(
        profile,
        "pump_calibration",
        body.name,
        body.notes,
        {
            "measurements": [m.model_dump(exclude_none=True) for m in body.measurements],
            "steps_per_unit": steps,
            **result,
        },
    )


@router.post("/profiles/{name}/valve")
async def valve_result(name: str, body: ValveResult):
    profile = saved(name)
    valve = next((v for v in profile.device_map.valves if v.name == body.name), None)
    if not body.confirmed or valve is None:
        raise HTTPException(400, "known valve and confirmation of both physical routes required")
    cfg = profile.device_map.axis(valve.axis)
    lo, hi = (0, cfg.travel) if cfg.home_direction == "min" else (-cfg.travel, 0)
    if body.position_a == body.position_b or not all(
        lo <= p <= hi for p in (body.position_a, body.position_b)
    ):
        raise HTTPException(
            400, "two distinct working coordinates must fit configured travel/home direction"
        )
    replacement = valve.model_copy(
        update={
            "position_a": body.position_a,
            "position_b": body.position_b,
            "calibrated": True,
            "calibration_notes": body.notes,
        }
    )
    device = profile.device_map.model_copy(
        update={
            "valves": [
                replacement if v.name == valve.name else v for v in profile.device_map.valves
            ]
        }
    )
    profile = profile.model_copy(update={"device_map": device})
    return record(
        profile,
        "valve_calibration",
        body.name,
        body.notes,
        {"position_a": body.position_a, "position_b": body.position_b},
    )


@router.get("/profiles/{name}/readiness")
async def readiness(name: str, ctx: Context):
    profile = saved(name)
    digest = fingerprint(profile)
    procedures_digest = procedures_fingerprint()
    checks = []
    for key, label in CHECKS.items():
        latest = next((r for r in reversed(profile.commissioning) if r.kind == key), None)
        current = bool(
            latest
            and latest.data.get("config_fingerprint") == digest
            and latest.data.get("procedures_fingerprint") == procedures_digest
        )
        checks.append(
            {
                "key": key,
                "label": label,
                "passed": bool(current and latest.passed),
                "stale": bool(latest and not current),
                "notes": latest.notes if latest else "",
            }
        )
    procedures = []
    for proc_name in (
        "startup",
        "wash",
        "sample_intake",
        "analysis_delivery",
        "cyclic_measurement",
    ):
        try:
            proc = _load_named(proc_name)
            expanded = expand_procedure(proc, _load_named, allow_draft=True)
            issues = execution_issues(expanded.procedure, profile.device_map, profile.runtime)
            for pump in profile.device_map.pumps:
                if not pump.allowed_routes:
                    issues.append(f"pump {pump.name}: allowed_routes not configured")
            try:
                expand_procedure(proc, _load_named, allow_draft=False)
                drafts_clear = True
            except (FlowEngineError, ValueError):
                drafts_clear = False
            procedures.append(
                {
                    "name": proc_name,
                    "draft": proc.draft,
                    "nested_drafts_clear": drafts_clear,
                    "issues": issues,
                    "steps": len(expanded.procedure.steps),
                }
            )
        except (FlowEngineError, ValueError) as exc:
            procedures.append({"name": proc_name, "issues": [str(exc)]})
    active_matches = profile.device_map == ctx.device_map and profile.runtime == ctx.runtime
    unreferenced = [axis for axis, referenced in ctx.motion.homed.items() if not referenced]
    return {
        "profile": name,
        "active_matches": active_matches,
        "checks": checks,
        "procedures": procedures,
        "homed": ctx.motion.homed,
        "unreferenced_axes": unreferenced,
        "ready": active_matches
        and ctx.state.state == State.CONNECTED_IDLE
        and not ctx.queue.faulted
        and all(c["passed"] for c in checks)
        and not unreferenced
        and all(not p["issues"] and p.get("nested_drafts_clear") for p in procedures),
        "note": "Evidence is operator-confirmed, not sensor proof. Draft flags remain explicit. Recheck after procedure edits.",
    }


@router.get("/profiles/{name}/bundle")
async def export_bundle(name: str):
    return {
        "format": "flowengine-handoff-v1",
        "profile": saved(name).model_dump(mode="json"),
        "procedures": [load_procedure(p).model_dump(mode="json") for p in list_procedures()],
    }


@router.post("/import")
async def import_bundle(body: Bundle):
    names = [p.name for p in body.procedures]
    if len(names) != len(set(names)):
        raise HTTPException(400, "duplicate procedure names")
    # Validate every name and nested dependency before writing the first file.
    from flowengine.config_store import validate_name

    try:
        validate_name(body.profile.name)
        by_name = {p.name: p for p in body.procedures}

        def visit(proc, stack):
            if proc.name in stack or len(stack) >= 8:
                raise ValueError("recursive or excessively deep procedure dependency")
            for step in proc.steps:
                if step.op in {"call", "repeat"}:
                    visit(by_name[step.procedure], (*stack, proc.name))

        for proc in body.procedures:
            validate_name(proc.name)
            visit(proc, ())
    except (ConfigError, FlowEngineError, ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    # Each file is atomically replaced. Import never activates hardware settings.
    for proc in body.procedures:
        save_yaml("procedures", proc.name, proc)
    path = save_yaml("profiles", body.profile.name, body.profile)
    return {"ok": True, "file": str(path), "procedures": names, "applied": False}
