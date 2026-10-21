"""калибруем X/Y по общему входу концевиков; сторону задаём по направлению"""
# ruff: noqa: RUF001
from __future__ import annotations

import asyncio
import copy
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from flowengine.api.deps import AppContext, get_ctx
from flowengine.api.router_motion import _wrap_errors
from flowengine.config_store import find_saved, list_saved, load_yaml, save_yaml
from flowengine.errors import ControllerError, StateError, TransportError
from flowengine.hardware.endstop_seek import seek_switch
from flowengine.hardware.homing import EndstopHoming
from flowengine.schemas import ConfigurationProfile
from flowengine.schemas.device import AxisCalibration
from flowengine.state import State

router = APIRouter(prefix="/api/axis-calibration", tags=["axis calibration"])
Context = Annotated[AppContext, Depends(get_ctx)]
POLL_SECONDS = 0.1
LEASE_SECONDS = 0.75
MAX_HOLD_SECONDS = 300.0
ENDSTOP_POLL_SECONDS = 0.04


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Begin(Request):
    axis: Literal["X", "Y"]
    free_space_confirmed: Literal[True]
    seek_feedrate: float | None = Field(default=None, ge=30, le=1200)
    latch_feedrate: float | None = Field(default=None, ge=15, le=600)
    backoff_mm: float = Field(default=2.0, ge=0.25, le=10.0)


class SwitchAxis(Request):
    token: str
    axis: Literal["X", "Y"]
    free_space_confirmed: Literal[True]
    seek_feedrate: float | None = Field(default=None, ge=30, le=1200)
    latch_feedrate: float | None = Field(default=None, ge=15, le=600)
    backoff_mm: float = Field(default=2.0, ge=0.25, le=10.0)


class Token(Request):
    token: str


class RunRequest(Token):
    run_id: str = Field(min_length=1, max_length=80)


class Start(RunRequest):
    direction: Literal[-1, 1]
    # оставлено для старого кеша браузера
    feedrate: float | None = Field(default=None, ge=30, le=1200)


class Renew(RunRequest):
    sequence: int = Field(gt=0)


class Record(Token):
    edge: Literal["motor", "far"]
    observed_contact_confirmed: Literal[True]


class Save(Token):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    observed_direction_confirmed: Literal[True]
    home_side: Literal["motor", "far"] = "motor"


class AutoStart(Request):
    axes: list[Literal["X", "Y"]] = Field(min_length=1, max_length=2)


@dataclass
class Hold:
    run_id: str
    direction: int
    feedrate: float
    latch_feedrate: float
    backoff_mm: float
    lease: float
    started: float
    sequence: int = 0
    state: str = "starting"
    phase: str = "seek"
    stop_reason: str | None = None
    elapsed_s: float = 0.0
    polls: int = 0
    endstop_state: bool | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def public(self):
        return {key: getattr(self, key) for key in
                ("run_id", "direction", "feedrate", "latch_feedrate", "backoff_mm",
                 "state", "phase", "stop_reason", "elapsed_s", "polls", "endstop_state")}


def commissioning_idle(ctx: AppContext) -> None:
    if ctx.runner.status["running"]:
        raise StateError("Выполняется процедура; калибровка X/Y заблокирована")
    ctx.state.require(State.CONNECTED_IDLE)
    if ctx.queue.depth:
        raise ValueError("Дождитесь завершения очереди")


def operation(fn):  # type: ignore[no-untyped-def]
    @wraps(fn)
    async def guarded(*args, **kwargs):  # type: ignore[no-untyped-def]
        ctx = kwargs["ctx"]
        if ctx.sender.manual_lock.locked():
            raise StateError("Команда калибровки уже выполняется; повтор не ставится в очередь")
        async with ctx.sender.manual_lock:
            return await fn(*args, **kwargs)

    return _wrap_errors(guarded)


def get_session(ctx: AppContext, token: str):
    s = ctx.sender.manual_session
    if (not s or s.get("origin_mode") != "axis_calibration"
            or not secrets.compare_digest(s["token"], token)
            or s["_epoch"] != ctx.sender.reference_epoch):
        raise StateError("Калибровка завершена. " + ctx.state.detail)
    return s


def active(ctx: AppContext, s) -> None:
    if ctx.sender.manual_session is not s or s["_epoch"] != ctx.sender.reference_epoch:
        raise StateError("Калибровка прервана кнопкой СТОП")
    ctx.state.require(State.MOVING)


def _candidate_calibration(s, cfg):
    if not all(s["contacts"].values()) or s["positive_toward_motor"] is None:
        return None
    motor = s["contacts"]["motor"]
    far = s["contacts"]["far"]
    travel = abs(motor["position"] - far["position"])
    if motor.get("step_count") is not None and far.get("step_count") is not None:
        travel_steps = abs(motor["step_count"] - far["step_count"])
        measurement_source = "marlin_count"
    else:
        travel_steps = travel * cfg.steps_per_unit
        measurement_source = "m114_x_steps_per_unit"
    return AxisCalibration(
        channel=f"{s['axis'].lower()}_min",
        motor=motor,
        far=far,
        positive_toward_motor=s["positive_toward_motor"],
        travel=travel,
        travel_steps=travel_steps,
        seek_feedrate=s["seek_feedrate"],
        latch_feedrate=s["latch_feedrate"],
        backoff_mm=s["backoff_mm"],
        home_side=s.get("home_side", "motor"),
        recorded_at=datetime.now(UTC).isoformat(),
        measurement_source=measurement_source,
    )


