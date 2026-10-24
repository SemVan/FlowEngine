"""Developer-only valve commissioning endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from flowengine.api.deps import AppContext, get_ctx
from flowengine.api.router_motion import _require_motion_enabled
from flowengine.errors import ControllerError, StateError, TransportError
from flowengine.logging_setup import audit
from flowengine.state import State

router = APIRouter(prefix="/api/valves", tags=["valves"])
Context = Annotated[AppContext, Depends(get_ctx)]


class ServiceJogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str
    channel: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    delta: float
    feedrate: float = 30.0


class ValveHomeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str
    channel: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    direction: Literal[-1, 1]
    step: float = 0.1
    feedrate: float = 30.0
    search_distance: float
    release_distance: float | None = None
    pull_off: float = 0.5
    release_motor: bool = True


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, StateError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (ControllerError, TransportError)):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("")
async def valves(ctx: Context):
    return {
        "valves": [
            {
                **item.model_dump(),
                "axis_config": ctx.device_map.axis(item.axis).model_dump(),
                "referenced": ctx.motion.homed[item.axis],
                "position": ctx.motion.positions[item.axis],
            }
            for item in ctx.device_map.valves
        ]
    }


@router.get("/endstop/{channel}")
async def endstop(channel: str, ctx: Context):
    try:
        return {"channel": channel, "triggered": await ctx.sender.valve_commissioning.endstop(channel)}
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/service-jog")
async def service_jog(body: ServiceJogRequest, ctx: Context):
    try:
        _require_motion_enabled(ctx)
        ctx.state.require(State.CONNECTED_IDLE)
        await ctx.state.transition(State.MOVING, detail=f"valve service jog {body.name}")
        result = await ctx.sender.valve_commissioning.service_jog(**body.model_dump())
        await ctx.state.transition(State.CONNECTED_IDLE, detail="valve service jog complete; motor released")
        audit("valve_service_jog", valve=body.name, delta=body.delta, channel=body.channel)
        return result
    except Exception as exc:
        if ctx.state.state == State.MOVING:
            await ctx.state.transition(State.CONNECTED_IDLE, detail=f"valve service jog stopped: {exc}")
        raise _error(exc) from exc


@router.post("/home")
async def home(body: ValveHomeRequest, ctx: Context):
    try:
        _require_motion_enabled(ctx)
        ctx.state.require(State.CONNECTED_IDLE)
        await ctx.state.transition(State.HOMING, detail=f"stepwise valve home {body.name}")
        result = await ctx.sender.valve_commissioning.home_stepwise(**body.model_dump())
        detail = "test home complete; motor released" if body.release_motor else "valve referenced"
        await ctx.state.transition(State.CONNECTED_IDLE, detail=detail)
        audit("valve_home_stepwise", valve=body.name, channel=body.channel, release=body.release_motor)
        return result
    except Exception as exc:
        if ctx.state.state == State.HOMING:
            await ctx.state.transition(State.CONNECTED_IDLE, detail=f"valve home stopped: {exc}")
        raise _error(exc) from exc
