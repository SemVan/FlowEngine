from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from flowengine.api.deps import AppContext
from flowengine.config_store import find_saved, load_yaml, save_yaml
from flowengine.events import EventBus
from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.homing import EndstopHoming
from flowengine.schemas import AxisCalibration, AxisConfig, ConfigurationProfile, DeviceMap, RuntimeParams
from flowengine.state import State, StateMachine
from flowengine.transport import MockTransport


def calibration(**overrides):
    values = {
        "channel": "y_min",
        "motor": {"position": 120.0, "approach_direction": 1, "step_count": 9600},
        "far": {"position": 20.0, "approach_direction": -1, "step_count": 1600},
        "positive_toward_motor": True,
        "travel": 100.0,
        "travel_steps": 8000,
        "recorded_at": "2026-10-06T12:00:00Z",
        "measurement_source": "marlin_count",
    }
    return AxisCalibration.model_validate(values | overrides)


def test_calibration_is_single_source_of_xy_span():
    c = calibration()
    axis = AxisConfig(
        marlin_axis="Y", name="Y", kind="syringe_pump", steps_per_unit=80,
        travel=None, home_direction="max", feedrate_default=60, feedrate_max=1200,
        axis_calibration=c,
    )
    assert axis.mechanical_travel == 100
    assert axis.travel is None
    assert axis.axis_calibration.travel_steps == 8000


def test_guessed_xy_travel_is_rejected_before_calibration():
    with pytest.raises(ValidationError, match="guessed travel"):
        AxisConfig(
            marlin_axis="X", name="X", kind="syringe_pump", steps_per_unit=80,
            travel=123, home_direction="min", feedrate_default=60, feedrate_max=1200,
        )


def test_motion_model_does_not_clip_to_calibrated_span():
    axis = AxisConfig(
        marlin_axis="Y", name="Y", kind="syringe_pump", steps_per_unit=80,
        travel=None, home_direction="max", feedrate_default=60, feedrate_max=1200,
        axis_calibration=calibration(),
    )
    model = MotionModel(DeviceMap(instrument_id="test", axes=[axis]), 1200)
    model.mark_homed(["Y"])
    assert model.plan_absolute("Y", 150, 100).target == 150
    assert model.plan_absolute("Y", -50, 100).target == -50


class Controller(MockTransport):
    def __init__(self):
        super().__init__(latency_s=0)
        self._positions.update(X=50.0, Y=50.0)
        self.lower_contact = {"X": 0.0, "Y": 49.0}
        self.upper_contact = {"X": 100.0, "Y": 51.0}

    async def _handle(self, payload):
        command = payload.upper()
        if command.startswith("M211 S"):
            await self._inbox.put("ok")
            return
        if command.startswith("G1 "):
            axis, target = re.search(r"([XY])(-?\d+(?:\.\d+)?)", command).groups()
            self._positions[axis] = float(target)
            self._counts[axis] = round(self._positions[axis] * self._steps_per_unit[axis])
            await self._inbox.put("ok")
            return
        if command == "M400":
            await self._inbox.put("ok")
            return
        if command == "M119":
            for axis in ("X", "Y"):
                pressed = self._positions[axis] <= self.lower_contact[axis] or self._positions[axis] >= self.upper_contact[axis]
                await self._inbox.put(f"{axis.lower()}_min: {'TRIGGERED' if pressed else 'open'}")
            await self._inbox.put("ok")
            return
        await super()._handle(payload)


def device_map():
    return DeviceMap(instrument_id="calibration-test", axes=[
        AxisConfig(marlin_axis=a, name=a, kind="syringe_pump", steps_per_unit=80,
                   travel=None, home_direction="min", feedrate_default=60, feedrate_max=1200)
        for a in ("X", "Y")
    ])