def public(s):
    result = {k: v for k, v in s.items() if not k.startswith("_")}
    run = s.get("_run")
    result["run"] = run.public() if run else None
    contact = s.get("_last_contact")
    result["blocked_direction"] = contact["approach_direction"] if s["pressed"] and contact else None
    cfg = s.get("_axis_config")
    candidate = _candidate_calibration(s, cfg) if cfg is not None else None
    result["measured_travel"] = candidate.travel if candidate else None
    result["measured_travel_steps"] = candidate.travel_steps if candidate else None
    result["measurement_source"] = candidate.measurement_source if candidate else None
    result["seek_feedrate"] = s.get("seek_feedrate")
    result["latch_feedrate"] = s.get("latch_feedrate")
    result["backoff_mm"] = s.get("backoff_mm")
    result["last_contact"] = copy.deepcopy(s.get("_last_contact")) if s.get("_last_contact_recordable") else None
    result["last_contact_edge"] = s.get("_last_contact_edge") if s.get("_last_contact_recordable") else None
    return result


def reason(run: Hold):
    if run.stop.is_set():
        return run.stop_reason or "released"
    now = asyncio.get_running_loop().time()
    if now >= run.lease:
        return "lease_expired"
    if now - run.started >= MAX_HOLD_SECONDS:
        return "hold_timeout"
    return None


async def command(ctx: AppContext, line: str):
    return await ctx.queue.send(line, timeout=ctx.runtime.timeouts.diagnostics)


async def read_switch(ctx: AppContext, axis: str) -> bool:
    result = await command(ctx, "M119")
    channel = f"{axis.lower()}_min"
    if channel not in (result.endstops or {}):
        raise ValueError(f"Нет ответа входа {channel}; движение прекращено")
    return result.endstops[channel]


def _step_count(result, axis: str) -> int | None:
    for line in result.raw:
        if "Count " not in line:
            continue
        found = re.search(rf"\b{re.escape(axis)}:(-?\d+)\b", line.split("Count ", 1)[1])
        if found:
            return int(found.group(1))
    return None


async def snapshot(ctx: AppContext, axis: str):
    result = await command(ctx, "M114")
    if axis not in (result.position or {}):
        raise ValueError("Плата не сообщила координату оси")
    ctx.motion.update_position(result.position)
    return result.position[axis], await read_switch(ctx, axis), _step_count(result, axis)


async def fail(ctx: AppContext, s, run: Hold, error: Exception | str) -> None:
    run.state, run.stop_reason = "stopped", "error"
    s["error"] = str(error)
    ctx.motion.invalidate()
    if s["_epoch"] != ctx.sender.reference_epoch or ctx.sender.manual_session is not s:
        return
    try:
        await ctx.sender.abort()
    finally:
        if ctx.state.state != State.DISCONNECTED:
            await ctx.state.transition(State.ERRORED, detail=f"Калибровка остановлена: {error}. Переподключитесь")


async def _fixed_relative_move(
    ctx: AppContext,
    s,
    run: Hold,
    direction: int,
    distance: float,
    feedrate: float,
) -> bool:
    position, _, _ = await snapshot(ctx, s["axis"])
    target = position + direction * distance
    await command(ctx, "G90")
    await command(ctx, f"G1 {s['axis']}{target:.4f} F{feedrate:.2f}")
    await ctx.queue.send("M400", timeout=min(5.0, ctx.runtime.timeouts.move))
    return await read_switch(ctx, s["axis"])


def _remaining_hold_seconds(run: Hold) -> float:
    elapsed = asyncio.get_running_loop().time() - run.started
    return max(0.25, MAX_HOLD_SECONDS - elapsed)


async def _continuous_phase(
    ctx: AppContext,
    s,
    run: Hold,
    *,
    direction: int,
    feedrate: float,
    target_pressed: bool,
):
    active(ctx, s)
    run.state = "moving"

    async def on_poll(elapsed: float, pressed: bool) -> None:
        run.elapsed_s = asyncio.get_running_loop().time() - run.started
        run.polls += 1
        run.endstop_state = pressed
        s["pressed"] = pressed

    outcome = await seek_switch(
        ctx.queue,
        axis=s["axis"],
        channel=f"{s['axis'].lower()}_min",
        direction=direction,
        feedrate=feedrate,
        target_pressed=target_pressed,
        timeout_s=_remaining_hold_seconds(run),
        steps_per_unit=s["_axis_config"].steps_per_unit,
        diagnostics_timeout=ctx.runtime.timeouts.diagnostics,
        move_timeout=min(5.0, ctx.runtime.timeouts.move),
        poll_interval_s=ENDSTOP_POLL_SECONDS,
        stop_reason=lambda: reason(run),
        on_poll=on_poll,
    )
    if outcome.stop_reason == "watchdog_timeout":
        run.stop_reason = "hold_timeout"
    elif outcome.stop_reason:
        run.stop_reason = outcome.stop_reason
    run.elapsed_s = asyncio.get_running_loop().time() - run.started
    return outcome


