"""настройка кранов и запуск операций из интерфейса"""
from __future__ import annotations

import asyncio
import os
import tempfile
from functools import partial
from pathlib import Path
from typing import Annotated, Literal

import yaml
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from flowengine.api.deps import AppContext, get_ctx
from flowengine.api.router_axis_calibration import _apply_runtime_device_map
from flowengine.api.router_motion import _require_motion_enabled, _wrap_errors
from flowengine.config import user_overlay_dir
from flowengine.config_store import find_saved, load_yaml, save_yaml
from flowengine.errors import StateError
from flowengine.schemas import ConfigurationProfile, DeviceMap
from flowengine.schemas.device import SharedValveEndstop, ValveConfig
from flowengine.state import State

router = APIRouter(prefix="/api/valves", tags=["valves"])
Context = Annotated[AppContext, Depends(get_ctx)]


class ValveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    action: Literal["home", "test_home", "go_a", "go_b", "calibrate", "jog", "service_jog", "save_a", "save_b"]
    delta: float = 0.1


class ValveSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shared_valve_endstop: SharedValveEndstop
    valves: list[ValveConfig]


def persist(ctx, device):
    """проверяем настройки и сохраняем профиль через временный файл"""
    device = DeviceMap.model_validate(device.model_dump())
    if ctx.profile_name != "default":
        profile = load_yaml(find_saved("profiles", ctx.profile_name), ConfigurationProfile)
        save_yaml("profiles", ctx.profile_name, profile.model_copy(update={"device_map": device}))
    else:
        path = user_overlay_dir() / "device_map.yaml"
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".valves-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                yaml.safe_dump(device.model_dump(mode="json"), stream, allow_unicode=True, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    _apply_runtime_device_map(ctx, device)


def require_idle(ctx):
    ctx.state.require(State.CONNECTED_IDLE)
    if ctx.runner.status["running"] or ctx.sender.valves.lock.locked() or ctx.sender.manual_session:
        raise StateError("Equipment busy; finish the active operation first")


@router.get("")
@_wrap_errors
async def status(ctx: Context):
    subsystem = ctx.sender.valves
    error = None
    # во время движения M119 опрашивает сама операция
    if ctx.state.state == State.CONNECTED_IDLE and not subsystem.lock.locked() and not ctx.sender.manual_session:
        try:
            await subsystem.read()
        except Exception as exc:
            error = str(exc)
    job = getattr(subsystem, "job", None)
    return {**subsystem.snapshot(), "error": error,
            "job": {k: v for k, v in job.items() if k != "task"} if job else None}


@router.get("/inputs")
@_wrap_errors
async def inputs(ctx: Context):
    require_idle(ctx)
    switches = await ctx.sender.read_endstops()
    return {"inputs": switches, "z_candidates": [k for k in switches if k.lower().startswith("z")]}


@router.put("/config")
@_wrap_errors
async def configure(body: ValveSetup, ctx: Context):
    require_idle(ctx)
    if {v.name: v.axis for v in body.valves} != {v.name: v.axis for v in ctx.device_map.valves}:
        raise ValueError("Keep existing valve names/axes; axis mapping belongs to the hardware profile")
    channel = body.shared_valve_endstop.channel
    if not channel or channel not in await ctx.sender.read_endstops():
        raise ValueError("Select an available M119 input for the shared valve endstop")
    # после изменения настроек A/B нужно подтвердить заново
    configured = [v.model_copy(update={"calibrated": False}) for v in body.valves]
    device = ctx.device_map.model_copy(update={"valves": configured,
                                              "shared_valve_endstop": body.shared_valve_endstop})
    persist(ctx, device)
    ctx.sender.valves.invalidate()
    return {"ok": True}


