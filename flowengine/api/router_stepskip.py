"""Step-skipping test endpoints — Phase 3."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/stepskip", tags=["stepskip"])


@router.post("/run")
async def run():
    raise HTTPException(status_code=501, detail="step-skipping test is Phase 3")


@router.get("/results")
async def results():
    raise HTTPException(status_code=501, detail="step-skipping test is Phase 3")