async def worker(ctx: AppContext, s, run: Hold) -> None:
    initial_pressed = False
    stop_reason = "released"
    soft_endstops_disabled = False
    try:
        async with ctx.sender.manual_lock:
            active(ctx, s)
            _, initial_pressed, _ = await snapshot(ctx, s["axis"])
            s["pressed"] = initial_pressed
            blocked_at_endstop = False

            if initial_pressed:
                contact = s["_last_contact"]
                if not contact:
                    stop_reason = "endstop_side_unknown"
                    blocked_at_endstop = True
                elif contact["approach_direction"] == run.direction:
                    stop_reason = "blocked_endstop"
                    blocked_at_endstop = True
                else:
                    run.phase = "release"
            elif s["_last_contact"]:
                s["_last_contact"] = None
                s["_last_contact_edge"] = None
                s["_last_contact_recordable"] = False

            if not reason(run) and not blocked_at_endstop:
                # на время поиска снимаем M211: границы определяем по микрикам
                await command(ctx, "M211 S0")
                soft_endstops_disabled = True
                active(ctx, s)

                if initial_pressed:
                    outcome = await _continuous_phase(
                        ctx, s, run,
                        direction=run.direction,
                        feedrate=min(run.latch_feedrate, run.feedrate),
                        target_pressed=False,
                    )
                    stop_reason = run.stop_reason or stop_reason
                    if outcome.reached:
                        initial_pressed = False
                        s["pressed"] = False
                        s["_last_contact"] = None
                        s["_last_contact_edge"] = None
                        s["_last_contact_recordable"] = False

                if not initial_pressed and not reason(run):
                    run.phase = "seek"
                    outcome = await _continuous_phase(
                        ctx, s, run,
                        direction=run.direction,
                        feedrate=run.feedrate,
                        target_pressed=True,
                    )
                    stop_reason = run.stop_reason or stop_reason
                    pressed = outcome.reached

                    if pressed and not reason(run):
                        # после первого касания отходим и медленно касаемся ещё раз
                        run.phase = "backoff"
                        release = await _continuous_phase(
                            ctx, s, run,
                            direction=-run.direction,
                            feedrate=min(run.latch_feedrate, run.feedrate),
                            target_pressed=False,
                        )
                        stop_reason = run.stop_reason or stop_reason
                        if release.reached and not reason(run):
                            pressed = await _fixed_relative_move(
                                ctx, s, run,
                                -run.direction,
                                run.backoff_mm,
                                min(run.feedrate, 600.0),
                            )
                            s["pressed"] = pressed
                            if pressed:
                                raise ValueError("Концевик снова сработал во время отхода")

                        if release.reached and not pressed and not reason(run):
                            run.phase = "latch"
                            latch = await _continuous_phase(
                                ctx, s, run,
                                direction=run.direction,
                                feedrate=run.latch_feedrate,
                                target_pressed=True,
                            )
                            if latch.reached:
                                stop_reason = "endstop"
                            else:
                                stop_reason = run.stop_reason or stop_reason

            run.state = "stopping"
            active(ctx, s)
            if soft_endstops_disabled:
                await command(ctx, "M211 S1")
                soft_endstops_disabled = False

            position, pressed, count = await snapshot(ctx, s["axis"])
            active(ctx, s)
            s.update(position=position, pressed=pressed)
            if stop_reason == "endstop" and pressed:
                # после M410 берём Count: цель из M114 могла устареть
                contact_position = (
                    count / s["_axis_config"].steps_per_unit if count is not None else position
                )
                s["_last_contact"] = {
                    "position": contact_position,
                    "approach_direction": run.direction,
                    "step_count": count,
                }
                s["_last_contact_edge"] = None
                s["_last_contact_recordable"] = True
            elif not pressed:
                s["_last_contact"] = None
                s["_last_contact_edge"] = None
                s["_last_contact_recordable"] = False

            ctx.motion.invalidate()
            run.state, run.stop_reason = "stopped", stop_reason
            s["_retired"].add(run.run_id)
            await ctx.state.transition(
                State.CONNECTED_IDLE,
                detail="Калибровочное движение остановлено; координатная привязка не установлена",
            )
    except asyncio.CancelledError:
        await fail(ctx, s, run, "Сеанс калибровки закрыт")
        raise
    except Exception as exc:  # noqa: BLE001
        if soft_endstops_disabled:
            try:
                await command(ctx, "M211 S1")
            except Exception:
                pass
        await fail(ctx, s, run, exc)


async def shutdown(ctx: AppContext) -> None:
    s = ctx.sender.manual_session
    if s and s.get("origin_mode") == "axis_calibration":
        run = s.get("_run")
        if run and run.task and not run.task.done():
            run.stop_reason = "released"
            run.stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(run.task), timeout=2)
            except TimeoutError:
                await ctx.sender.abort()
                run.task.cancel()
                await asyncio.gather(run.task, return_exceptions=True)

    job = getattr(ctx.sender, "auto_calibration_job", None)
    if job and job.get("state") in {"starting", "running"}:
        job["cancel"].set()
        ctx.sender._home_cancel.set()
        task = job.get("task")
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3)
            except TimeoutError:
                await ctx.sender.abort()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@router.get("/readiness")
@_wrap_errors
async def readiness(ctx: Context):
    axes = {}
    device_map = ctx.device_map
    if ctx.profile_name != "default":
        try:
            device_map = load_yaml(
                find_saved("profiles", ctx.profile_name), ConfigurationProfile
            ).device_map
        except Exception:
            device_map = ctx.device_map
    for axis_name in ("X", "Y"):
        try:
            cfg = device_map.axis(axis_name)
        except KeyError:
            continue
        calibration = cfg.axis_calibration
        axes[axis_name] = {
            "calibrated": calibration is not None,
            "direction_verified": calibration is not None,
            "positive_toward_motor": calibration.positive_toward_motor if calibration else None,
            "endpoints_verified": calibration is not None,
            "travel": calibration.travel if calibration else None,
            "travel_steps": calibration.travel_steps if calibration else None,
            "measurement_source": calibration.measurement_source if calibration else None,
            "feedrate_default": cfg.feedrate_default,
            "feedrate_max": cfg.feedrate_max,
            "seek_feedrate": calibration.seek_feedrate if calibration else cfg.feedrate_default,
            "latch_feedrate": calibration.latch_feedrate if calibration else min(120.0, cfg.feedrate_default),
            "backoff_mm": calibration.backoff_mm if calibration else 2.0,
            "home_side": calibration.home_side if calibration else "motor",
        }
    return {
        "motion_enabled": ctx.runtime.motion.enabled or ctx.transport.name == "mock",
        "commissioning_bypasses_motion_interlock": True,
        "profile": ctx.profile_name,
        "axes": axes,
    }



