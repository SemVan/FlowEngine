"""Procedures endpoints — Phase 2.

Phase 1 ships only `GET /api/procedures` (list of available procedures, loaded
from YAML and lint-checked). Execution endpoints return 501 until Phase 2.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from flowengine.api.deps import AppContext, get_ctx
from flowengine.config_store import save_yaml
from flowengine.errors import ConfigError, FlowEngineError
from flowengine.loaders import list_procedures, load_procedure
from flowengine.procedures import expand_procedure
from flowengine.schemas import CallStep, Procedure

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/procedures", tags=["procedures"])
Context = Annotated[AppContext, Depends(get_ctx)]
RunParameters = Annotated[dict[str, float | int | str | bool] | None, Body()]


def _find(name: str):
    for path in list_procedures():
        if path.stem == name or path.name == name:
            return path
    raise HTTPException(status_code=404, detail=f"procedure {name!r} not found")


def _load_named(name: str) -> Procedure:
    return load_procedure(_find(name))


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
                "draft": proc.draft,
                "parameters": list(proc.parameters),
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


@router.put("/{name}")
async def save(name: str, procedure: Procedure):
    if procedure.name != name:
        raise HTTPException(status_code=400, detail="URL name must match procedure.name")
    try:
        path = save_yaml("procedures", name, procedure)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "name": name, "file": path.name}


@router.post("/{name}/preview")
async def preview(
    name: str,
    parameters: RunParameters = None,
):
    """Resolve a saved procedure without sending anything to the controller."""
    try:
        source = _load_named(name)
        expanded = expand_procedure(
            source,
            _load_named,
            parameters,
            allow_draft=True,
        )
    except (FlowEngineError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "name": name,
        "draft": source.draft,
        "steps": [
            step.model_dump(mode="json", exclude_none=True) for step in expanded.procedure.steps
        ],
        "step_paths": expanded.step_paths,
    }


@router.post("/{name}/steps/{step_number}/run")
async def run_step(
    name: str,
    step_number: int,
    ctx: Context,
    parameters: RunParameters = None,
):
    """Execute one selected step, including from a draft procedure."""
    if ctx.transport.name != "mock" and not ctx.runtime.motion.enabled:
        raise HTTPException(status_code=409, detail="motion interlock is disabled")
    try:
        source = _load_named(name)
        resolved = source.resolve(parameters, allow_draft=True)
        if step_number < 1 or step_number > len(resolved.steps):
            raise ValueError(f"step number must be between 1 and {len(resolved.steps)}")
        step = resolved.steps[step_number - 1]
        if isinstance(step, CallStep):
            expanded = expand_procedure(
                _load_named(step.procedure),
                _load_named,
                step.parameters,
                allow_draft=True,
            )
            paths = [f"{name} step {step_number} → {path}" for path in expanded.step_paths]
            await ctx.runner.run_selected(
                name,
                step_number,
                expanded.procedure.steps,
                paths,
            )
        else:
            await ctx.runner.run_one(name, step_number, step)
    except (FlowEngineError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "single_step": True,
        "operation": step.op,
        **ctx.runner.status,
    }


@router.post("/{name}/run")
async def run(
    name: str,
    ctx: Context,
    parameters: RunParameters = None,
):
    if ctx.transport.name != "mock" and not ctx.runtime.motion.enabled:
        raise HTTPException(status_code=409, detail="motion interlock is disabled")
    try:
        source = _load_named(name)
        expanded = expand_procedure(source, _load_named, parameters)
        ctx.runner.start(expanded.procedure, expanded.step_paths)
    except (FlowEngineError, ValueError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True, **ctx.runner.status}


@router.post("/abort")
async def abort(ctx: Context):
    await ctx.runner.abort()
    return {"ok": True, **ctx.runner.status}
