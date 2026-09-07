"""Dependency-injection helpers for FastAPI routers.

Holds the singletons that the app constructs at startup: device map, runtime
params, transport, queue, sender, state machine, event bus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from flowengine.events import EventBus
    from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
    from flowengine.procedures.runner import ProcedureRunner
    from flowengine.schemas import DeviceMap, ModesConfig, RuntimeParams
    from flowengine.state import StateMachine
    from flowengine.transport import Transport


@dataclass
class AppContext:
    device_map: DeviceMap
    runtime: RuntimeParams
    modes: ModesConfig | None
    transport: Transport
    queue: CommandQueue
    motion: MotionModel
    sender: GcodeSender
    state: StateMachine
    events: EventBus
    runner: ProcedureRunner


def get_ctx(request: Request) -> AppContext:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        raise RuntimeError("AppContext is not set on app.state — bad startup wiring")
    return ctx