def _draft_from_session(s) -> dict:
    return {
        "axis": s["axis"],
        "position": s["position"],
        "pressed": s["pressed"],
        "positive_toward_motor": s["positive_toward_motor"],
        "saved_calibration": copy.deepcopy(s["saved_calibration"]),
        "contacts": copy.deepcopy(s["contacts"]),
        "seek_feedrate": s["seek_feedrate"],
        "latch_feedrate": s["latch_feedrate"],
        "backoff_mm": s["backoff_mm"],
        "home_side": s.get("home_side", "motor"),
        "error": s.get("error"),
        "_last_contact": copy.deepcopy(s.get("_last_contact")),
        "_last_contact_edge": s.get("_last_contact_edge"),
        "_last_contact_recordable": bool(s.get("_last_contact_recordable")),
        "_axis_config": s["_axis_config"],
    }


def _apply_draft(s, draft: dict, *, position: float, pressed: bool) -> None:
    for key, value in draft.items():
        if key in {"position", "pressed"}:
            continue
        s[key] = copy.deepcopy(value)
    s["position"] = position
    s["pressed"] = pressed
    s["_run"] = None
    s["_retired"] = set()
    s["error"] = None


def _fresh_axis_draft(ctx: AppContext, axis: str, seek: float, latch: float, backoff: float) -> dict:
    cfg = ctx.device_map.axis(axis)
    saved = cfg.axis_calibration.model_dump() if cfg.axis_calibration else None
    return {
        "axis": axis,
        "position": 0.0,
        "pressed": False,
        "positive_toward_motor": saved["positive_toward_motor"] if saved else None,
        "saved_calibration": saved,
        "contacts": {"motor": None, "far": None},
        "seek_feedrate": seek,
        "latch_feedrate": latch,
        "backoff_mm": backoff,
        "home_side": saved["home_side"] if saved else "motor",
        "error": None,
        "_last_contact": None,
        "_last_contact_edge": None,
        "_last_contact_recordable": False,
        "_axis_config": cfg,
    }


@router.post("/begin")
@operation
async def begin(body: Begin, ctx: Context):
    commissioning_idle(ctx)
    cfg = ctx.device_map.axis(body.axis)
    if cfg.kind != "syringe_pump" or cfg.units != "mm" or not cfg.endstop_channels_shared:
        raise ValueError("Калибровка предназначена для X/Y с двумя параллельными физическими концевиками")
    if any(body.axis in g.axes and len(g.axes) > 1 for g in ctx.device_map.endstop_inputs):
        raise ValueError("Один вход разделяют разные оси; этот мастер рассчитан на отдельный вход каждой X/Y")
    requested_seek = body.seek_feedrate or cfg.feedrate_default
    requested_latch = body.latch_feedrate or min(120.0, requested_seek)
    if requested_latch > requested_seek:
        raise ValueError("Скорость точного подхода не должна превышать скорость быстрого поиска")
    seek_feedrate = min(requested_seek, ctx.motion.max_feedrate(body.axis), 1200)
    latch_feedrate = min(requested_latch, seek_feedrate)
    epoch = ctx.sender.reference_epoch
    await ctx.state.transition(State.MOVING, detail="Диагностика X/Y перед калибровкой")
    ctx.sender.manual_session = None
    ctx.motion.invalidate()
    try:
        position, pressed, _ = await snapshot(ctx, body.axis)
        if pressed:
            raise ValueError("Начните калибровку между концевиками, когда общий вход отпущен")
        if epoch != ctx.sender.reference_epoch:
            raise StateError("Диагностика прервана кнопкой СТОП")
        ctx.state.require(State.MOVING)
        saved = cfg.axis_calibration.model_dump() if cfg.axis_calibration else None
        if ctx.profile_name == "default":
            profile = ConfigurationProfile(
                name="manual", device_map=ctx.device_map, runtime=ctx.runtime, modes=ctx.modes
            ).model_dump()
        else:
            profile = load_yaml(
                find_saved("profiles", ctx.profile_name), ConfigurationProfile
            ).model_dump()
        s = dict(
            token=secrets.token_urlsafe(24),
            axis=body.axis,
            origin_mode="axis_calibration",
            position=position,
            pressed=False,
            positive_toward_motor=saved["positive_toward_motor"] if saved else None,
            saved_calibration=saved,
            contacts={"motor": None, "far": None},
            seek_feedrate=seek_feedrate,
            latch_feedrate=latch_feedrate,
            backoff_mm=body.backoff_mm,
            home_side=saved["home_side"] if saved else "motor",
            error=None,
            _epoch=epoch,
            _profile=profile,
            _source_profile_name=ctx.profile_name,
            _run=None,
            _retired=set(),
            _last_contact=None,
            _last_contact_edge=None,
            _last_contact_recordable=False,
            _axis_config=cfg,
            _drafts={},
        )
        s["_drafts"][body.axis] = _draft_from_session(s)
        ctx.sender.manual_session = s
    except (ControllerError, TransportError) as exc:
        if epoch == ctx.sender.reference_epoch:
            await ctx.sender.abort()
            await ctx.state.transition(State.ERRORED, detail=f"Нет надёжного ответа платы: {exc}; переподключитесь")
        raise
    finally:
        if epoch == ctx.sender.reference_epoch and ctx.state.state == State.MOVING:
            await ctx.state.transition(State.CONNECTED_IDLE, detail="Калибровка готова; движение только при удержании")
    return public(s)