async def run_job(ctx, subsystem, job, operation, machine_state):
    try:
        await operation()
        job["state"] = "completed"
        if ctx.sender is subsystem.sender and ctx.state.state == machine_state:
            await ctx.state.transition(State.CONNECTED_IDLE, detail="Valve operation complete")
    except BaseException as exc:
        job.update(state="failed", error=str(exc))
        if ctx.sender is subsystem.sender and ctx.state.state not in {State.ERRORED, State.DISCONNECTED}:
            await ctx.state.transition(State.ERRORED, detail=f"Valve fault: {exc}")


async def start_job(ctx, operation, *, homing=False):
    require_idle(ctx)
    subsystem = ctx.sender.valves
    machine_state = State.HOMING if homing else State.MOVING
    await ctx.state.transition(machine_state, detail="Valve operation")
    job = {"state": "running", "error": None}
    subsystem.job = job
    job["task"] = asyncio.create_task(run_job(ctx, subsystem, job, operation, machine_state))
    return {"ok": True, "state": "running"}


@router.post("/home-all")
@_wrap_errors
async def home_all(ctx: Context):
    require_idle(ctx)
    if await ctx.sender.valves.read():
        raise StateError("Shared valve endstop TRIGGERED / SOURCE UNKNOWN; service release required")
    return await start_job(ctx, ctx.sender.valves.home_all, homing=True)


@router.post("/{name}")
@_wrap_errors
async def action(name: str, body: ValveRequest, ctx: Context):
    require_idle(ctx)
    subsystem = ctx.sender.valves
    cfg = subsystem.config(name)
    if body.action == "test_home" and len(cfg.axis) != 1:
        raise ValueError("Test Home release requires a single-letter motor axis")
    if body.action in {"jog", "service_jog"}:
        # неверный шаг отклоняем до запуска задачи и движения
        subsystem.validate_jog(cfg.name, body.delta, service=body.action == "service_jog")
    if body.action in {"save_a", "save_b"}:
        position = "A" if body.action == "save_a" else "B"
        coordinate = await subsystem.capture(cfg.name, position)
        st = subsystem.states[cfg.name]
        saved_a, saved_b = st.saved_a or position == "A", st.saved_b or position == "B"
        updates = {"position_a" if position == "A" else "position_b": coordinate,
                   "calibrated": saved_a and saved_b}
        updated = cfg.model_copy(update=updates)
        if updated.calibrated and abs(updated.position_a - updated.position_b) < 0.001:
            raise ValueError("A and B must be different mechanical positions")
        persist(ctx, ctx.device_map.model_copy(update={"valves": [
            updated if v.name == cfg.name else v for v in ctx.device_map.valves]}))
        st.saved_a, st.saved_b = saved_a, saved_b
        st.current = position
        return {"ok": True, "coordinate": coordinate,
                "steps_from_home": round(coordinate * ctx.device_map.axis(cfg.axis).steps_per_unit),
                "calibrated": updated.calibrated}
    service = body.action == "service_jog"
    if await subsystem.read() and not service:
        raise StateError("Shared valve endstop TRIGGERED / SOURCE UNKNOWN; service release required")
    if body.action in {"go_a", "go_b"}:
        _require_motion_enabled(ctx)
        if not cfg.calibrated or not ctx.motion.homed[cfg.axis]:
            raise StateError("Valve calibration and Home required before Go A/B")
        operation = partial(subsystem.set_valve, cfg.name, "A" if body.action == "go_a" else "B")
    elif body.action == "home":
        operation = partial(subsystem.home_valve, cfg.name)
    elif body.action == "test_home":
        operation = partial(subsystem.test_home, cfg.name)
    elif body.action == "calibrate":
        persist(ctx, ctx.device_map.model_copy(update={"valves": [
            v.model_copy(update={"calibrated": False}) if v.name == cfg.name else v
            for v in ctx.device_map.valves]}))
        operation = partial(subsystem.calibrate, cfg.name)
    else:
        operation = partial(subsystem.jog, cfg.name, body.delta, service=service)
    return await start_job(ctx, operation, homing=body.action in {"home", "test_home", "calibrate"})
