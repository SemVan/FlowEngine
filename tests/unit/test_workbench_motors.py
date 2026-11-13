from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from flowengine.api.deps import AppContext
from flowengine.api.router_motion import router as motion_router
from flowengine.api.router_workbench import router as workbench_router
from flowengine.events import EventBus
from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.homing import EndstopHoming
from flowengine.schemas import AxisConfig, DeviceMap, RuntimeParams
from flowengine.state import State, StateMachine
from flowengine.transport import MockTransport


@pytest.fixture
async def motor_api():
    device = DeviceMap(
        instrument_id="motor-race-mock",
        axes=[AxisConfig(marlin_axis=axis, name=axis, kind="syringe_pump",
                         steps_per_unit=80, travel=200, home_direction="min",
                         feedrate_default=600, feedrate_max=3000,
                         endstop_channels_shared=False)
              for axis in ("X", "Y")],
    )
    runtime = RuntimeParams()
    transport = MockTransport(latency_s=0)
    await transport.open()
    queue = CommandQueue(transport)
    motion = MotionModel(device, runtime.motion.feedrate_cap)
    sender = GcodeSender(queue, motion, device, EndstopHoming(device, 1), runtime.timeouts)
    state = StateMachine()
    await state.transition(State.CONNECTED_IDLE)

    async def abort_runner():
        pass

    runner = SimpleNamespace(status={"running": False}, abort=abort_runner)
    ctx = AppContext(device, runtime, None, transport, queue, motion, sender,
                     state, EventBus(), runner)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(workbench_router)
    app.include_router(motion_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://mock-test") as client:
        yield client, ctx
    await transport.close()


@pytest.mark.parametrize("enabled", [False, True])
async def test_stop_at_motor_ack_boundary_preserves_error_and_lost_references(motor_api, monkeypatch, enabled):
    client, ctx = motor_api
    ctx.motion.mark_homed(["X", "Y"])
    acknowledged = asyncio.Event()
    return_to_router = asyncio.Event()
    original = ctx.sender.motors

    async def pause_after_ack(enabled, axes):
        await original(enabled, axes)
        acknowledged.set()
        await return_to_router.wait()

    monkeypatch.setattr(ctx.sender, "motors", pause_after_ack)
    pending = asyncio.create_task(client.post("/api/workbench/motors", json={
        "enabled": enabled, "axes": ["Y"],
    }))
    await asyncio.wait_for(acknowledged.wait(), 1)
    assert ctx.state.state == State.MOVING
    assert ctx.transport.driver_enabled["Y"] is enabled
    stop = await client.post("/api/stop")
    assert stop.status_code == 200
    stop_detail = ctx.state.detail
    assert "stop requested" in stop_detail
    return_to_router.set()
    completed = await asyncio.wait_for(pending, 1)
    assert completed.status_code == 409, completed.text
    assert ctx.state.state == State.ERRORED
    assert ctx.state.detail == stop_detail
    assert not any(ctx.motion.homed.values())
    command = "M17 Y" if enabled else "M18 Y"
    assert ctx.transport.commands == ["M400", "M114", command, "M410"]


async def test_motor_completion_requires_moving_state_even_without_stop_epoch(motor_api, monkeypatch):
    client, ctx = motor_api
    ctx.motion.mark_homed(["X", "Y"])
    original = ctx.sender.motors

    async def loss_of_state_after_ack(enabled, axes):
        await original(enabled, axes)
        await ctx.state.transition(State.ERRORED, detail="connection state lost at completion")

    monkeypatch.setattr(ctx.sender, "motors", loss_of_state_after_ack)
    result = await client.post("/api/workbench/motors", json={"enabled": False, "axes": ["Y"]})
    assert result.status_code == 409, result.text
    assert ctx.state.state == State.ERRORED
    assert ctx.state.detail == "connection state lost at completion"
    assert not any(ctx.motion.homed.values())


async def test_motor_commands_are_blocked_during_procedure_before_any_gcode(motor_api):
    client, ctx = motor_api
    ctx.runner.status["running"] = True
    result = await client.post("/api/workbench/motors", json={"enabled": False, "axes": ["Y"]})
    assert result.status_code == 409, result.text
    assert ctx.transport.commands == []
    assert ctx.state.state == State.CONNECTED_IDLE


async def test_motor_transport_failure_invalidates_references_and_sets_error(motor_api, monkeypatch):
    client, ctx = motor_api
    ctx.motion.mark_homed(["X", "Y"])

    async def fail_motors(enabled, axes):
        raise ValueError("mock motor control failure")

    monkeypatch.setattr(ctx.sender, "motors", fail_motors)
    result = await client.post("/api/workbench/motors", json={"enabled": False, "axes": ["Y"]})
    assert result.status_code == 400
    assert ctx.state.state == State.ERRORED
    assert ctx.state.detail == "motor control failed"
    assert not any(ctx.motion.homed.values())
