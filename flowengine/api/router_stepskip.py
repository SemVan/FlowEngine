"""сравниваем Count при возврате к HOME; люфт и повторяемость микрика тоже влияют на результат"""
from __future__ import annotations

import asyncio
import secrets
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from flowengine.api.deps import AppContext, get_ctx
from flowengine.api.router_motion import _wrap_errors
from flowengine.errors import MotionValidationError, StateError
from flowengine.logging_setup import audit
from flowengine.state import State

router = APIRouter(prefix="/api/stepskip", tags=["stepskip"])


class StepCheckStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axes: list[str] = Field(min_length=1)
    tolerance_steps: float = Field(default=8.0, ge=0)


def _targets(ctx: AppContext, axes: list[str]) -> list[str]:
    if ctx.runner.status["running"]:
        raise StateError("procedure is running; step check is blocked")
    result: list[str] = []
    for raw in axes:
        axis = raw.upper()
        if axis in result:
            continue
        cfg = ctx.device_map.axis(axis)
        if axis not in {"X", "Y"} or cfg.axis_calibration is None or not cfg.endstop_channels_shared:
            raise ValueError(f"{axis}: сначала выполните физическую калибровку X/Y")
        if not ctx.motion.homed.get(axis, False):
            raise MotionValidationError(f"axis {axis} is not referenced; Home required before step check")
        if axis not in ctx.sender.reference_counts:
            raise ValueError(f"{axis}: нет эталонного Marlin Count; повторите Home")
        result.append(axis)
    return result


def _summarize(details: dict[str, Any]) -> dict[str, Any]:
    axes: dict[str, Any] = {}
    any_loss = False
    for axis, detail in details.items():
        check = detail.get("step_check") or {}
        status = check.get("status", "UNKNOWN")
        if status == "STEP_LOSS":
            any_loss = True
        axes[axis] = {
            "status": status,
            "baseline_count": check.get("baseline_count"),
            "measured_count": check.get("measured_count"),
            "delta_steps": check.get("delta_steps"),
            "abs_delta_steps": check.get("abs_delta_steps"),
            "tolerance_steps": check.get("tolerance_steps"),
            "referenced": bool(detail.get("referenced")),
            "home_side": detail.get("home_side"),
            "warning": check.get("warning"),
        }
    return {
        "status": "STEP_LOSS" if any_loss else "OK",
        "axes": axes,
        "warning": (
            "Это проверка ошибки возврата к физическому концевику. Она выявляет потерю позиции, "
            "но сама по себе не доказывает пропуск шагов: результат также включает люфт, "
            "повторяемость концевика и механическую упругость."
        ),
    }


async def _run_job(ctx: AppContext, targets: list[str], tolerance: float, job: dict[str, Any]) -> None:
    async def progress(detail: str) -> None:
        job["detail"] = detail
        if ctx.state.state == State.HOMING:
            await ctx.state.transition(State.HOMING, detail=detail)

    try:
        details = await ctx.sender.check_reference(
            targets, tolerance_steps=tolerance, progress=progress
        )
        summary = _summarize(details)
        job["result"] = summary
        if summary["status"] == "STEP_LOSS":
            lost = [a for a, item in summary["axes"].items() if item["status"] == "STEP_LOSS"]
            detail = f"Проверка шагов: STEP_LOSS на {', '.join(lost)} · требуется Home"
        else:
            detail = "Проверка шагов: OK · reference подтверждён"
        job.update(state="completed", detail=detail)
        await ctx.state.transition(State.CONNECTED_IDLE, detail=detail)
        audit("step_check", axes=targets, tolerance_steps=tolerance, result=summary["status"])
    except (ValueError, MotionValidationError, StateError) as exc:
        if ctx.state.state == State.HOMING:
            await ctx.state.transition(State.CONNECTED_IDLE, detail=f"Проверка шагов остановлена: {exc}")
        job.update(state="failed", detail=f"Проверка шагов: {exc}", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        ctx.motion.invalidate(targets)
        if ctx.state.state != State.DISCONNECTED:
            await ctx.state.transition(State.ERRORED, detail="step check failed; position unverified")
        job.update(state="failed", detail=f"Проверка шагов: ошибка: {exc}", error=str(exc))


@router.post("/start")
@_wrap_errors
async def start(body: StepCheckStartRequest, ctx: AppContext = Depends(get_ctx)):
    ctx.state.require(State.CONNECTED_IDLE)
    targets = _targets(ctx, body.axes)
    previous = ctx.sender.step_check_job
    if previous and previous.get("state") in {"starting", "running"}:
        raise StateError("проверка шагов уже выполняется")

    job_id = secrets.token_urlsafe(12)
    job: dict[str, Any] = {
        "job_id": job_id,
        "axes": targets,
        "tolerance_steps": body.tolerance_steps,
        "state": "starting",
        "detail": f"Проверка шагов: запуск {targets}",
        "error": None,
        "result": None,
        "task": None,
    }
    ctx.sender.step_check_job = job
    await ctx.state.transition(State.HOMING, detail=job["detail"])
    job["state"] = "running"
    task = asyncio.create_task(
        _run_job(ctx, targets, body.tolerance_steps, job), name=f"step-check-{job_id}"
    )
    job["task"] = task
    return {
        "ok": True,
        "job_id": job_id,
        "axes": targets,
        "state": job["state"],
        "detail": job["detail"],
    }


@router.get("/status")
@_wrap_errors
async def status(job_id: str, ctx: AppContext = Depends(get_ctx)):
    job = ctx.sender.step_check_job
    if not job or not secrets.compare_digest(job.get("job_id", ""), job_id):
        raise StateError("сеанс проверки шагов не найден или относится к старому подключению")
    return {
        "ok": True,
        "job_id": job["job_id"],
        "axes": job["axes"],
        "tolerance_steps": job["tolerance_steps"],
        "state": job["state"],
        "detail": job.get("detail"),
        "error": job.get("error"),
        "result": job.get("result"),
    }


@router.post("/cancel")
@_wrap_errors
async def cancel(ctx: AppContext = Depends(get_ctx)):
    ctx.state.require(State.HOMING)
    job = ctx.sender.step_check_job
    if not job or job.get("state") not in {"starting", "running"}:
        raise StateError("проверка шагов уже завершена")
    accepted = await ctx.sender.cancel_home()
    if not accepted:
        raise StateError("проверка шагов уже завершена")
    job["detail"] = "Проверка шагов: остановка запрошена"
    return {"ok": True, "detail": job["detail"]}


@router.post("/run")
async def legacy_run():
    from fastapi import HTTPException
    raise HTTPException(status_code=410, detail="use /api/stepskip/start for physical reference check")


@router.get("/results")
async def legacy_results():
    from fastapi import HTTPException
    raise HTTPException(status_code=410, detail="use /api/stepskip/status?job_id=...")
