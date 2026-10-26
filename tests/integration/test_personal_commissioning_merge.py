from __future__ import annotations

import re

from fastapi.testclient import TestClient
import pytest

from flowengine.app import create_app
from flowengine.config import AppSettings
from flowengine.events import EventBus
from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.homing import EndstopHoming
from flowengine.procedures.preflight import execution_issues
from flowengine.procedures.runner import ProcedureRunner
from flowengine.schemas import ConfigurationProfile, DeviceMap, Procedure, RuntimeParams
from flowengine.state import State, StateMachine
from flowengine.transport import MockTransport
from tests.test_axis_calibration import HomeController, homing_map
from tests.test_valves import valve_map


async def runner_for(device, transport):
    runtime = RuntimeParams()
    await transport.open()
    sender = GcodeSender(CommandQueue(transport), MotionModel(device, 1200), device,
                         EndstopHoming(device, 1), runtime.timeouts)
    state = StateMachine()
    await state.transition(State.CONNECTED_IDLE)
    return ProcedureRunner(sender, device, state, EventBus()), sender


@pytest.mark.parametrize("positive,side", [(True, "motor"), (False, "motor"), (True, "far")])
async def test_procedure_home_uses_saved_xy_direction_side_and_speeds(positive, side):
    data = homing_map().model_dump(mode="json")
    cal = data["axes"][0]["axis_calibration"]
    cal["positive_toward_motor"] = positive
    cal["home_side"] = side
    if not positive:
        cal["motor"], cal["far"] = cal["far"], cal["motor"]
    device = DeviceMap.model_validate(data)
    transport = HomeController()
    runner, sender = await runner_for(device, transport)
    try:
        await runner.run(Procedure(name="saved_home", steps=[{"op": "home", "axes": ["Y"]}]))
        assert runner.status["error"] is None
        result = runner.status["results"][0]["result"]["Y"]
        assert result["home_side"] == side
        assert result["reference_coordinate"] == device.axis("Y").axis_calibration.home_coordinate
        first = next(c for c in transport.commands if c.startswith("G1 Y"))
        delta = float(re.search(r"Y(-?[\d.]+)", first).group(1)) - 50
        assert delta * device.axis("Y").axis_calibration.home_approach_direction > 0
        assert any("F1200" in c for c in transport.commands if c.startswith("G1"))
        assert any("F120." in c for c in transport.commands if c.startswith("G1"))
        assert not any(c.startswith("G28") for c in transport.commands)
        assert sender.reference_sides == {"Y": side}
    finally:
        await transport.close()


@pytest.mark.parametrize("direction", [-1, 1])
async def test_procedure_valve_home_and_named_position_use_personal_engine(direction):
    data = valve_map().model_dump(mode="json")
    data["axes"][2]["travel"] = .01
    data["valves"][2].update(direction_toward_home=direction, home_seek_speed=113,
                            home_latch_speed=37, position_a=-direction, position_b=-2 * direction,
                            position_names={"ПРОКАЧКА": "A"})
    device = DeviceMap.model_validate(data)
    transport = MockTransport(latency_s=0, valve_homes={v.axis: v.direction_toward_home for v in device.valves})
    runner, sender = await runner_for(device, transport)
    try:
        proc = Procedure(name="valve_recipe", steps=[
            {"op": "home_valve", "name": "sample_valve"},
            {"op": "set_valve", "name": "sample_valve", "position": "ПРОКАЧКА"},
            {"op": "motors", "axes": ["C"], "enabled": False},
        ])
        assert not execution_issues(proc, device, RuntimeParams())
        await runner.run(proc)
        assert runner.status["error"] is None
        moves = [c for c in transport.commands if c.startswith("G1 ")]
        assert float(re.search(r"C(-?[\d.]+)", moves[0]).group(1)) * direction > 0
        assert any("F113." in c for c in moves)
        assert any("F37." in c for c in moves)
        assert float(re.search(r"C(-?[\d.]+)", moves[-1]).group(1)) == -direction
        assert "M18 C" in transport.commands
        assert not sender._motion.homed["C"]
        assert transport.soft_endstops
        assert not any(c.startswith(("G28", "M906", "M17")) for c in transport.commands)
    finally:
        await transport.close()


async def test_procedural_xy_home_without_calibration_cannot_fall_back_to_g28():
    data = homing_map().model_dump(mode="json")
    data["axes"][0]["axis_calibration"] = None
    device = DeviceMap.model_validate(data)
    transport = HomeController()
    runner, sender = await runner_for(device, transport)
    try:
        proc = Procedure(name="uncalibrated", steps=[{"op": "home", "axes": ["Y"]}])
        assert any("G28" in issue for issue in execution_issues(proc, device, RuntimeParams()))
        with pytest.raises(ValueError, match="G28"):
            await sender.home(["Y"])
        assert not any(c.startswith(("G1", "G28")) for c in transport.commands)
    finally:
        await transport.close()


def test_commissioning_apply_keeps_personal_calibration_without_gcode(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    device = homing_map()
    profile = ConfigurationProfile(name="ours", device_map=device, runtime=RuntimeParams())
    with TestClient(create_app(AppSettings(mock=True, log_level="WARNING"))) as client:
        assert client.put("/api/config/profiles/ours", json=profile.model_dump(mode="json")).status_code == 200
        before = list(client.app.state.ctx.transport.commands)
        response = client.post("/api/commissioning/profiles/ours/apply", json={})
        assert response.status_code == 200, response.text
        ctx = client.app.state.ctx
        assert ctx.device_map.axis("Y").axis_calibration == device.axis("Y").axis_calibration
        assert ctx.sender._map.axis("Y").axis_calibration == device.axis("Y").axis_calibration
        assert ctx.transport.commands == before
        assert not any(ctx.motion.homed.values())
        response = client.post("/api/commissioning/reference", json={
            "axis": "Y", "position": 0, "notes": "not a replacement for HOME", "confirmed": True})
        assert response.status_code == 400
        assert ctx.transport.commands == before
