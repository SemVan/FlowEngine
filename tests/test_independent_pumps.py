import asyncio
import math
import re

import httpx
import pytest
from fastapi import FastAPI

from flowengine.api.deps import AppContext
from flowengine.api.router_diagnostics import router as diagnostics_router
from flowengine.api.router_procedures import router as procedures_router
from flowengine.errors import ControllerError, MotionValidationError, StateError
from flowengine.events import EventBus
from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.coordinated import independent_segments
from flowengine.hardware.homing import EndstopHoming
from flowengine.procedures.runner import ProcedureRunner
from flowengine.schemas import RuntimeParams
from flowengine.schemas.procedure import MoveMultiStep
from flowengine.state import State, StateMachine
from tests.test_syringe_group_home import PairController, pair_map


class StreamingPumps(PairController):
    def __init__(self):
        super().__init__()
        self.streaming = False
        self.pending = []
        self.started = asyncio.Event()
        self.trigger = False

    async def _handle(self, command):
        if not self.streaming:
            return await super()._handle(command)
        if command.startswith("G1 "):
            targets = {a: float(v) for a, v in re.findall(r"\b([XY])(-?[\d.]+)", command)}
            self.pending.append({"targets": targets, "speed": float(re.search(r"\bF([\d.]+)", command).group(1))})
            for a in targets:
                self.driver_enabled[a] = True
            self.started.set()
            await self._inbox.put("ok")
            return
        if command == "M119" and self.pending:
            current = self.pending[0]
            if "start" not in current:
                current["start"] = {a: self._positions[a] for a in current["targets"]}
                length = math.sqrt(sum((v - current["start"][a]) ** 2 for a, v in current["targets"].items()))
                current.update(duration=60 * length / current["speed"], elapsed=0)
            current["elapsed"] = min(current["duration"], current["elapsed"] + .2)
            fraction = current["elapsed"] / current["duration"]
            if fraction > 1 - 1e-8:
                fraction = 1
            for a, target in current["targets"].items():
                position = current["start"][a] + (target - current["start"][a]) * fraction
                delta = position - self._positions[a]
                self._positions[a] = position
                self.physical[a] += delta
                self._counts[a] += round(delta * self._steps_per_unit[a])
            if fraction == 1:
                self.pending.pop(0)
            if self.trigger:
                self.physical["X"] = 20
        if command == "M410":
            self.pending.clear()
        if command == "M400":
            assert not self.pending, "M119 должен опрашиваться до завершения движения"
        await super()._handle(command)


async def bench(streaming=False):
    device, runtime = pair_map(), RuntimeParams()
    transport = StreamingPumps() if streaming else PairController()
    await transport.open()
    sender = GcodeSender(CommandQueue(transport), MotionModel(device, 1200), device,
                         EndstopHoming(device, 2), runtime.timeouts)
    await sender.home(["X", "Y"])
    if streaming:
        transport.streaming = True
        transport.name = "marlin"
    return sender, transport, runtime


def test_individual_speeds_are_preserved_for_each_active_segment():
    segments, durations = independent_segments({"X": 10, "Y": 10}, {"X": 14, "Y": 6}, {"X": 120, "Y": 60})
    assert durations == {"X": 2, "Y": 4}
    assert [set(s["targets"]) for s in segments] == [{"X", "Y"}, {"Y"}]
    starts = {"X": 10, "Y": 10}
    for segment in segments:
        for a, target in segment["targets"].items():
            assert 60 * abs(target - starts[a]) / segment["nominal_duration_s"] == pytest.approx({"X": 120, "Y": 60}[a])
        starts.update(segment["targets"])
    assert starts == {"X": 14, "Y": 6}


@pytest.mark.parametrize("kwargs", [
    {"feedrate": 120}, {"duration_s": 2}, {"axis_feedrates": {"X": 120}},
    {"axis_feedrates": {"X": 120, "Y": 0}},
])
def test_schema_requires_complete_separate_speeds_and_excludes_common_speed(kwargs):
    data = {"op": "move_multi", "axes": {"X": 4, "Y": -4}, "axis_feedrates": {"X": 120, "Y": 60}, **kwargs}
    with pytest.raises(ValueError):
        MoveMultiStep.model_validate(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_joint_start_different_finish_times_and_retained_references(streaming):
    sender, transport, _ = await bench(streaming)
    initial = sender._motion.positions
    start = len(transport.commands)
    result = await sender.move_multi({"X": 4, "Y": -4}, relative=True, axis_feedrates={"X": 120, "Y": 60})
    commands = transport.commands[start:]
    lines = [c for c in commands if c.startswith("G1 ")]
    assert len(lines) == 2
    assert "X" in lines[0] and "Y" in lines[0]
    assert "X" not in lines[1] and "Y" in lines[1]
    assert result["axes"]["X"]["nominal_duration_s"] == 2
    assert result["axes"]["Y"]["nominal_duration_s"] == 4
    assert sender._motion.positions["X"] == pytest.approx(initial["X"] + 4)
    assert sender._motion.positions["Y"] == pytest.approx(initial["Y"] - 4)
    assert all(sender._motion.homed.values())
    assert not any(c.startswith(("G28", "G92", "M18", "M906", "M500")) for c in commands)
    assert not any(re.search(r"G1.*\b[ABC]", c) for c in commands)


@pytest.mark.asyncio
async def test_stationary_pump_waits_without_receiving_a_move():
    sender, transport, _ = await bench()
    start = len(transport.commands)
    result = await sender.move_multi({"X": 0, "Y": -2}, relative=True, axis_feedrates={"X": 120, "Y": 60})
    assert result["axes"]["X"]["nominal_duration_s"] == 0
    assert all("X" not in c for c in transport.commands[start:] if c.startswith("G1 "))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["speed", "reference", "keys"])
