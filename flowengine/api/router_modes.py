"""Modes endpoints — Phase 2."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from flowengine.api.deps import AppContext, get_ctx

router = APIRouter(prefix="/api/modes", tags=["modes"])


@router.get("")
async def list_modes(ctx: AppContext = Depends(get_ctx)):
    if ctx.modes is None:
        return {"default": None, "modes": []}
    return ctx.modes.model_dump()


@router.post("/{name}")
async def switch_mode(name: str):
    raise HTTPException(status_code=501, detail="mode switching is Phase 2")
