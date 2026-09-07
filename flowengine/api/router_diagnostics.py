"""Diagnostics: firmware info, endstop snapshot, state."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from flowengine.api.deps import AppContext, get_ctx
from flowengine.errors import TransportError
from flowengine.schemas import DiagnosticResponse, EndstopsSnapshot, FirmwareInfo, StateResponse

router = APIRouter(prefix="/api", tags=["diagnostics"])
Context = Annotated[AppContext, Depends(get_ctx)]


@router.get("/state", response_model=StateResponse)
async def state(ctx: Context):
    firmware = ctx.runtime.firmware.flavor
    return StateResponse(
        state=ctx.state.state.value,  # type: ignore[arg-type]
        detail=ctx.state.detail,
        positions=ctx.motion.positions,
        homed=ctx.motion.homed,
        queue_depth=ctx.queue.depth,
        firmware=firmware,
        transport=ctx.transport.name,
        port=getattr(ctx.transport, "port", None),
        baud=getattr(ctx.transport, "baud", None),
        motion_enabled=ctx.runtime.motion.enabled,
    )


@router.get("/diagnostics/firmware", response_model=FirmwareInfo)
async def firmware(ctx: Context):
    try:
        raw, caps = await ctx.sender.read_firmware()
    except TransportError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return FirmwareInfo(raw=raw, flavor=ctx.runtime.firmware.flavor, features=sorted(caps))


@router.get("/diagnostics/endstops", response_model=EndstopsSnapshot)
async def endstops(ctx: Context):
    try:
        result = await ctx.sender.read_diagnostic("M119")
    except TransportError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return EndstopsSnapshot(triggered=result.endstops or {}, raw="\n".join(result.raw))


@router.get("/diagnostics/position", response_model=DiagnosticResponse)
async def position(ctx: Context):
    return await _diagnostic(ctx, "M114")


@router.get("/diagnostics/settings", response_model=DiagnosticResponse)
async def settings(ctx: Context):
    return await _diagnostic(ctx, "M503")


@router.get("/diagnostics/drivers", response_model=DiagnosticResponse)
async def drivers(ctx: Context):
    return await _diagnostic(ctx, "M122")


async def _diagnostic(ctx: AppContext, command: str) -> DiagnosticResponse:
    try:
        result = await ctx.sender.read_diagnostic(command)
    except TransportError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return DiagnosticResponse(command=command, raw=result.raw, positions=result.position)