@router.post("/switch")
@operation
async def switch_axis(body: SwitchAxis, ctx: Context):
    commissioning_idle(ctx)
    s = get_session(ctx, body.token)
    run = s.get("_run")
    if run and run.state != "stopped":
        raise StateError("Сначала остановите движение, затем переключайте X/Y")
    if body.axis == s["axis"]:
        return public(s)

    s["_drafts"][s["axis"]] = _draft_from_session(s)
    cfg = ctx.device_map.axis(body.axis)
    if cfg.kind != "syringe_pump" or cfg.units != "mm" or not cfg.endstop_channels_shared:
        raise ValueError("Калибровка предназначена для X/Y с параллельными физическими концевиками")
    requested_seek = body.seek_feedrate or cfg.feedrate_default
    requested_latch = body.latch_feedrate or min(120.0, requested_seek)
    if requested_latch > requested_seek:
        raise ValueError("Скорость точного подхода не должна превышать скорость быстрого поиска")
    seek = min(requested_seek, ctx.motion.max_feedrate(body.axis), 1200)
    latch = min(requested_latch, seek)

    await ctx.state.transition(State.MOVING, detail=f"Проверяем ось {body.axis} перед переключением")
    try:
        position, pressed, _ = await snapshot(ctx, body.axis)
        draft = s["_drafts"].get(body.axis)
        if draft is None:
            if pressed:
                raise ValueError(
                    f"{body.axis}: общий концевик уже нажат, а сторона ещё неизвестна. "
                    "Освободите ось перед первым переключением на неё."
                )
            draft = _fresh_axis_draft(ctx, body.axis, seek, latch, body.backoff_mm)
        else:
            draft["seek_feedrate"] = seek
            draft["latch_feedrate"] = latch
            draft["backoff_mm"] = body.backoff_mm
        _apply_draft(s, draft, position=position, pressed=pressed)
        s["_drafts"][body.axis] = _draft_from_session(s)
        ctx.motion.invalidate()
    finally:
        if ctx.state.state == State.MOVING:
            await ctx.state.transition(State.CONNECTED_IDLE, detail=f"Калибровка переключена на {body.axis}")
    return public(s)


@router.get("/state")
@_wrap_errors
async def state(token: str, ctx: Context):
    return public(get_session(ctx, token))


@router.post("/start")
@_wrap_errors
async def start(body: Start, ctx: Context):
    s = get_session(ctx, body.token)
    if body.run_id in s["_retired"]:
        raise StateError("Это удержание уже отпущено; новый запуск требует нового нажатия")
    old = s["_run"]
    if old and old.run_id == body.run_id:
        if old.direction != body.direction:
            raise ValueError("Идентификатор удержания относится к другой команде")
        return public(s)
    if ctx.sender.manual_lock.locked():
        raise StateError("Дождитесь завершения текущей команды калибровки")
    commissioning_idle(ctx)
    if old and old.state != "stopped":
        raise StateError("Дождитесь остановки предыдущего движения")
    if s.get("_last_contact_recordable") and s.get("_last_contact") and s.get("_last_contact_edge") is None:
        raise StateError("Сначала назовите только что найденный физический край: «у мотора» или «от мотора»")
    speed = min(body.feedrate or s["seek_feedrate"], ctx.motion.max_feedrate(s["axis"]), 1200)
    if body.feedrate is not None:
        s["seek_feedrate"] = speed
        s["latch_feedrate"] = min(s["latch_feedrate"], speed)
    now = asyncio.get_running_loop().time()
    run = Hold(
        body.run_id,
        body.direction,
        speed,
        min(s["latch_feedrate"], speed),
        s["backoff_mm"],
        now + LEASE_SECONDS,
        now,
    )
    s["_run"], s["error"] = run, None
    await ctx.state.transition(State.MOVING, detail="Калибровка X/Y удержанием")
    run.task = asyncio.create_task(worker(ctx, s, run), name=f"axis-calibration-{s['axis']}")
    return public(s)


@router.post("/renew")
@_wrap_errors
async def renew(body: Renew, ctx: Context):
    s = get_session(ctx, body.token)
    run = s["_run"]
    if not run or run.run_id != body.run_id or run.state not in {"starting", "moving"} or reason(run):
        raise StateError("Удержание завершено; старый сигнал не возобновляет движение")
    if body.sequence > run.sequence:
        run.sequence = body.sequence
        run.lease = asyncio.get_running_loop().time() + LEASE_SECONDS
    return {"ok": True}


@router.post("/release")
@_wrap_errors
async def release(body: RunRequest, ctx: Context):
    s = get_session(ctx, body.token)
    s["_retired"].add(body.run_id)
    run = s["_run"]
    if run and run.run_id == body.run_id and run.state != "stopped":
        run.stop_reason = "released"
        run.stop.set()
    return public(s)


@router.post("/record")
@operation
async def record(body: Record, ctx: Context):
    commissioning_idle(ctx)
    s = get_session(ctx, body.token)
    contact = copy.deepcopy(s.get("_last_contact"))
    run = s.get("_run")
    if not s.get("_last_contact_recordable") or not contact or not run or run.state != "stopped" or run.stop_reason != "endstop":
        raise ValueError("Сначала физически дойдите до концевика в этом сеансе")

    # подписываем сохранённое касание, держать микрик нажатым уже не нужно
    contacts = copy.deepcopy(s["contacts"])
    other = "far" if body.edge == "motor" else "motor"
    if contacts.get(other) and contacts[other]["approach_direction"] == contact["approach_direction"]:
        raise ValueError("Этот же физический контакт уже назначен противоположному краю")
    contacts[body.edge] = contact
    positive_toward_motor = contact["approach_direction"] == (1 if body.edge == "motor" else -1)
    s.update(contacts=contacts, positive_toward_motor=positive_toward_motor)
    s["_last_contact_edge"] = body.edge
    _candidate_calibration(s, s["_axis_config"])
    return public(s)


