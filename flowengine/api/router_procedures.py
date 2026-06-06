"""Procedures endpoints — Phase 2.

Phase 1 ships only `GET /api/procedures` (list of available procedures, loaded
from YAML and lint-checked). Execution endpoints return 501 until Phase 2.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from flowengine.errors import ConfigError
from flowengine.loaders import list_procedures, load_procedure

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/procedures", tags=["procedures"])


@router.get("")
async def list_all():
    out = []
    for path in list_procedures():
        try:
            proc = load_procedure(path)
        except ConfigError as e:
            out.append({"file": path.name, "ok": False, "error": str(e)})
            continue
        out.append({
            "file": path.name,
            "name": proc.name,
            "description": proc.description,
            "steps": len(proc.steps),
            "ok": True,
        })
    return out


@router.get("/{name}")
async def get_one(name: str):
    for path in list_procedures():
        if path.stem == name or path.name == name:
            try:
                proc = load_procedure(path)
            except ConfigError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            return proc.model_dump()
    raise HTTPException(status_code=404, detail=f"procedure {name!r} not found")


@router.post("/{name}/run")
async def run(name: str):
    raise HTTPException(status_code=501, detail="procedure execution is Phase 2")


@router.post("/abort")
async def abort():
    raise HTTPException(status_code=501, detail="procedure abort is Phase 2")