async def test_invalid_group_is_rejected_before_any_command(fault):
    sender, transport, _ = await bench()
    rates = {"X": 120, "Y": 60}
    if fault == "speed":
        rates["Y"] = 9999
    elif fault == "keys":
        rates.pop("Y")
    else:
        sender._motion.invalidate(["Y"])
    start = len(transport.commands)
    with pytest.raises((ValueError, StateError, MotionValidationError)):
        await sender.move_multi({"X": 4, "Y": -4}, relative=True, axis_feedrates=rates)
    assert transport.commands[start:] == []


@pytest.mark.asyncio
async def test_endstop_stops_both_pumps_and_prevents_following_segment():
    sender, transport, _ = await bench(True)
    transport.trigger = True
    start = len(transport.commands)
    with pytest.raises(ControllerError, match="концевик"):
        await sender.move_multi({"X": 4, "Y": -4}, relative=True, axis_feedrates={"X": 120, "Y": 60})
    commands = transport.commands[start:]
    first = next(i for i, c in enumerate(commands) if c.startswith("G1 "))
    assert commands[first + 1:first + 3] == ["M119", "M410"]
    assert len([c for c in commands if c.startswith("G1 ")]) == 1
    assert not transport.pending
    assert not any(sender._motion.homed.values())
    assert all(not transport.driver_enabled[a] for a in "XY")


@pytest.mark.asyncio
async def test_stop_during_independent_move_releases_pumps_without_queued_continuation():
    sender, transport, _ = await bench(True)
    transport._latency = .002
    start = len(transport.commands)
    task = asyncio.create_task(sender.move_multi({"X": 4, "Y": -4}, relative=True,
                                               axis_feedrates={"X": 120, "Y": 60}))
    await transport.started.wait()
    await sender.abort()
    with pytest.raises(ControllerError):
        await task
    commands = transport.commands[start:]
    stop = commands.index("M410")
    assert not any(c.startswith("G1 ") for c in commands[stop + 1:])
    assert not transport.pending
    assert not any(sender._motion.homed.values())
    assert all(not transport.driver_enabled[a] for a in "XY")


@pytest.mark.asyncio
async def test_procedure_roundtrip_parameters_and_repeated_runs_without_volume_calibration(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    sender, transport, runtime = await bench()
    state = StateMachine()
    await state.transition(State.CONNECTED_IDLE)
    events = EventBus()
    runner = ProcedureRunner(sender, sender._map, state, events)
    ctx = AppContext(sender._map, runtime, None, transport, sender._q, sender._motion, sender, state, events, runner)
    app = FastAPI(); app.state.ctx = ctx
    app.include_router(procedures_router); app.include_router(diagnostics_router)
    procedure = {"name": "two_pumps", "parameters": {"speed_x": {"type": "number", "default": 120}, "speed_y": {"type": "number", "default": 60}}, "steps": [
        {"op": "move_multi", "axes": {"X": 4, "Y": -4}, "relative": True, "axis_feedrates": {"X": "${speed_x}", "Y": "${speed_y}"}},
        {"op": "move_multi", "axes": {"X": -4, "Y": 4}, "relative": True, "axis_feedrates": {"X": "${speed_x}", "Y": "${speed_y}"}},
    ]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        saved = await client.put("/api/procedures/two_pumps", json=procedure)
        assert saved.status_code == 200, saved.text
        loaded = (await client.get("/api/procedures/two_pumps")).json()
        assert loaded["steps"][0]["axis_feedrates"]["X"] == "${speed_x}"
        preview = await client.post("/api/procedures/two_pumps/preview", json={"speed_x": 180})
        assert preview.status_code == 200, preview.text
        assert not preview.json()["execution_issues"] and not preview.json()["readiness_issues"]
        initial = sender._motion.positions
        start = len(transport.commands)
        for _ in range(3):
            response = await client.post("/api/procedures/two_pumps/run", json={"speed_x": 180})
            assert response.status_code == 200, response.text
            for _ in range(100):
                status = (await client.get("/api/procedures/status")).json()
                if not status["running"]:
                    break
                await asyncio.sleep(.01)
            assert not status["running"] and status["error"] is None, status
            assert sender._motion.positions == initial
            assert all(sender._motion.homed.values())
        assert not any(c.startswith(("G28", "G92", "M18")) for c in transport.commands[start:])
        assert not sender._map.pumps
        procedure["name"] = "invalid_speed"
        procedure["steps"].insert(0, {"op": "home", "axes": ["X", "Y"]})
        procedure["steps"][1]["axis_feedrates"]["Y"] = 9999
        assert (await client.put("/api/procedures/invalid_speed", json=procedure)).status_code == 200
        start = len(transport.commands)
        response = await client.post("/api/procedures/invalid_speed/run", json={})
        assert response.status_code == 409
        assert transport.commands[start:] == []
