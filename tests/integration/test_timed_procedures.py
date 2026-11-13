import time

import pytest
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings
from tests.conftest import use_firmware_home_axes


@pytest.fixture
def bench(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    with TestClient(create_app(AppSettings(mock=True, log_level="WARNING"))) as client:
        use_firmware_home_axes(client.app.state.ctx)
        client.app.state.ctx.transport._latency = 0
        yield client, client.app.state.ctx.transport


def wait(client):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = client.get("/api/procedures/status").json()
        if not status["running"]:
            return status
        time.sleep(.01)
    pytest.fail("procedure did not finish")


def test_repeated_procedure_reuses_home_and_parking_then_release_requires_home_again(bench):
    client, transport = bench
    config = client.get("/api/config").json()["device_map"]
    configured = client.put("/api/valves/config", json={"valves": config["valves"],
                            "shared_valve_endstop": {"channel": "z_min"}})
    assert configured.status_code == 200, configured.text
    proc = {"name": "repeatable", "steps": [
        {"op": "home", "axes": ["X"], "only_if_needed": True},
        {"op": "home_valve", "name": "sample_valve", "only_if_needed": True},
        {"op": "move", "axis": "X", "to": 10, "feedrate": 120},
        {"op": "move", "axis": "X", "by": -2, "feedrate": 120},
        {"op": "move", "axis": "X", "to": 10, "feedrate": 120},
    ]}
    assert client.put("/api/procedures/repeatable", json=proc).status_code == 200
    first_valve_moves = None
    for _ in range(3):
        assert client.post("/api/procedures/repeatable/run", json={}).status_code == 200
        assert wait(client)["error"] is None
        assert client.get("/api/state").json()["positions"]["X"] == 10
        count = sum(c.startswith("G1 C") for c in transport.commands)
        if first_valve_moves is None:
            first_valve_moves = count
        assert count == first_valve_moves
    assert transport.commands.count("G28 X") == 1
    valve_moves = [c for c in transport.commands if c.startswith("G1 C")]
    assert valve_moves
    assert all(client.get("/api/state").json()["homed"][a] for a in ["X", "C"])
    assert transport.stepper_timeout_s == 0
    assert not any(c.startswith("M18") for c in transport.commands)
    preview = client.post("/api/procedures/repeatable/preview", json={}).json()
    assert preview["session_warnings"] == []
    assert client.post("/api/workbench/motors", json={"enabled": False}).status_code == 200
    assert transport.stepper_timeout_s == 120
    assert not any(client.get("/api/state").json()["homed"].values())
    assert client.post("/api/procedures/repeatable/run", json={}).status_code == 200
    assert wait(client)["error"] is None
    assert transport.commands.count("G28 X") == 2
    assert len([c for c in transport.commands if c.startswith("G1 C")]) > len(valve_moves)


def test_preview_explains_forced_home_and_release_without_changing_their_meaning(bench):
    client, transport = bench
    proc = {"name": "forced", "steps": [
        {"op": "home", "axes": ["X"]}, {"op": "motors", "enabled": False},
    ]}
    assert client.put("/api/procedures/forced", json=proc).status_code == 200
    preview = client.post("/api/procedures/forced/preview", json={}).json()
    assert len(preview["session_warnings"]) == 2
    for _ in range(2):
        assert client.post("/api/procedures/forced/run", json={}).status_code == 200
        assert wait(client)["error"] is None
    assert transport.commands.count("G28 X") == 2
    assert not any(client.get("/api/state").json()["homed"].values())


def test_prepared_session_shutdown_releases_drivers(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    with TestClient(create_app(AppSettings(mock=True, log_level="WARNING"))) as client:
        use_firmware_home_axes(client.app.state.ctx)
        transport = client.app.state.ctx.transport
        assert client.post("/api/home", json={"axes": ["X"]}).status_code == 200
        assert transport.driver_enabled["X"]
        assert transport.stepper_timeout_s == 0
    assert not transport.driver_enabled["X"]
    assert transport.stepper_timeout_s == 120


@pytest.mark.parametrize("direction,speed,units,seconds,expected", [
    (1, 120, "axis/min", 2, 14),
    (-1, 120, "axis/min", 2, 6),
    (1, 1, "axis/s", 3, 13),
    (1, 80, "steps/s", 2, 12),
])
def test_time_speed_and_direction_produce_expected_travel(bench, direction, speed, units, seconds, expected):
    client, transport = bench
    proc = {"name": "timed", "parameters": {
        "speed": {"type": "number", "default": speed},
        "time": {"type": "number", "default": seconds},
    }, "steps": [
        {"op": "home", "axes": ["X"]},
        {"op": "move", "axis": "X", "to": 10, "feedrate": 120},
        {"op": "move_for", "axis": "X", "direction": direction, "feedrate": "${speed}",
         "seconds": "${time}", "speed_units": units},
        {"op": "motors", "enabled": False, "axes": ["X"]},
    ]}
    assert client.put("/api/procedures/timed", json=proc).status_code == 200
    before = list(transport.commands)
    preview = client.post("/api/procedures/timed/preview", json={}).json()
    assert not preview["execution_issues"] and not preview["readiness_issues"]
    assert transport.commands == before
    assert client.post("/api/procedures/timed/run", json={}).status_code == 200
    status = wait(client)
    assert status["error"] is None, status
    assert abs(client.get("/api/state").json()["positions"]["X"] - expected) < 1e-6
    assert status["results"][-1]["result"]["nominal_duration_s"] == seconds
    assert not transport.driver_enabled["X"]


def test_distance_over_time_converts_steps_to_axis_units(bench):
    client, transport = bench
    proc = {"name": "distance_time", "steps": [
        {"op": "home", "axes": ["X"]},
        {"op": "move", "axis": "X", "by": 800, "units": "steps", "duration_s": 2},
    ]}
    assert client.put("/api/procedures/distance_time", json=proc).status_code == 200
    assert client.post("/api/procedures/distance_time/run", json={}).status_code == 200
    assert wait(client)["error"] is None
    assert any(command == "G1 X10.0000 F300.000" for command in transport.commands)


@pytest.mark.parametrize("step", [
    {"op": "move_for", "axis": "X", "feedrate": 120, "seconds": 0},
    {"op": "move_for", "axis": "X", "feedrate": -120, "seconds": 2},
    {"op": "move", "axis": "X", "by": 5, "duration_s": 2, "feedrate": 120},
])
def test_invalid_timing_rejected_without_commands(bench, step):
    client, transport = bench
    before = list(transport.commands)
    assert client.put("/api/procedures/invalid", json={"name": "invalid", "steps": [step]}).status_code == 422
    assert transport.commands == before


def test_missing_reference_is_reported_before_any_step_runs(bench):
    client, transport = bench
    proc = {"name": "missing_home", "steps": [
        {"op": "home", "axes": ["Y"]},
        {"op": "move_for", "axis": "X", "feedrate": 120, "seconds": 2},
    ]}
    assert client.put("/api/procedures/missing_home", json=proc).status_code == 200
    before = list(transport.commands)
    preview = client.post("/api/procedures/missing_home/preview", json={}).json()
    assert preview["readiness_issues"] == ["Шаг 2: нужен HOME для X до этого шага"]
    response = client.post("/api/procedures/missing_home/run", json={})
    assert response.status_code == 409
    assert "HOME" in response.json()["detail"]
    assert transport.commands == before


def test_release_then_movement_needs_another_home(bench):
    client, transport = bench
    proc = {"name": "released", "steps": [
        {"op": "home", "axes": ["X"]},
        {"op": "motors", "enabled": False, "axes": ["X"]},
        {"op": "move_for", "axis": "X", "feedrate": 120, "seconds": 2},
    ]}
    assert client.put("/api/procedures/released", json=proc).status_code == 200
    before = list(transport.commands)
    assert client.post("/api/procedures/released/run", json={}).status_code == 409
    assert transport.commands == before


def test_excess_speed_does_not_silently_extend_timed_motion(bench):
    client, transport = bench
    proc = {"name": "too_fast", "steps": [
        {"op": "home", "axes": ["X"]},
        {"op": "move_for", "axis": "X", "feedrate": 99999, "seconds": 2},
    ]}
    assert client.put("/api/procedures/too_fast", json=proc).status_code == 200
    before = list(transport.commands)
    result = client.post("/api/procedures/too_fast/run", json={})
    assert result.status_code == 409
    assert "выше предела" in result.json()["detail"]
    assert transport.commands == before
