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
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
from flowengine.config import AppSettings, WEB_DIR
from flowengine.errors import ConfigError
from flowengine.events import EventBus
from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.homing import EndstopHoming
from flowengine.loaders import load_device_map, load_modes, load_runtime
from flowengine.logging_setup import configure_logging
from flowengine.state import State, StateMachine
from flowengine.transport import MarlinTransport, MockTransport, Transport

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _build_transport(settings: AppSettings) -> Transport:
    if settings.mock:
        return MockTransport(latency_s=0.005)
    return MarlinTransport(port=settings.serial_port, baud=settings.serial_baud)


async def _forward_state_to_ws(state: StateMachine, events: EventBus) -> None:
    q = state.subscribe()
    try:
        while True:
            st, detail = await q.get()
            await events.publish({"type": "state", "state": st.value, "detail": detail})
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
            device_map = load_device_map()
            runtime = load_runtime()
            try:
                modes = load_modes()
            except ConfigError:
                modes = None  # modes are optional in Phase 1
        except ConfigError as e:
            log.error("config load failed: %s", e)
            raise

        transport = _build_transport(settings)
        await transport.open()

        queue = CommandQueue(transport, default_timeout_s=runtime.timeouts.ok_default)
        motion = MotionModel(device_map, feedrate_cap=runtime.motion.feedrate_cap)
        homing = EndstopHoming(device_map, timeout_s=runtime.timeouts.homing)
        sender = GcodeSender(queue, motion, device_map, homing, runtime.timeouts)
        state = StateMachine()
        events = EventBus()

        try:
            await sender.configure()
            await state.transition(State.CONNECTED_IDLE, detail="connected")
        except Exception as e:  # noqa: BLE001
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
        )
        forwarder = asyncio.create_task(_forward_state_to_ws(state, events))

        try:
            yield
        finally:
            forwarder.cancel()
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
