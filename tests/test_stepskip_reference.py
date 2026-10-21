from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from flowengine.api.deps import AppContext
from flowengine.api.router_stepskip import router
from flowengine.events import EventBus
from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.homing.endstop import EndstopHoming
from flowengine.schemas.runtime import RuntimeParams
from flowengine.state import State, StateMachine
from tests.test_axis_calibration import HomeController, homing_map


async def make_sender():
    device = homing_map()
    runtime = RuntimeParams()
    transport = HomeController()
    await transport.open()
    queue = CommandQueue(transport)
    motion = MotionModel(device, runtime.motion.feedrate_cap)
    sender = GcodeSender(queue, motion, device, EndstopHoming(device, 1), runtime.timeouts)
    return device, runtime, transport, queue, motion, sender


@pytest.mark.asyncio
async def test_reference_check_ok_reuses_home_latch_and_keeps_reference():
    device, runtime, transport, queue, motion, sender = await make_sender()
    try:
        await sender.home(["Y"])
        baseline = sender.reference_counts["Y"]
        result = await sender.check_reference(["Y"], tolerance_steps=8)
        check = result["Y"]["step_check"]
        assert check["status"] == "OK"
        assert check["baseline_count"] == baseline
        assert abs(check["delta_steps"]) <= 8
        assert motion.homed["Y"] is True
        # удачная проверка не должна сдвигать исходную точку HOME
        assert sender.reference_counts["Y"] == baseline
        assert not (await sender.read_endstops())["y_min"]
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_reference_check_detects_physical_offset_and_invalidates_reference():
    device, runtime, transport, queue, motion, sender = await make_sender()
    try:
        await sender.home(["Y"])
        baseline = sender.reference_counts["Y"]
        # сдвигаем механизм, но оставляем прежний Count
        transport.physical["Y"] -= 0.25
        result = await sender.check_reference(["Y"], tolerance_steps=8)
        check = result["Y"]["step_check"]
        assert check["status"] == "STEP_LOSS"
        assert check["baseline_count"] == baseline
        assert check["abs_delta_steps"] > 8
        assert motion.homed["Y"] is False
        assert "Y" not in sender.reference_counts
        assert not (await sender.read_endstops())["y_min"]
    finally:
        await transport.close()


@asynccontextmanager
async def stepskip_api():
    device, runtime, transport, queue, motion, sender = await make_sender()
    state = StateMachine()
    await state.transition(State.CONNECTED_IDLE)
    runner = SimpleNamespace(status={"running": False})
    ctx = AppContext(
        device, runtime, None, transport, queue, motion, sender, state, EventBus(), runner,
        profile_name="qa_test",
    )
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            yield client, ctx
        finally:
            await transport.close()


@pytest.mark.asyncio
async def test_stepskip_api_requires_home_then_reports_ok():
    async with stepskip_api() as (client, ctx):
        refused = await client.post("/api/stepskip/start", json={"axes": ["Y"], "tolerance_steps": 8})
        assert refused.status_code == 400
        assert "Home" in refused.text

        await ctx.sender.home(["Y"])
        started = await client.post("/api/stepskip/start", json={"axes": ["Y"], "tolerance_steps": 8})
        assert started.status_code == 200, started.text
        job_id = started.json()["job_id"]
        for _ in range(500):
            status = await client.get(f"/api/stepskip/status?job_id={job_id}")
            assert status.status_code == 200, status.text
            body = status.json()
            if body["state"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.005)
        assert body["state"] == "completed", body
        assert body["result"]["status"] == "OK"
        assert body["result"]["axes"]["Y"]["referenced"] is True
        assert ctx.state.state == State.CONNECTED_IDLE
