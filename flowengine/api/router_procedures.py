"""Procedures endpoints — Phase 2.

Phase 1 ships only `GET /api/procedures` (list of available procedures, loaded
from YAML and lint-checked). Execution endpoints return 501 until Phase 2.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from flowengine.api.deps import AppContext, get_ctx
from flowengine.errors import ConfigError, ProcedureError
from flowengine.loaders import list_procedures, load_procedure

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/procedures", tags=["procedures"])
Context = Annotated[AppContext, Depends(get_ctx)]


def _find(name: str):
    for path in list_procedures():
        if path.stem == name or path.name == name:
            return path
    raise HTTPException(status_code=404, detail=f"procedure {name!r} not found")


@router.get("")
async def list_all():
    out = []
    for path in list_procedures():
        try:
            proc = load_procedure(path)
        except ConfigError as e:
            out.append({"file": path.name, "ok": False, "error": str(e)})
            continue
        out.append(
            {
                "file": path.name,
                "name": proc.name,
                "description": proc.description,
                "steps": len(proc.steps),
                "ok": True,
            }
        )
    return out


@router.get("/status")
async def status(ctx: Context):
    return ctx.runner.status


@router.get("/{name}")
async def get_one(name: str):
    try:
        return load_procedure(_find(name)).model_dump()
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{name}/run")
async def run(name: str, ctx: Context):
    if ctx.transport.name != "mock" and not ctx.runtime.motion.enabled:
        raise HTTPException(status_code=409, detail="motion interlock is disabled")
    try:
        proc = load_procedure(_find(name))
        ctx.runner.start(proc)
    except (ConfigError, ProcedureError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True, **ctx.runner.status}


@router.post("/abort")
async def abort(ctx: Context):
    await ctx.runner.abort()
    return {"ok": True, **ctx.runner.status}