@router.post("/save")
@operation
async def save(body: Save, ctx: Context):
    commissioning_idle(ctx)
    s = get_session(ctx, body.token)
    s["home_side"] = body.home_side
    calibration = _candidate_calibration(s, s["_axis_config"])
    if calibration is None:
        raise ValueError("Найдите и назовите оба физических края: у мотора и от мотора")
    s["_drafts"][s["axis"]] = _draft_from_session(s)
    completed: dict[str, AxisCalibration] = {}
    for axis_name, draft in s["_drafts"].items():
        candidate = _candidate_calibration(draft, draft["_axis_config"])
        if candidate is not None:
            completed[axis_name] = candidate
    existing = {p.stem for p in list_saved("profiles")}
    source_name = s.get("_source_profile_name", "default")
    if body.name == "default":
        raise ValueError("Заводской профиль не перезаписывается; выберите имя личного профиля")
    if body.name in existing and body.name != source_name:
        raise ValueError("Можно обновить только текущий активный профиль или сохранить под новым именем")

    data = copy.deepcopy(s["_profile"])
    data["name"] = body.name
    for axis in data["device_map"]["axes"]:
        calibrated = completed.get(axis["marlin_axis"])
        if calibrated is not None:
            axis["travel"] = None
            axis["axis_calibration"] = calibrated.model_dump()
    save_yaml("profiles", body.name, ConfigurationProfile.model_validate(data))
    ctx.sender.manual_session = None
    ctx.motion.invalidate()
    return {
        "ok": True,
        "name": body.name,
        "apply_required": True,
        "updated_current": body.name == source_name,
        "axis": s["axis"],
        "axes": sorted(completed),
        "travel": calibration.travel,
        "travel_steps": calibration.travel_steps,
        "measurement_source": calibration.measurement_source,
        "seek_feedrate": calibration.seek_feedrate,
        "latch_feedrate": calibration.latch_feedrate,
        "backoff_mm": calibration.backoff_mm,
        "home_side": calibration.home_side,
    }


@router.post("/cancel")
@operation
async def cancel(body: Token, ctx: Context):
    commissioning_idle(ctx)
    get_session(ctx, body.token)
    ctx.sender.manual_session = None
    ctx.motion.invalidate()
    return {"ok": True}


def _auto_public(job: dict | None) -> dict:
    if not job:
        return {"active": False, "state": "idle"}
    return {
        "active": job.get("state") in {"starting", "running"},
        "job_id": job.get("job_id"),
        "axes": job.get("axes", []),
        "state": job.get("state"),
        "axis": job.get("axis"),
        "phase": job.get("phase"),
        "detail": job.get("detail"),
        "error": job.get("error"),
        "results": copy.deepcopy(job.get("results", {})),
    }


async def _auto_progress(ctx: AppContext, job: dict, *, axis: str, phase: str, detail: str) -> None:
    job.update(axis=axis, phase=phase, detail=detail)
    if ctx.state.state == State.MOVING:
        await ctx.state.transition(State.MOVING, detail=detail)


async def _auto_fixed_move(
    ctx: AppContext, axis: str, direction: int, distance: float, feedrate: float
) -> None:
    position, _, _ = await snapshot(ctx, axis)
    target = position + direction * distance
    await command(ctx, "G90")
    await command(ctx, f"G1 {axis}{target:.4f} F{feedrate:.2f}")
    await ctx.queue.send("M400", timeout=ctx.runtime.timeouts.move)


async def _auto_clear_switch(
    ctx: AppContext,
    job: dict,
    *,
    axis: str,
    direction: int,
    latch: float,
    backoff: float,
    cfg,
) -> None:
    channel = f"{axis.lower()}_min"
    cancel = job["cancel"]

    def stopped() -> str | None:
        return "cancelled" if cancel.is_set() else None

    await _auto_progress(ctx, job, axis=axis, phase="release", detail=f"Автокалибровка {axis}: освобождаем концевик")
    released = await seek_switch(
        ctx.queue, axis=axis, channel=channel, direction=direction,
        feedrate=latch, target_pressed=False,
        timeout_s=max(5.0, 60.0 * (backoff + 2.0) / max(latch, 1e-9)),
        steps_per_unit=cfg.steps_per_unit,
        diagnostics_timeout=ctx.runtime.timeouts.diagnostics,
        move_timeout=ctx.runtime.timeouts.move,
        stop_reason=stopped,
        on_poll=lambda elapsed, sw: _auto_progress(
            ctx, job, axis=axis, phase="release",
            detail=f"Автокалибровка {axis}: освобождение · {elapsed:.1f} с · {'НАЖАТ' if sw else 'open'}",
        ),
    )
    if not released.reached:
        if released.stop_reason == "cancelled":
            raise StateError("Автокалибровка остановлена оператором")
        raise ControllerError(f"{axis}: концевик не освободился")

    await _auto_progress(ctx, job, axis=axis, phase="backoff", detail=f"Автокалибровка {axis}: отход {backoff:.2f} мм")
    await _auto_fixed_move(ctx, axis, direction, backoff, min(latch, 600.0))
    if await read_switch(ctx, axis):
        raise ControllerError(f"{axis}: концевик снова нажат после отхода")