@asynccontextmanager
async def api():
    from flowengine.api.router_axis_calibration import router, shutdown

    device = device_map()
    runtime = RuntimeParams()
    transport = Controller()
    await transport.open()
    queue = CommandQueue(transport)
    motion = MotionModel(device, runtime.motion.feedrate_cap)
    sender = GcodeSender(queue, motion, device, EndstopHoming(device, 1), runtime.timeouts)
    state = StateMachine()
    await state.transition(State.CONNECTED_IDLE)
    runner = SimpleNamespace(status={"running": False})
    ctx = AppContext(device, runtime, None, transport, queue, motion, sender, state, EventBus(), runner)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            yield client, ctx
        finally:
            await shutdown(ctx)
            await transport.close()


async def wait_stopped(ctx):
    for _ in range(200):
        if ctx.state.state != State.MOVING:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("calibration move did not stop")


@pytest.mark.asyncio
async def test_physical_endstops_define_span_and_count(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    from flowengine.api import router_axis_calibration

    async with api() as (client, ctx):
        begin = await client.post("/api/axis-calibration/begin", json={
            "axis": "Y", "free_space_confirmed": True,
            "seek_feedrate": 1200, "latch_feedrate": 120, "backoff_mm": 0.5,
        })
        assert begin.status_code == 200, begin.text
        token = begin.json()["token"]

        start = await client.post("/api/axis-calibration/start", json={"token": token, "run_id": "motor", "direction": 1, "feedrate": 1200})
        assert start.status_code == 200, start.text
        await wait_stopped(ctx)
        assert ctx.sender.manual_session["pressed"] is False
        r = await client.post("/api/axis-calibration/record", json={"token": token, "edge": "motor", "observed_contact_confirmed": True})
        assert r.status_code == 200, r.text

        leave = await client.post("/api/axis-calibration/start", json={"token": token, "run_id": "leave", "direction": -1, "feedrate": 1200})
        assert leave.status_code == 200, leave.text
        await wait_stopped(ctx)
        assert ctx.sender.manual_session["pressed"] is False
        assert ctx.sender.manual_session["_run"].stop_reason == "endstop"

        r = await client.post("/api/axis-calibration/record", json={"token": token, "edge": "far", "observed_contact_confirmed": True})
        assert r.status_code == 200, r.text
        state = r.json()
        assert state["measured_travel"] == pytest.approx(2.0, abs=0.3)
        assert state["measured_travel_steps"] == pytest.approx(state["measured_travel"] * 80, abs=2)

        saved = await client.post("/api/axis-calibration/save", json={
            "token": token, "name": "xy", "observed_direction_confirmed": True, "home_side": "far",
        })
        assert saved.status_code == 200, saved.text
        profile = load_yaml(find_saved("profiles", "xy"), ConfigurationProfile)
        y = profile.device_map.axis("Y")
        assert y.travel is None
        assert y.axis_calibration is not None
        assert y.axis_calibration.positive_toward_motor is True
        assert y.axis_calibration.travel == pytest.approx(saved.json()["travel"])
        assert y.axis_calibration.seek_feedrate == 1200
        assert y.axis_calibration.latch_feedrate == 120
        assert y.axis_calibration.backoff_mm == 0.5
        assert y.axis_calibration.home_side == "far"
        assert not any(ctx.motion.homed.values())

@pytest.mark.asyncio
async def test_active_endstop_allows_reverse_even_if_unreferenced_m114_drifted(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    from flowengine.api import router_axis_calibration

    async with api() as (client, ctx):
        begin = await client.post("/api/axis-calibration/begin", json={
            "axis": "Y", "free_space_confirmed": True,
            "seek_feedrate": 1200, "latch_feedrate": 120, "backoff_mm": 0.5,
        })
        token = begin.json()["token"]
        start = await client.post("/api/axis-calibration/start", json={
            "token": token, "run_id": "edge", "direction": 1, "feedrate": 1200,
        })
        assert start.status_code == 200, start.text
        await wait_stopped(ctx)
        assert ctx.sender.manual_session["pressed"] is False
        labelled = await client.post("/api/axis-calibration/record", json={
            "token": token, "edge": "motor", "observed_contact_confirmed": True,
        })
        assert labelled.status_code == 200, labelled.text

        # имитируем снова нажатый микрик после ручного смещения механики
        ctx.transport._positions["Y"] = 51.4
        ctx.transport._counts["Y"] = round(ctx.transport._positions["Y"] * ctx.transport._steps_per_unit["Y"])

        reverse = await client.post("/api/axis-calibration/start", json={
            "token": token, "run_id": "reverse", "direction": -1, "feedrate": 1200,
        })
        assert reverse.status_code == 200, reverse.text
        await wait_stopped(ctx)
        assert ctx.state.state == State.CONNECTED_IDLE
        assert ctx.sender.manual_session is not None
        assert ctx.sender.manual_session["_run"].stop_reason == "endstop"


@pytest.mark.asyncio
async def test_command_into_pressed_endstop_is_nonfatal_and_reverse_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    from flowengine.api import router_axis_calibration

    async with api() as (client, ctx):
        begin = await client.post("/api/axis-calibration/begin", json={
            "axis": "Y", "free_space_confirmed": True,
            "seek_feedrate": 1200, "latch_feedrate": 120, "backoff_mm": 0.5,
        })
        token = begin.json()["token"]
        hit = await client.post("/api/axis-calibration/start", json={
            "token": token, "run_id": "hit", "direction": 1, "feedrate": 1200,
        })
        assert hit.status_code == 200, hit.text
        await wait_stopped(ctx)
        assert ctx.sender.manual_session["pressed"] is False
        labelled = await client.post("/api/axis-calibration/record", json={
            "token": token, "edge": "motor", "observed_contact_confirmed": True,
        })
        assert labelled.status_code == 200, labelled.text
        ctx.transport._positions["Y"] = 51.0
        ctx.transport._counts["Y"] = round(51.0 * ctx.transport._steps_per_unit["Y"])

        wrong = await client.post("/api/axis-calibration/start", json={
            "token": token, "run_id": "wrong", "direction": 1, "feedrate": 1200,
        })
        assert wrong.status_code == 200, wrong.text
        await wait_stopped(ctx)
        assert ctx.state.state == State.CONNECTED_IDLE
        assert ctx.sender.manual_session is not None
        assert ctx.sender.manual_session["_run"].stop_reason == "blocked_endstop"
        state = await client.get(f"/api/axis-calibration/state?token={token}")
        assert state.status_code == 200, state.text

        reverse = await client.post("/api/axis-calibration/start", json={
            "token": token, "run_id": "reverse-after-block", "direction": -1, "feedrate": 1200,
        })
        assert reverse.status_code == 200, reverse.text
        await wait_stopped(ctx)
        assert ctx.state.state == State.CONNECTED_IDLE
        assert ctx.sender.manual_session["_run"].stop_reason == "endstop"

@pytest.mark.asyncio
async def test_latched_contact_can_be_labelled_after_switch_is_no_longer_pressed(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    from flowengine.api import router_axis_calibration

    async with api() as (client, ctx):
        begin = await client.post("/api/axis-calibration/begin", json={
            "axis": "Y", "free_space_confirmed": True,
            "seek_feedrate": 1200, "latch_feedrate": 120, "backoff_mm": 0.5,
        })
        token = begin.json()["token"]
        hit = await client.post("/api/axis-calibration/start", json={
            "token": token, "run_id": "captured", "direction": 1, "feedrate": 1200,
        })
        assert hit.status_code == 200, hit.text
        await wait_stopped(ctx)
        state = (await client.get(f"/api/axis-calibration/state?token={token}")).json()
        assert state["last_contact"] is not None
        assert state["last_contact_edge"] is None

        ctx.transport._positions["Y"] = 50.0
        ctx.transport._counts["Y"] = 4000
        labelled = await client.post("/api/axis-calibration/record", json={
            "token": token, "edge": "motor", "observed_contact_confirmed": True,
        })
        assert labelled.status_code == 200, labelled.text
        body = labelled.json()
        assert body["last_contact_edge"] == "motor"
        assert body["contacts"]["motor"]["approach_direction"] == 1


@pytest.mark.asyncio
async def test_switch_xy_preserves_unsaved_axis_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    async with api() as (client, ctx):
        begin = await client.post("/api/axis-calibration/begin", json={
            "axis": "Y", "free_space_confirmed": True,
            "seek_feedrate": 1200, "latch_feedrate": 120, "backoff_mm": 0.5,
        })
        assert begin.status_code == 200, begin.text
        token = begin.json()["token"]

        hit = await client.post("/api/axis-calibration/start", json={
            "token": token, "run_id": "y-edge", "direction": 1,
        })
        assert hit.status_code == 200, hit.text
        await wait_stopped(ctx)
        labelled = await client.post("/api/axis-calibration/record", json={
            "token": token, "edge": "motor", "observed_contact_confirmed": True,
        })
        assert labelled.status_code == 200, labelled.text

        switched = await client.post("/api/axis-calibration/switch", json={
            "token": token, "axis": "X", "free_space_confirmed": True,
            "seek_feedrate": 1200, "latch_feedrate": 120, "backoff_mm": 0.5,
        })
        assert switched.status_code == 200, switched.text
        assert switched.json()["axis"] == "X"

        back = await client.post("/api/axis-calibration/switch", json={
            "token": token, "axis": "Y", "free_space_confirmed": True,
            "seek_feedrate": 1200, "latch_feedrate": 120, "backoff_mm": 0.5,
        })
        assert back.status_code == 200, back.text
        state = back.json()
        assert state["axis"] == "Y"
        assert state["contacts"]["motor"] is not None
        assert state["positive_toward_motor"] is True


class HomeController(MockTransport):
    """команда G92 меняет координату, но не положение механизма в симуляторе"""

    def __init__(self):
        super().__init__(latency_s=0)
        self._positions.update(Y=50.0)
        self.physical = {"Y": 50.0}
        self.lower_contact = 49.0
        self.upper_contact = 51.0

    async def _handle(self, payload):
        command = payload.upper()
        if command.startswith("M211 S"):
            await self._inbox.put("ok")
            return
        if command.startswith("G1 "):
            match = re.search(r"Y(-?\d+(?:\.\d+)?)", command)
            if match:
                target = float(match.group(1))
                delta = target - self._positions["Y"]
                self._positions["Y"] = target
                self.physical["Y"] += delta
                self._counts["Y"] += round(delta * self._steps_per_unit["Y"])
            await self._inbox.put("ok")
            return
        if command.startswith("G92"):
            match = re.search(r"Y(-?\d+(?:\.\d+)?)", command)
            if match:
                self._positions["Y"] = float(match.group(1))
            await self._inbox.put("ok")
            return
        if command == "M400":
            await self._inbox.put("ok")
            return
        if command == "M119":
            pressed = self.physical["Y"] <= self.lower_contact or self.physical["Y"] >= self.upper_contact
            await self._inbox.put("x_min: open")
            await self._inbox.put(f"y_min: {'TRIGGERED' if pressed else 'open'}")
            await self._inbox.put("z_min: open")
            await self._inbox.put("ok")
            return
        await super()._handle(payload)


def homing_map():
    c = calibration(
        motor={"position": 51.0, "approach_direction": 1, "step_count": 4080},
        far={"position": 49.0, "approach_direction": -1, "step_count": 3920},
        travel=2.0,
        travel_steps=160,
        seek_feedrate=1200,
        latch_feedrate=120,
        backoff_mm=0.5,
        home_side="motor",
    )
    return DeviceMap(instrument_id="home-test", axes=[
        AxisConfig(
            marlin_axis="Y", name="Y", kind="syringe_pump", steps_per_unit=80,
            travel=None, home_direction="max", feedrate_default=1200, feedrate_max=1200,
            axis_calibration=c,
        )
    ])


@pytest.mark.asyncio
async def test_parallel_xy_home_uses_calibration_and_leaves_switch_released():
    device = homing_map()
    runtime = RuntimeParams()
    transport = HomeController()
    await transport.open()
    try:
        queue = CommandQueue(transport)
        motion = MotionModel(device, runtime.motion.feedrate_cap)
        sender = GcodeSender(queue, motion, device, EndstopHoming(device, 1), runtime.timeouts)
        details = await sender.home(["Y"])

        assert motion.homed["Y"] is True
        assert details["Y"]["home_side"] == "motor"
        assert details["Y"]["reference_coordinate"] == pytest.approx(2.0)
        assert motion.positions["Y"] == pytest.approx(1.5, abs=0.08)
        assert sender.reference_sides == {"Y": "motor"}
        assert "Y" in sender.reference_counts
        assert not (await sender.read_endstops())["y_min"]
        assert "M211 S0" in transport.commands
        assert not any(command.startswith("G28 Y") for command in transport.commands)
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_parallel_home_refuses_unknown_pressed_side_without_losing_session():
    device = homing_map()
    runtime = RuntimeParams()
    transport = HomeController()
    transport.physical["Y"] = 51.0
    transport._positions["Y"] = 50.0
    await transport.open()
    try:
        queue = CommandQueue(transport)
        motion = MotionModel(device, runtime.motion.feedrate_cap)
        sender = GcodeSender(queue, motion, device, EndstopHoming(device, 1), runtime.timeouts)
        with pytest.raises(ValueError, match="сторона после запуска неизвестна"):
            await sender.home(["Y"])
        assert motion.homed["Y"] is False
        assert sender.reference_counts == {}
        assert not any(command.startswith("M410") for command in transport.commands)
    finally:
        await transport.close()


@asynccontextmanager
async def auto_api(tmp_path):
    from flowengine.api.router_axis_calibration import router, shutdown

    device = homing_map()
    runtime = RuntimeParams()
    save_yaml(
        "profiles",
        "qa_test",
        ConfigurationProfile(name="qa_test", device_map=device, runtime=runtime, modes=None),
    )
    transport = HomeController()
    await transport.open()
    queue = CommandQueue(transport)
    motion = MotionModel(device, runtime.motion.feedrate_cap)
    sender = GcodeSender(queue, motion, device, EndstopHoming(device, 1), runtime.timeouts)
    state = StateMachine()
    await state.transition(State.CONNECTED_IDLE)
    runner = SimpleNamespace(status={"running": False}, _map=device)
    ctx = AppContext(
        device, runtime, None, transport, queue, motion, sender, state, EventBus(), runner,
        profile_name="qa_test",
    )
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            yield client, ctx
        finally:
            await shutdown(ctx)
            await transport.close()


async def wait_auto_done(client):
    for _ in range(800):
        response = await client.get("/api/axis-calibration/auto/status")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["state"] in {"completed", "failed", "cancelled"}:
            return body
        await asyncio.sleep(0.005)
    raise AssertionError("automatic calibration did not finish")


@pytest.mark.asyncio
async def test_auto_calibration_reuses_verified_direction_saves_span_and_homes(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    async with auto_api(tmp_path) as (client, ctx):
        started = await client.post(
            "/api/axis-calibration/auto/start", json={"axes": ["Y"]}
        )
        assert started.status_code == 200, started.text
        assert started.json()["state"] in {"starting", "running"}

        result = await wait_auto_done(client)
        assert result["state"] == "completed", result
        y = result["results"]["Y"]
        assert y["travel"] == pytest.approx(2.0, abs=0.3)
        assert y["travel_steps"] == pytest.approx(y["travel"] * 80, abs=3)
        assert y["referenced"] is True
        assert ctx.motion.homed["Y"] is True
        assert ctx.sender.reference_sides["Y"] == "motor"

        profile = load_yaml(find_saved("profiles", "qa_test"), ConfigurationProfile)
        saved = profile.device_map.axis("Y").axis_calibration
        assert saved is not None
        assert saved.positive_toward_motor is True
        assert saved.travel == pytest.approx(y["travel"])
        assert ctx.device_map.axis("Y").axis_calibration.travel == pytest.approx(saved.travel)


@pytest.mark.asyncio
async def test_auto_calibration_refuses_pressed_parallel_endstop_at_start(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    async with auto_api(tmp_path) as (client, ctx):
        ctx.transport.physical["Y"] = 51.0
        ctx.transport._positions["Y"] = 50.0
        response = await client.post(
            "/api/axis-calibration/auto/start", json={"axes": ["Y"]}
        )
        assert response.status_code == 400, response.text
        assert "Освободите" in response.text or "освободите" in response.text
        assert ctx.state.state == State.CONNECTED_IDLE
