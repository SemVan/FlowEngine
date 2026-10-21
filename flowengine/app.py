"""FastAPI application factory.

Lifespan hook:
- Loads configs (device map + runtime + modes).
- Opens transport (Mock or Marlin per CLI flag).
- Probes firmware capabilities.
- Wires CommandQueue / Motion / GcodeSender / StateMachine / EventBus into AppContext.
- Starts the state→WS forwarder.
- Tears it all down on shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.responses import HTMLResponse

from flowengine.api import (
    config_router,
    diagnostics_router,
    modes_router,
    motion_router,
    procedures_router,
    stepskip_router,
    ws_router,
)
from flowengine.api.deps import AppContext
from flowengine.api.router_workbench import router as workbench_router
from flowengine.api.router_valves import router as valves_router
from flowengine.api.router_axis_calibration import router as axis_calibration_router, shutdown as shutdown_axis_calibration
from flowengine.config import WEB_DIR, AppSettings, user_overlay_dir
from flowengine.config_store import find_saved, load_yaml
from flowengine.errors import ConfigError, ControllerError
from flowengine.events import EventBus
from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.homing import EndstopHoming
from flowengine.loaders import load_device_map, load_modes, load_runtime
from flowengine.logging_setup import configure_logging
from flowengine.procedures.runner import ProcedureRunner
from flowengine.schemas import ConfigurationProfile
from flowengine.state import State, StateMachine
from flowengine.transport import MarlinTransport, MockTransport, Transport, discover_marlin_port

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


class ProfileSelection(BaseModel):
    name: str


def _profile_parts(name: str):
    if name == "default":
        device_map = load_device_map()
        runtime = load_runtime()
        try:
            modes = load_modes()
        except ConfigError:
            modes = None
        return device_map, runtime, modes
    profile = load_yaml(find_saved("profiles", name), ConfigurationProfile)
    return profile.device_map, profile.runtime, profile.modes


def _remember_profile(name: str) -> None:
    prefs = user_overlay_dir() / "desktop.json"
    temporary = prefs.with_suffix(".tmp")
    temporary.write_text(json.dumps({"profile": name}), encoding="utf-8")
    os.replace(temporary, prefs)


def _build_transport(settings: AppSettings, runtime, device_map=None) -> Transport:
    if settings.mock:
        homes = {v.axis: v.direction_toward_home or (-1 if device_map.axis(v.axis).home_direction == "min" else 1)
                 for v in device_map.valves} if device_map else {}
        channel = device_map.shared_valve_endstop.channel if device_map else None
        return MockTransport(latency_s=0.005, valve_homes=homes, valve_channel=channel or "z_min")
    return MarlinTransport(
        port=discover_marlin_port(settings.serial_port or runtime.transport.port),
        baud=settings.serial_baud or runtime.transport.baud,
        use_line_numbers=runtime.firmware.use_line_numbers,
    )


async def _forward_state_to_ws(
    state: StateMachine, events: EventBus, ctx: AppContext
) -> None:
    q = state.subscribe()
    try:
        while True:
            st, detail = await q.get()
            await events.publish({
                "type": "state",
                "state": st.value,
                "detail": detail,
                "homed": ctx.motion.homed,
                "reference_counts": ctx.sender.reference_counts,
                "reference_sides": ctx.sender.reference_sides,
            })
    except asyncio.CancelledError:
        return
    finally:
        state.unsubscribe(q)


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings.from_env_and_args(argv=[])
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            device_map, runtime, modes = _profile_parts(settings.profile)
            if settings.profile != "default":
                log.info("loaded configuration profile %s", settings.profile)
        except ConfigError as e:
            log.error("config load failed: %s", e)
            raise

        transport = _build_transport(settings, runtime, device_map)
        await transport.open()

        queue = CommandQueue(transport, default_timeout_s=runtime.timeouts.ok_default)
        motion = MotionModel(device_map, feedrate_cap=runtime.motion.feedrate_cap)
        homing = EndstopHoming(device_map, timeout_s=runtime.timeouts.homing)
        sender = GcodeSender(queue, motion, device_map, homing, runtime.timeouts,
                             runtime.firmware, runtime.motion.accel_cap)
        app.state.profile_name = settings.profile
        state = StateMachine()
        events = EventBus()
        runner = ProcedureRunner(sender, device_map, state, events)

        try:
            firmware_raw, firmware_caps = await sender.read_firmware()
            if "marlin" not in firmware_raw.lower():
                raise ControllerError(f"unexpected firmware response: {firmware_raw!r}")
            missing = set(runtime.firmware.features_required) - set(firmware_caps)
            if missing:
                raise ControllerError("firmware features missing: " + ", ".join(sorted(missing)))
            if runtime.motion.configure_firmware:
                await sender.configure()
            detail = (
                "connected; motion enabled"
                if runtime.motion.enabled
                else "connected; diagnostics only"
            )
            await state.transition(State.CONNECTED_IDLE, detail=detail)
        except Exception as e:
            log.exception("initial configure failed: %s", e)
            await state.transition(State.ERRORED, detail=str(e))

        app.state.ctx = AppContext(
            device_map=device_map,
            runtime=runtime,
            modes=modes,
            transport=transport,
            queue=queue,
            motion=motion,
            sender=sender,
            state=state,
            events=events,
            runner=runner,
            profile_name=settings.profile,
        )
        app.state.reconnect_lock = asyncio.Lock()
        forwarder = asyncio.create_task(_forward_state_to_ws(state, events, app.state.ctx))

        try:
            yield
        finally:
            forwarder.cancel()
            valve_job = getattr(app.state.ctx.sender.valves, "job", None)
            if valve_job and valve_job.get("task") and not valve_job["task"].done():
                await app.state.ctx.sender.abort()
                valve_job["task"].cancel()
                await asyncio.gather(valve_job["task"], return_exceptions=True)
            await shutdown_axis_calibration(app.state.ctx)
            await transport.close()

    app = FastAPI(title="FlowEngine", version="0.1.0", lifespan=lifespan)

    # Routers
    app.include_router(motion_router)
    app.include_router(diagnostics_router)
    app.include_router(config_router)
    app.include_router(procedures_router)
    app.include_router(modes_router)
    app.include_router(stepskip_router)
    app.include_router(ws_router)
    app.include_router(workbench_router)
    app.include_router(axis_calibration_router)
    app.include_router(valves_router)

    @app.get("/valves", response_class=HTMLResponse)
    async def valves_page(request: Request):
        return templates.TemplateResponse(request=request, name="valves.html",
                                          context={"title": "Valves · Краны"})

    @app.post("/api/config/apply")
    async def apply_profile_in_browser(selection: ProfileSelection):
        """применяем профиль и заново открываем связь; старые координаты сбрасываем"""
        ctx: AppContext = app.state.ctx
        lock = app.state.reconnect_lock
        async with lock:
            if ctx.runner.status["running"] or ctx.queue.depth or ctx.sender.manual_session or ctx.sender.valves.lock.locked():
                raise HTTPException(409, "Оборудование занято. Завершите текущую операцию или калибровку.")
            if ctx.state.state not in {State.CONNECTED_IDLE, State.ERRORED, State.DISCONNECTED}:
                raise HTTPException(409, "Профиль можно применить только при остановленном оборудовании.")
            try:
                device_map, runtime, modes = _profile_parts(selection.name)
            except ConfigError as exc:
                raise HTTPException(400, str(exc)) from exc

            ctx.sender.manual_session = None
            ctx.sender.invalidate_positions()
            if ctx.state.state != State.DISCONNECTED:
                await ctx.state.transition(State.DISCONNECTED, detail=f"Применяем профиль {selection.name}")

            old_transport = ctx.transport
            try:
                await old_transport.close()
            except Exception:
                log.exception("error while closing transport during profile apply")

            new_transport = None
            try:
                new_transport = _build_transport(settings, runtime, device_map)
                await new_transport.open()
                new_queue = CommandQueue(new_transport, default_timeout_s=runtime.timeouts.ok_default)
                new_motion = MotionModel(device_map, feedrate_cap=runtime.motion.feedrate_cap)
                new_homing = EndstopHoming(device_map, timeout_s=runtime.timeouts.homing)
                new_sender = GcodeSender(
                    new_queue, new_motion, device_map, new_homing, runtime.timeouts,
                    runtime.firmware, runtime.motion.accel_cap,
                )
                firmware_raw, firmware_caps = await new_sender.read_firmware()
                if "marlin" not in firmware_raw.lower():
                    raise ControllerError(f"unexpected firmware response: {firmware_raw!r}")
                missing = set(runtime.firmware.features_required) - set(firmware_caps)
                if missing:
                    raise ControllerError("firmware features missing: " + ", ".join(sorted(missing)))
                if runtime.motion.configure_firmware:
                    await new_sender.configure()

                new_runner = ProcedureRunner(new_sender, device_map, ctx.state, ctx.events)
                ctx.device_map = device_map
                ctx.runtime = runtime
                ctx.modes = modes
                ctx.transport = new_transport
                ctx.queue = new_queue
                ctx.motion = new_motion
                ctx.sender = new_sender
                ctx.runner = new_runner
                ctx.profile_name = selection.name
                app.state.profile_name = selection.name
                _remember_profile(selection.name)
                detail = (
                    f"profile {selection.name} applied; references lost; motion enabled"
                    if runtime.motion.enabled
                    else f"profile {selection.name} applied; references lost; diagnostics/commissioning only"
                )
                await ctx.state.transition(State.CONNECTED_IDLE, detail=detail)
                return {"ok": True, "profile": selection.name, "references_lost": True}
            except Exception as exc:
                if new_transport is not None:
                    try:
                        await new_transport.close()
                    except Exception:
                        pass
                await ctx.state.transition(State.ERRORED, detail=f"Не удалось применить профиль: {exc}")
                raise HTTPException(503, f"Не удалось применить профиль: {exc}") from exc

    @app.post("/api/reconnect")
    async def reconnect_controller():
        """пересоздаём связь и очередь без повторного запуска движений"""
        ctx: AppContext = app.state.ctx
        lock = app.state.reconnect_lock
        async with lock:
            if ctx.runner.status["running"] or ctx.queue.depth or ctx.sender.valves.lock.locked():
                raise HTTPException(409, "Оборудование занято. Сначала остановите текущую операцию.")
            if ctx.state.state not in {State.CONNECTED_IDLE, State.ERRORED, State.DISCONNECTED}:
                raise HTTPException(409, "Переподключение разрешено только после остановки движения.")

            ctx.sender.manual_session = None
            ctx.sender.invalidate_positions()
            if ctx.state.state != State.DISCONNECTED:
                await ctx.state.transition(State.DISCONNECTED, detail="Переподключение контроллера")

            old_transport = ctx.transport
            try:
                await old_transport.close()
            except Exception:  # noqa: BLE001
                log.exception("error while closing old transport during reconnect")

            new_transport = None
            try:
                new_transport = _build_transport(settings, ctx.runtime, ctx.device_map)
                await new_transport.open()
                new_queue = CommandQueue(
                    new_transport, default_timeout_s=ctx.runtime.timeouts.ok_default
                )
                new_motion = MotionModel(
                    ctx.device_map, feedrate_cap=ctx.runtime.motion.feedrate_cap
                )
                new_homing = EndstopHoming(
                    ctx.device_map, timeout_s=ctx.runtime.timeouts.homing
                )
                new_sender = GcodeSender(
                    new_queue,
                    new_motion,
                    ctx.device_map,
                    new_homing,
                    ctx.runtime.timeouts,
                    ctx.runtime.firmware,
                    ctx.runtime.motion.accel_cap,
                )
                firmware_raw, firmware_caps = await new_sender.read_firmware()
                if "marlin" not in firmware_raw.lower():
                    raise ControllerError(f"unexpected firmware response: {firmware_raw!r}")
                missing = set(ctx.runtime.firmware.features_required) - set(firmware_caps)
                if missing:
                    raise ControllerError(
                        "firmware features missing: " + ", ".join(sorted(missing))
                    )
                if ctx.runtime.motion.configure_firmware:
                    await new_sender.configure()

                new_runner = ProcedureRunner(
                    new_sender, ctx.device_map, ctx.state, ctx.events
                )
                ctx.transport = new_transport
                ctx.queue = new_queue
                ctx.motion = new_motion
                ctx.sender = new_sender
                ctx.runner = new_runner
                detail = (
                    "reconnected; references lost; motion enabled"
                    if ctx.runtime.motion.enabled
                    else "reconnected; references lost; diagnostics/commissioning only"
                )
                await ctx.state.transition(State.CONNECTED_IDLE, detail=detail)
                return {
                    "ok": True,
                    "references_lost": True,
                    "transport": new_transport.name,
                    "port": getattr(new_transport, "port", None),
                }
            except Exception as exc:  # noqa: BLE001
                if new_transport is not None:
                    try:
                        await new_transport.close()
                    except Exception:  # noqa: BLE001
                        pass
                await ctx.state.transition(State.ERRORED, detail=f"Переподключение не удалось: {exc}")
                raise HTTPException(503, f"Переподключение не удалось: {exc}") from exc

    @app.middleware("http")
    async def manual_interlock(request, call_next):
        from starlette.responses import JSONResponse
        ctx = getattr(app.state, "ctx", None)
        path = request.url.path
        manual = ctx.sender.manual_session if ctx else None
        hold = manual.get("_run") if manual and manual.get("origin_mode") == "axis_calibration" else None
        if hold and hold.state != "stopped" and (
                path.startswith("/api/diagnostics/") or path == "/api/workbench/sensors"):
            return JSONResponse({"detail": "Во время калибровочного удержания концевики читает только калибровка X/Y"}, status_code=409)
        if (ctx and ctx.sender.manual_session and request.method in {"POST", "PUT", "DELETE"}
                and not path.startswith("/api/axis-calibration/")
                and path not in {"/api/stop", "/api/reconnect", "/api/desktop/reconnect", "/api/desktop/apply"}):
            return JSONResponse({"detail": "Активна калибровка X/Y. Завершите её или переподключитесь"}, status_code=409)
        return await call_next(request)

    @app.get("/calibration", response_class=HTMLResponse)
    async def axis_calibration_page(request: Request):
        return templates.TemplateResponse(request=request, name="axis-calibration.html",
                                          context={"title": "Калибровка X / Y"})

    # Static + page routes
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        ctx: AppContext = request.app.state.ctx
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "device_map": ctx.device_map.model_dump(),
                "runtime": ctx.runtime.model_dump(),
                "title": "FlowEngine",
            },
        )

    @app.get("/procedures", response_class=HTMLResponse)
    async def procedures_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="procedures.html", context={"title": "Procedures"}
        )

    @app.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="config.html", context={"title": "Config"}
        )

    @app.get("/stepskip", response_class=HTMLResponse)
    async def stepskip_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="stepskip.html", context={"title": "Step-skip test"}
        )

    return app