async def _auto_find_edge(
    ctx: AppContext,
    job: dict,
    *,
    axis: str,
    edge: Literal["motor", "far"],
    direction: int,
    seek: float,
    latch: float,
    backoff: float,
    cfg,
    watchdog_s: float,
) -> dict:
    channel = f"{axis.lower()}_min"
    cancel = job["cancel"]
    edge_text = "у мотора" if edge == "motor" else "от мотора"

    def stopped() -> str | None:
        return "cancelled" if cancel.is_set() else None

    await _auto_progress(ctx, job, axis=axis, phase=f"seek_{edge}", detail=f"Автокалибровка {axis}: быстрый поиск края {edge_text}")
    coarse = await seek_switch(
        ctx.queue, axis=axis, channel=channel, direction=direction,
        feedrate=seek, target_pressed=True, timeout_s=watchdog_s,
        steps_per_unit=cfg.steps_per_unit,
        diagnostics_timeout=ctx.runtime.timeouts.diagnostics,
        move_timeout=ctx.runtime.timeouts.move,
        stop_reason=stopped,
        on_poll=lambda elapsed, sw: _auto_progress(
            ctx, job, axis=axis, phase=f"seek_{edge}",
            detail=f"Автокалибровка {axis}: край {edge_text} · быстрый поиск · {elapsed:.1f} с · {'НАЖАТ' if sw else 'open'}",
        ),
    )
    if not coarse.reached:
        if coarse.stop_reason == "cancelled":
            raise StateError("Автокалибровка остановлена оператором")
        raise ValueError(f"{axis}: физический край {edge_text} не найден до watchdog")

    await _auto_clear_switch(
        ctx, job, axis=axis, direction=-direction, latch=latch, backoff=backoff, cfg=cfg
    )

    await _auto_progress(ctx, job, axis=axis, phase=f"latch_{edge}", detail=f"Автокалибровка {axis}: точный подход к краю {edge_text}")
    precise = await seek_switch(
        ctx.queue, axis=axis, channel=channel, direction=direction,
        feedrate=latch, target_pressed=True,
        timeout_s=max(10.0, 60.0 * (backoff + 3.0) / max(latch, 1e-9)),
        steps_per_unit=cfg.steps_per_unit,
        diagnostics_timeout=ctx.runtime.timeouts.diagnostics,
        move_timeout=ctx.runtime.timeouts.move,
        stop_reason=stopped,
        on_poll=lambda elapsed, sw: _auto_progress(
            ctx, job, axis=axis, phase=f"latch_{edge}",
            detail=f"Автокалибровка {axis}: край {edge_text} · точный подход · {elapsed:.1f} с · {'НАЖАТ' if sw else 'open'}",
        ),
    )
    if not precise.reached:
        if precise.stop_reason == "cancelled":
            raise StateError("Автокалибровка остановлена оператором")
        raise ControllerError(f"{axis}: точный контакт края {edge_text} не найден")

    position, pressed, count = await snapshot(ctx, axis)
    if not pressed:
        raise ControllerError(f"{axis}: точный контакт края {edge_text} не подтвердился")
    return {"position": position, "approach_direction": direction, "step_count": count}


def _build_auto_calibration(cfg, old: AxisCalibration, motor: dict, far: dict) -> AxisCalibration:
    travel = abs(float(motor["position"]) - float(far["position"]))
    if motor.get("step_count") is not None and far.get("step_count") is not None:
        travel_steps = abs(int(motor["step_count"]) - int(far["step_count"]))
        measurement_source = "marlin_count"
    else:
        travel_steps = travel * cfg.steps_per_unit
        measurement_source = "m114_x_steps_per_unit"
    return AxisCalibration(
        channel=f"{cfg.marlin_axis.lower()}_min",
        motor=motor,
        far=far,
        positive_toward_motor=old.positive_toward_motor,
        travel=travel,
        travel_steps=travel_steps,
        seek_feedrate=old.seek_feedrate,
        latch_feedrate=old.latch_feedrate,
        backoff_mm=old.backoff_mm,
        home_side=old.home_side,
        recorded_at=datetime.now(UTC).isoformat(),
        measurement_source=measurement_source,
    )


def _apply_runtime_device_map(ctx: AppContext, device_map) -> None:
    ctx.device_map = device_map
    ctx.motion._map = device_map
    ctx.sender._map = device_map
    ctx.sender._homing = EndstopHoming(device_map, timeout_s=ctx.runtime.timeouts.homing)
    if hasattr(ctx.runner, "_map"):
        ctx.runner._map = device_map


def _save_and_apply_auto_calibration(ctx: AppContext, axis: str, calibration: AxisCalibration) -> None:
    if ctx.profile_name == "default":
        raise ValueError("Автокалибровка сохраняет результат только в активный личный профиль; выберите профиль вместо default")
    profile = load_yaml(find_saved("profiles", ctx.profile_name), ConfigurationProfile)
    axes = [
        item.model_copy(update={"travel": None, "axis_calibration": calibration})
        if item.marlin_axis == axis else item
        for item in profile.device_map.axes
    ]
    new_map = profile.device_map.model_copy(update={"axes": axes})
    new_profile = profile.model_copy(update={"device_map": new_map})
    new_profile = ConfigurationProfile.model_validate(new_profile.model_dump())
    save_yaml("profiles", ctx.profile_name, new_profile)
    _apply_runtime_device_map(ctx, new_profile.device_map)


async def _auto_calibrate_axis(ctx: AppContext, job: dict, axis: str) -> dict:
    cfg = ctx.device_map.axis(axis)
    old = cfg.axis_calibration
    if old is None:
        raise ValueError(f"{axis}: сначала один раз задайте направления в ручной калибровке")
    if not cfg.endstop_channels_shared:
        raise ValueError(f"{axis}: автоматическая калибровка рассчитана на параллельные физические концевики")
    if await read_switch(ctx, axis):
        raise ValueError(
            f"{axis}: общий концевик уже нажат. Сторона при старте неоднозначна; "
            "освободите концевик ручным движением и повторите автокалибровку."
        )

    seek = min(old.seek_feedrate, ctx.motion.max_feedrate(axis), 1200.0)
    latch = min(old.latch_feedrate, seek)
    backoff = old.backoff_mm
    expected = 60.0 * old.travel / max(seek, 1e-9)
    watchdog = max(30.0, min(300.0, expected * 2.0 + 20.0))
    motor_direction = 1 if old.positive_toward_motor else -1
    soft_disabled = False
    try:
        ctx.motion.invalidate([axis])
        await command(ctx, "M211 S0")
        soft_disabled = True
        motor = await _auto_find_edge(
            ctx, job, axis=axis, edge="motor", direction=motor_direction,
            seek=seek, latch=latch, backoff=backoff, cfg=cfg, watchdog_s=watchdog,
        )
        await _auto_clear_switch(
            ctx, job, axis=axis, direction=-motor_direction, latch=latch, backoff=backoff, cfg=cfg
        )
        far = await _auto_find_edge(
            ctx, job, axis=axis, edge="far", direction=-motor_direction,
            seek=seek, latch=latch, backoff=backoff, cfg=cfg, watchdog_s=watchdog,
        )
        await _auto_clear_switch(
            ctx, job, axis=axis, direction=motor_direction, latch=latch, backoff=backoff, cfg=cfg
        )
    finally:
        if soft_disabled:
            await command(ctx, "M211 S1")

    calibrated = _build_auto_calibration(cfg, old, motor, far)
    _save_and_apply_auto_calibration(ctx, axis, calibrated)

    if job["cancel"].is_set():
        raise StateError("Автокалибровка остановлена оператором")
    await _auto_progress(ctx, job, axis=axis, phase="home", detail=f"Автокалибровка {axis}: измерение сохранено · выполняем Home")
    ctx.sender._home_cancel.clear()
    home_details = await ctx.sender._home_parallel_axis(
        axis,
        progress=lambda detail: _auto_progress(ctx, job, axis=axis, phase="home", detail=detail),
        calibration=calibrated,
    )
    return {
        "axis": axis,
        "travel": calibrated.travel,
        "travel_steps": calibrated.travel_steps,
        "measurement_source": calibrated.measurement_source,
        "motor": calibrated.motor.model_dump(),
        "far": calibrated.far.model_dump(),
        "home_side": calibrated.home_side,
        "home": home_details,
        "referenced": bool(ctx.motion.homed.get(axis)),
    }


