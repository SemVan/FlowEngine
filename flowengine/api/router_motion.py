"""Motion endpoints: jog, move, home, stop.

Idempotency: every mutating motion endpoint accepts an `Idempotency-Key` header.
Repeating the same key returns the cached result instead of moving twice. This
guards against UI retries during transient network blips.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from functools import wraps
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from flowengine.api.deps import AppContext, get_ctx
from flowengine.errors import ControllerError, MotionValidationError, StateError, TransportError
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
        except MotionValidationError as e:
            raise HTTPException(status_code=400, detail=f"motion: {e}") from e
        except StateError as e:
            raise HTTPException(status_code=409, detail=f"state: {e}") from e
        except ControllerError as e:
            raise HTTPException(status_code=502, detail=f"controller: {e}") from e
        except TransportError as e:
            raise HTTPException(status_code=503, detail=f"transport: {e}") from e
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    return inner


def _require_motion_enabled(ctx: AppContext) -> None:
    if ctx.runner.status["running"]:
        raise StateError("procedure is running; manual commands are blocked")
    if ctx.transport.name != "mock" and not ctx.runtime.motion.enabled:
        raise StateError(
            "motion interlock is disabled; calibrate X/Y and verify the hardware, "
            "then set runtime.motion.enabled=true"
        )


def _require_home_allowed(ctx: AppContext, axes: list[str] | None) -> list[str]:
    """разрешаем Home калиброванных X/Y без включения обычного движения"""
    targets = axes if axes is not None else [a.marlin_axis for a in ctx.device_map.axes if a.kind != "peristaltic_pump"]
    if not targets:
        raise ValueError("select at least one axis to home")
    needs_normal_interlock = False
    for axis in targets:
        cfg = ctx.device_map.axis(axis)
        physical_xy = (
            axis in {"X", "Y"}
            and cfg.axis_calibration is not None
            and cfg.endstop_channels_shared
        )
        if not physical_xy:
            needs_normal_interlock = True
    if needs_normal_interlock:
        _require_motion_enabled(ctx)
    elif ctx.runner.status["running"]:
        raise StateError("procedure is running; Home is blocked")
    return targets


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
    ctx.motion.plan_relative(body.axis, body.delta, body.feedrate)
    await ctx.state.transition(State.MOVING, detail=f"jog {body.axis} {body.delta:+.3f}")
    try:
        await ctx.sender.jog(body.axis, body.delta, body.feedrate)
        positions = await ctx.sender.wait_idle()
    except Exception:
        ctx.motion.invalidate([body.axis])
        await ctx.state.transition(State.ERRORED, detail="jog failed; position unverified")
        raise
    else:
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
    ctx.motion.plan_absolute(body.axis, body.target, body.feedrate)
    await ctx.state.transition(State.MOVING, detail=f"move {body.axis}→{body.target:.3f}")
    try:
        await ctx.sender.move_to(body.axis, body.target, body.feedrate)
        positions = await ctx.sender.wait_idle()
    except Exception:
        ctx.motion.invalidate([body.axis])
        await ctx.state.transition(State.ERRORED, detail="move failed; position unverified")
        raise
    else:
        await ctx.state.transition(State.CONNECTED_IDLE, detail="move complete")
    audit("move", axis=body.axis, target=body.target, feedrate=body.feedrate)
    result = {"ok": True, "positions": positions}
    _idem_put(idempotency_key, result)
    return result


def _home_result(ctx: AppContext, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "homed": ctx.motion.homed,
        "positions": ctx.motion.positions,
        "reference_counts": ctx.sender.reference_counts,
        "reference_sides": ctx.sender.reference_sides,
        "details": details,
    }


async def _execute_home(
    ctx: AppContext,
    targets: list[str],
    *,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    await ctx.state.transition(State.HOMING, detail=f"Home: подготовка {targets}")
    if job is not None:
        job.update(state="running", detail=f"Home: подготовка {targets}")

    async def progress(detail: str) -> None:
        if job is not None:
            job["detail"] = detail
        if ctx.state.state == State.HOMING:
            await ctx.state.transition(State.HOMING, detail=detail)

    try:
        details = await ctx.sender.home(targets, progress=progress)
    except (ValueError, MotionValidationError, StateError) as exc:
        ctx.motion.invalidate(targets)
        if ctx.state.state == State.HOMING:
            await ctx.state.transition(State.CONNECTED_IDLE, detail=f"Home заблокирован: {exc}")
        if job is not None:
            job.update(state="failed", detail=f"Home заблокирован: {exc}", error=str(exc))
        raise
    except Exception as exc:
        ctx.motion.invalidate(targets)
        if ctx.state.state != State.DISCONNECTED:
            await ctx.state.transition(State.ERRORED, detail="home failed; position unverified")
        if job is not None:
            job.update(state="failed", detail=f"Home: ошибка: {exc}", error=str(exc))
        raise
    else:
        result = _home_result(ctx, details)
        await ctx.state.transition(State.CONNECTED_IDLE, detail="Home завершён; reference установлен")
        if job is not None:
            job.update(state="completed", detail="Home завершён; reference установлен", result=result)
        audit("home", axes=targets)
        return result


@router.post("/home")
@_wrap_errors
async def home(body: HomeRequest, ctx: AppContext = Depends(get_ctx)):
    """старый синхронный Home; интерфейс использует запуск и опрос статуса"""
    targets = _require_home_allowed(ctx, body.axes)
    ctx.state.require(State.CONNECTED_IDLE)
    return await _execute_home(ctx, targets)


async def _home_job_runner(ctx: AppContext, targets: list[str], job: dict[str, Any]) -> None:
    try:
        await _execute_home(ctx, targets, job=job)
    except Exception as exc:  # noqa: BLE001
        # ошибку уже записали в статус; здесь только забираем её из задачи
        if job.get("state") not in {"failed", "cancelled"}:
            job.update(state="failed", detail=f"Home: ошибка: {exc}", error=str(exc))


@router.post("/home/start")
@_wrap_errors
async def start_home(body: HomeRequest, ctx: AppContext = Depends(get_ctx)):
    """запускаем Home в фоне; доступны статус и отмена"""
    targets = _require_home_allowed(ctx, body.axes)
    ctx.state.require(State.CONNECTED_IDLE)
    previous = getattr(ctx.sender, "home_job", None)
    if previous and previous.get("state") in {"starting", "running"}:
        raise StateError("Home уже выполняется")

    job_id = secrets.token_urlsafe(12)
    job: dict[str, Any] = {
        "job_id": job_id,
        "axes": targets,
        "state": "starting",
        "detail": f"Home: запуск {targets}",
        "error": None,
        "result": None,
        "task": None,
    }
    ctx.sender.home_job = job
    await ctx.state.transition(State.HOMING, detail=job["detail"])
    task = asyncio.create_task(_home_job_runner(ctx, targets, job), name=f"home-{job_id}")
    job["task"] = task
    return {"ok": True, "job_id": job_id, "axes": targets, "state": job["state"], "detail": job["detail"]}


@router.get("/home/status")
@_wrap_errors
async def home_status(job_id: str, ctx: AppContext = Depends(get_ctx)):
    job = getattr(ctx.sender, "home_job", None)
    if not job or not secrets.compare_digest(job.get("job_id", ""), job_id):
        raise StateError("Home-сеанс не найден или уже относится к старому подключению")
    return {
        "ok": True,
        "job_id": job["job_id"],
        "axes": job["axes"],
        "state": job["state"],
        "detail": job.get("detail"),
        "error": job.get("error"),
        "result": job.get("result"),
    }


@router.post("/home/cancel")
@_wrap_errors
async def cancel_home(ctx: AppContext = Depends(get_ctx)):
    ctx.state.require(State.HOMING)
    accepted = await ctx.sender.cancel_home()
    if not accepted:
        raise StateError("Home уже завершён")
    job = getattr(ctx.sender, "home_job", None)
    if job and job.get("state") in {"starting", "running"}:
        job["detail"] = "Home: остановка запрошена"
    return {"ok": True, "detail": "Остановка Home запрошена"}


@router.post("/stop")
@_wrap_errors
async def stop(ctx: AppContext = Depends(get_ctx)):
    # сначала отправляем Stop, потом ждём отмены задач
    try:
        await ctx.sender.abort()
    finally:
        await ctx.runner.abort()
        await ctx.state.transition(
            State.ERRORED,
            detail="stop requested; physical stop unverified; reconnect after motion stops; references lost",
        )
    audit("stop")
    return {"ok": True}
