from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    with TestClient(create_app(AppSettings(mock=True, log_level="WARNING"))) as client:
        yield client


def test_connection_report_and_trace(client):
    assert client.get("/").status_code == 200
    assert "connection-view" in client.get("/").text
    assert client.get("/api/state").json()["profile"] == "default"
    response = client.post("/api/workbench/console", json={"command": "M114"})
    assert response.status_code == 200
    report = client.get("/api/workbench/report").json()
    assert any(r["direction"] == "TX" and r["line"] == "M114" for r in report["serial_trace"])
    assert any(r["direction"] == "RX" for r in report["serial_trace"])


def test_physical_motion_and_release(client):
    client.post("/api/home", json={"axes": ["X"]})
    result = client.post(
        "/api/workbench/jog",
        json={
            "axis": "X",
            "by": 80,
            "units": "steps",
            "feedrate": 80,
            "speed_units": "steps/s",
            "acceleration": 50,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["positions"]["X"] == 1
    assert result.json()["effective_feedrate_axis_min"] == 60
    result = client.post("/api/workbench/motors", json={"enabled": False, "axes": ["X"]})
    assert result.status_code == 200
    assert result.json()["homed"]["X"] is False
    result = client.post("/api/workbench/jog", json={"axis": "X", "by": 1, "feedrate": 60})
    assert result.status_code == 400


def test_calibration_does_not_apply_or_move(client):
    before = client.get("/api/workbench/report").json()["serial_trace"]
    result = client.post(
        "/api/workbench/calibrate-pump",
        json={
            "name": "sample_pump",
            "steps_per_unit": 80,
            "measurements": [{"step_pulses": 80, "volume_ul": 25}],
        },
    )
    assert result.status_code == 200
    assert result.json()["volume_per_unit_ul"] == 25 and result.json()["applied"] is False
    assert client.get("/api/workbench/report").json()["serial_trace"] == before
    assert client.get("/api/config").json()["device_map"]["pumps"][0]["calibrated"] is False


def test_nested_uncalibrated_procedure_is_rejected_before_motion(client):
    child = {
        "name": "uncalibrated",
        "steps": [{"op": "pump", "name": "sample_pump", "volume_ul": 1, "flow_ul_min": 1}],
    }
    parent = {
        "name": "parent",
        "steps": [{"op": "home", "axes": ["X"]}, {"op": "call", "procedure": "uncalibrated"}],
    }
    assert client.put("/api/procedures/uncalibrated", json=child).status_code == 200
    assert client.put("/api/procedures/parent", json=parent).status_code == 200
    before = client.get("/api/workbench/report").json()["serial_trace"]
    preview = client.post("/api/procedures/parent/preview", json={})
    assert preview.json()["execution_issues"]
    assert client.post("/api/procedures/parent/run", json={}).status_code == 409
    assert client.get("/api/workbench/report").json()["serial_trace"] == before


def test_probe_current_and_missing_adc_are_explicit(client):
    assert (
        client.post("/api/workbench/current", json={"axis": "X", "current_ma": 600}).status_code
        == 400
    )
    assert client.get("/api/workbench/capabilities").json()["holding_current"] is False
    result = client.get("/api/workbench/sensors")
    assert result.status_code == 200 and result.json()["values"] == []
    result = client.post(
        "/api/workbench/reference-error",
        json={"baseline_pulses": 80, "measured_pulses": [81, 100], "tolerance_pulses": 5},
    )
    assert result.json()["suspected_position_error"] is True


def test_stop_does_not_restore_idle_or_reference(client):
    client.post("/api/home", json={"axes": ["X"]})
    assert client.post("/api/stop").status_code == 200
    state = client.get("/api/state").json()
    assert state["state"] == "errored" and state["homed"]["X"] is False
    assert client.post("/api/home", json={"axes": ["X"]}).status_code == 409


def test_saved_profile_calibrated_pumps_and_sensors(client):
    profile = {"name": "calibrated_profile", **client.get("/api/config").json()}
    for pump in profile["device_map"]["pumps"]:
        pump["calibrated"] = True
        pump["volume_per_unit_ul"] = 25 if pump["axis"] == "X" else 50
        pump["dispense_direction"] = -1 if pump["axis"] == "Y" else 1
    profile["device_map"]["analog_inputs"] = [
        {"name": "temperature", "source": "T0"},
        {"name": "unavailable_adc", "source": "ADC0", "quantity": "analog", "units": "counts"},
    ]
    assert client.put("/api/config/profiles/calibrated_profile", json=profile).status_code == 200
    with TestClient(
        create_app(AppSettings(mock=True, profile="calibrated_profile", log_level="WARNING"))
    ) as active:
        procedure = {
            "name": "calibrated_run",
            "steps": [
                {"op": "home", "axes": ["X", "Y"]},
                {
                    "op": "pump_multi",
                    "pumps": {
                        "sample_pump": {"volume_ul": 25, "flow_ul_min": 25},
                        "sheath_pump": {"volume_ul": 100, "flow_ul_min": 100},
                    },
                },
                {"op": "read_sensors"},
            ],
        }
        assert active.put("/api/procedures/calibrated_run", json=procedure).status_code == 200
        response = active.post("/api/procedures/calibrated_run/run", json={})
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = active.get("/api/procedures/status").json()
            if not status["running"]:
                break
            time.sleep(0.01)
        assert status["error"] is None and not status["running"]
        assert active.get("/api/state").json()["positions"]["X"] == 1
        assert active.get("/api/state").json()["positions"]["Y"] == -2
        sensors = status["results"][-1]["result"]["values"]
        assert sensors[0]["value"] == 25
        assert sensors[1]["value"] is None and sensors[1]["available"] is False


def test_console_rejection_is_nonmutating(client):
    before = client.get("/api/workbench/report").json()["serial_trace"]
    for command in ["G1 X1", "M114\nG1 X1", "N1 M114*42"]:
        assert client.post("/api/workbench/console", json={"command": command}).status_code == 400
    assert client.get("/api/workbench/report").json()["serial_trace"] == before
    assert client.get("/api/state").json()["state"] == "connected_idle"