async def _auto_job_runner(ctx: AppContext, job: dict) -> None:
    job["state"] = "running"
    ctx.sender._home_cancel.clear()
    try:
        for axis in job["axes"]:
            if job["cancel"].is_set():
                raise StateError("Автокалибровка остановлена оператором")
            job["results"][axis] = await _auto_calibrate_axis(ctx, job, axis)
        job.update(state="completed", axis=None, phase="done", detail="Автокалибровка завершена; оси откалиброваны и REFERENCED")
        if ctx.state.state == State.MOVING:
            await ctx.state.transition(State.CONNECTED_IDLE, detail=job["detail"])
    except StateError as exc:
        job.update(state="cancelled" if job["cancel"].is_set() else "failed", error=str(exc), detail=str(exc))
        if ctx.state.state == State.MOVING:
            await ctx.state.transition(State.CONNECTED_IDLE, detail=str(exc))
    except ValueError as exc:
        job.update(state="failed", error=str(exc), detail=str(exc))
        if ctx.state.state == State.MOVING:
            await ctx.state.transition(State.CONNECTED_IDLE, detail=f"Автокалибровка остановлена: {exc}")
    except (ControllerError, TransportError) as exc:
        job.update(state="failed", error=str(exc), detail=f"Автокалибровка: ошибка контроллера: {exc}")
        try:
            await ctx.sender.abort()
        finally:
            if ctx.state.state != State.DISCONNECTED:
                await ctx.state.transition(State.ERRORED, detail=f"Автокалибровка прервана: {exc}; переподключитесь")
    except Exception as exc:  # noqa: BLE001
        job.update(state="failed", error=str(exc), detail=f"Автокалибровка: ошибка: {exc}")
        try:
            await ctx.sender.abort()
        finally:
            if ctx.state.state != State.DISCONNECTED:
                await ctx.state.transition(State.ERRORED, detail=f"Автокалибровка прервана: {exc}; переподключитесь")
    finally:
        ctx.sender._home_cancel.clear()


@router.post("/auto/start")
@_wrap_errors
async def auto_start(body: AutoStart, ctx: Context):
    commissioning_idle(ctx)
    if ctx.sender.manual_session is not None:
        raise StateError("Сначала завершите ручную калибровку")
    if ctx.profile_name == "default":
        raise ValueError("Выберите активный личный профиль: автокалибровка не перезаписывает default")
    # перечитываем профиль, чтобы Auto увидел только что сохранённую калибровку
    latest_profile = load_yaml(find_saved("profiles", ctx.profile_name), ConfigurationProfile)
    _apply_runtime_device_map(ctx, latest_profile.device_map)
    axes = list(dict.fromkeys(body.axes))
    previous = getattr(ctx.sender, "auto_calibration_job", None)
    if previous and previous.get("state") in {"starting", "running"}:
        raise StateError("Автокалибровка уже выполняется")
    for axis in axes:
        cfg = ctx.device_map.axis(axis)
        if cfg.axis_calibration is None:
            raise ValueError(f"{axis}: направления ещё не сохранены; сначала выполните ручную калибровку")
        if await read_switch(ctx, axis):
            raise ValueError(
                f"{axis}: концевик уже нажат. Освободите его ручным движением и повторите автокалибровку."
            )

    job = {
        "job_id": secrets.token_urlsafe(12),
        "axes": axes,
        "state": "starting",
        "axis": None,
        "phase": "starting",
        "detail": f"Автокалибровка: запуск {' + '.join(axes)}",
        "error": None,
        "results": {},
        "cancel": asyncio.Event(),
        "task": None,
    }
    ctx.sender.auto_calibration_job = job
    await ctx.state.transition(State.MOVING, detail=job["detail"])
    job["task"] = asyncio.create_task(_auto_job_runner(ctx, job), name=f"auto-cal-{job['job_id']}")
    return _auto_public(job)


@router.get("/auto/status")
@_wrap_errors
async def auto_status(ctx: Context):
    return _auto_public(getattr(ctx.sender, "auto_calibration_job", None))


@router.post("/auto/cancel")
@_wrap_errors
async def auto_cancel(ctx: Context):
    job = getattr(ctx.sender, "auto_calibration_job", None)
    if not job or job.get("state") not in {"starting", "running"}:
        raise StateError("Автокалибровка сейчас не выполняется")
    job["cancel"].set()
    ctx.sender._home_cancel.set()
    job["detail"] = "Автокалибровка: остановка запрошена"
    return _auto_public(job)

