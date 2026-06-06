"""Diagnostics: firmware info, endstop snapshot, state."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from flowengine.api.deps import AppContext, get_ctx
from flowengine.errors import TransportError
from flowengine.schemas import EndstopsSnapshot, FirmwareInfo, StateResponse

router = APIRouter(prefix="/api", tags=["diagnostics"])


@router.get("/state", response_model=StateResponse)
async def state(ctx: AppContext = Depends(get_ctx)):
    firmware = ctx.runtime.firmware.flavor
    return StateResponse(
        state=ctx.state.state.value,  # type: ignore[arg-type]
        detail=ctx.state.detail,
        positions=ctx.motion.positions,
        homed=ctx.motion.homed,
        queue_depth=ctx.queue.depth,
        firmware=firmware,
        transport=ctx.transport.name,
    )


@router.get("/diagnostics/firmware", response_model=FirmwareInfo)
async def firmware(ctx: AppContext = Depends(get_ctx)):
    try:
        raw, caps = await ctx.sender.read_firmware()
    except TransportError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return FirmwareInfo(raw=raw, flavor=ctx.runtime.firmware.flavor, features=sorted(caps))


@router.get("/diagnostics/endstops", response_model=EndstopsSnapshot)
async def endstops(ctx: AppContext = Depends(get_ctx)):
    try:
        triggered = await ctx.sender.read_endstops()
    except TransportError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return EndstopsSnapshot(triggered=triggered, raw="")
