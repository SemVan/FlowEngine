"""собираем данные наладки без команд движения"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from flowengine.api.deps import AppContext, get_ctx
from flowengine.errors import FlowEngineError
from flowengine.state import State

router = APIRouter(prefix="/api/commissioning", tags=["commissioning"])
Context = Annotated[AppContext, Depends(get_ctx)]


def snapshot(ctx: AppContext) -> dict:
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "profile": ctx.profile_name,
        "instrument": ctx.device_map.instrument_id,
        "transport": ctx.transport.name,
        "port": getattr(ctx.transport, "port", None),
        "baud": getattr(ctx.transport, "baud", None),
        "state": ctx.state.state.value,
        "detail": ctx.state.detail,
        "motion_enabled": ctx.runtime.motion.enabled,
        "positions": ctx.motion.positions,
        "referenced": ctx.motion.homed,
        "reference_counts": ctx.sender.reference_counts,
        "axes": [
            {
                "axis": a.marlin_axis, "name": a.name, "units": a.units,
                "steps_per_unit": a.steps_per_unit,
                "feedrate_default": a.feedrate_default, "feedrate_max": a.feedrate_max,
                "calibration": a.axis_calibration.model_dump(mode="json") if a.axis_calibration else None,
            }
            for a in ctx.device_map.axes
        ],
        "valves": ctx.sender.valves.snapshot(),
        "pumps": [p.model_dump(mode="json") for p in ctx.device_map.pumps],
        "procedure": ctx.runner.status,
    }


@router.get("/snapshot")
async def cached_snapshot(ctx: Context):
    return snapshot(ctx)


def _require_idle(ctx: AppContext) -> None:
    if (ctx.state.state != State.CONNECTED_IDLE or ctx.queue.depth
            or ctx.runner.status["running"] or ctx.sender.manual_session
            or ctx.sender.valves.lock.locked()):
        raise HTTPException(409, "Оборудование занято или не готово. Завершите текущую операцию перед чтением платы.")


@router.post("/diagnostics")
async def read_board(ctx: Context):
    return await _read_commands(ctx, ("M115", "M503", "M119", "M114", "M211"))


@router.post("/endstops")
async def read_inputs(ctx: Context):
    return await _read_commands(ctx, ("M119",))


async def _read_commands(ctx: AppContext, commands: tuple[str, ...]) -> dict:
    _require_idle(ctx)
    sender = ctx.sender
    device = ctx.device_map
    initial_context = snapshot(ctx)
    readings: dict[str, object] = {}
    errors: list[str] = []
    started = datetime.now(timezone.utc).isoformat()
    for command in commands:
        try:
            _require_idle(ctx)
            if ctx.sender is not sender or ctx.device_map is not device:
                raise ValueError("Профиль или подключение изменились во время чтения; повторите сбор данных.")
            if command == "M115":
                raw, caps = await sender.read_firmware()
                readings[command] = {"raw": raw, "features": sorted(caps)}
            else:
                result = await sender.read_diagnostic(command)
                readings[command] = {"raw": result.raw, "positions": result.position,
                                     "endstops": result.endstops}
        except (FlowEngineError, ValueError, HTTPException) as exc:
            errors.append(f"{command}: {exc.detail if isinstance(exc, HTTPException) else exc}")
            break
    changed = ctx.sender is not sender or ctx.device_map is not device
    if changed and not errors:
        errors.append("Профиль или подключение изменились во время чтения; повторите сбор данных.")
    return {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "complete": not errors,
        "readings": readings,
        "errors": errors,
        "context": initial_context if changed else snapshot(ctx),
    }
