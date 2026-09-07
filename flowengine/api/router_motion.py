"""Motion endpoints: jog, move, home, stop.

Idempotency: every mutating motion endpoint accepts an `Idempotency-Key` header.
Repeating the same key returns the cached result instead of moving twice. This
guards against UI retries during transient network blips.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from flowengine.api.deps import AppContext, get_ctx
from flowengine.errors import ControllerError, SoftLimitError, StateError, TransportError
from flowengine.logging_setup import audit
from flowengine.schemas import HomeRequest, JogRequest, MoveRequest
from flowengine.state import State

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["motion"])

_idem_cache: dict[str, dict[str, Any]] = {}
_IDEM_MAX = 256


def _idem_get(key: str | None) -> dict[str, Any] | None:
    if key is None:
        return None
    return _idem_cache.get(key)


def _idem_put(key: str | None, value: dict[str, Any]) -> None:
    if key is None:
        return
    if len(_idem_cache) >= _IDEM_MAX:
        _idem_cache.pop(next(iter(_idem_cache)))
    _idem_cache[key] = value


def _wrap_errors(fn):  # type: ignore[no-untyped-def]
    @wraps(fn)
    async def inner(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return await fn(*args, **kwargs)
        except SoftLimitError as e:
            raise HTTPException(status_code=400, detail=f"soft-limit: {e}") from e
        except StateError as e:
            raise HTTPException(status_code=409, detail=f"state: {e}") from e
        except ControllerError as e:
            raise HTTPException(status_code=502, detail=f"controller: {e}") from e
        except TransportError as e:
            raise HTTPException(status_code=503, detail=f"transport: {e}") from e

    return inner


def _require_motion_enabled(ctx: AppContext) -> None:
    if ctx.transport.name != "mock" and not ctx.runtime.motion.enabled:
        raise StateError(
            "motion interlock is disabled; verify the firmware axis map and limits, "
            "then set runtime.motion.enabled=true"
        )


@router.post("/jog")
@_wrap_errors
async def jog(
    body: JogRequest,
    ctx: AppContext = Depends(get_ctx),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    cached = _idem_get(idempotency_key)
    if cached:
        return cached
    _require_motion_enabled(ctx)
    ctx.state.require(State.CONNECTED_IDLE)
    await ctx.state.transition(State.MOVING, detail=f"jog {body.axis} {body.delta:+.3f}")
    try:
        await ctx.sender.jog(body.axis, body.delta, body.feedrate)
        positions = await ctx.sender.wait_idle()
    finally:
        await ctx.state.transition(State.CONNECTED_IDLE, detail="jog complete")
    audit("jog", axis=body.axis, delta=body.delta, feedrate=body.feedrate)
    result = {"ok": True, "positions": positions}
    _idem_put(idempotency_key, result)
    return result


@router.post("/move")
@_wrap_errors
async def move(
    body: MoveRequest,
    ctx: AppContext = Depends(get_ctx),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    cached = _idem_get(idempotency_key)
    if cached:
        return cached
    _require_motion_enabled(ctx)
    ctx.state.require(State.CONNECTED_IDLE)
    await ctx.state.transition(State.MOVING, detail=f"move {body.axis}→{body.target:.3f}")
    try:
        await ctx.sender.move_to(body.axis, body.target, body.feedrate)
        positions = await ctx.sender.wait_idle()
    finally:
        await ctx.state.transition(State.CONNECTED_IDLE, detail="move complete")
    audit("move", axis=body.axis, target=body.target, feedrate=body.feedrate)
    result = {"ok": True, "positions": positions}
    _idem_put(idempotency_key, result)
    return result


@router.post("/home")
@_wrap_errors
async def home(body: HomeRequest, ctx: AppContext = Depends(get_ctx)):
    _require_motion_enabled(ctx)
    ctx.state.require(State.CONNECTED_IDLE, State.ERRORED)
    await ctx.state.transition(State.HOMING, detail=f"home {body.axes or 'all'}")
    try:
        await ctx.sender.home(body.axes)
    finally:
        await ctx.state.transition(State.CONNECTED_IDLE, detail="homed")
    audit("home", axes=body.axes or "all")
    return {"ok": True, "homed": ctx.motion.homed}


@router.post("/stop")
@_wrap_errors
async def stop(ctx: AppContext = Depends(get_ctx)):
    await ctx.state.transition(State.ABORTING, detail="operator abort")
    await ctx.queue.abort()
    ctx.queue.reset_abort()
    await ctx.state.transition(State.CONNECTED_IDLE, detail="aborted")
    audit("stop")
    return {"ok": True}
