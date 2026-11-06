"""парковка и запуск рабочей сессии"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from flowengine.api.deps import AppContext, get_ctx
from flowengine.api.router_motion import _wrap_errors
from flowengine.config_store import find_saved, load_yaml, save_yaml
from flowengine.errors import StateError
from flowengine.parking import park_and_finish, parking_status, restore_parking
from flowengine.schemas import ConfigurationProfile, DeviceMap
from flowengine.state import State

router = APIRouter(prefix="/api/session", tags=["session"])
Context = Annotated[AppContext, Depends(get_ctx)]


@router.get("/parking")
async def status(ctx: Context):
    return parking_status(ctx)


@router.post("/start")
@_wrap_errors
async def start(ctx: Context):
    return await restore_parking(ctx)


@router.post("/park")
@_wrap_errors
async def finish(ctx: Context):
    return await park_and_finish(ctx)


@router.post("/parking/capture")
@_wrap_errors
async def capture(ctx: Context):
    ctx.state.require(State.CONNECTED_IDLE)
    if ctx.runner.status["running"] or ctx.queue.depth or ctx.sender.valves.lock.locked() or ctx.sender.manual_session:
        raise StateError("Аппарат занят")
    if ctx.profile_name == "default":
        raise ValueError("Сначала сохраните личный профиль")
    axes = [a.marlin_axis for a in ctx.device_map.axes if a.kind in {"syringe_pump", "valve"}]
    if not axes or not all(ctx.motion.homed[a] for a in axes):
        raise StateError("Сначала установите привязку и выставьте узлы в нужные парковочные положения")
    profile = load_yaml(find_saved("profiles", ctx.profile_name), ConfigurationProfile)
    if profile.device_map != ctx.device_map:
        raise StateError("Сохранённый профиль отличается от активного; сначала примените его")
    positions = await ctx.sender.wait_idle()
    await ctx.sender._check_manual_endstops(axes)
    if ctx.device_map.valves and await ctx.sender.valves.read():
        raise StateError("Для парковки краны должны отпустить общий концевик")
    data = ctx.device_map.model_dump()
    data["parking"] = {"positions": {a: positions[a] for a in axes}, "feedrates": {}}
    device = DeviceMap.model_validate(data)
    save_yaml("profiles", profile.name, profile.model_copy(update={"device_map": device}))
    ctx.device_map = ctx.motion._map = ctx.sender._map = device
    ctx.sender._homing._map = ctx.sender.valve_commissioning._map = device
    ctx.runner._map = device
    return {"saved": True, "parking": device.parking.model_dump(), "movement": False}
