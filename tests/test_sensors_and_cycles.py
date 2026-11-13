from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from flowengine.app import create_app
from flowengine.config import AppSettings
from flowengine.errors import ProcedureError
from flowengine.hardware.sensors import parse_sensor_report
from flowengine.procedures import expand_procedure
from flowengine.schemas import Procedure
from flowengine.schemas.device import AnalogInput


def test_parse_m105_separates_temperature_target_adc_and_heater_power():
    result = parse_sensor_report([
        "ok T:25.0 /200.0 (512.0) T0:26.0 /210.0 (500.5) "
        "T1:-2.5 /0 (720) B:24.5 /60 (520) @:127 B@:10 ADC0:1234 P:2e1 /0 (51)",
    ])
    assert result == {"T": 25, "T.adc": 512, "T0": 26, "T0.adc": 500.5,
                      "T1": -2.5, "T1.adc": 720, "B": 24.5, "B.adc": 520,
                      "ADC0": 1234, "P": 20, "P.adc": 51}
    assert parse_sensor_report(["ok T:25 /0 B:24 /0"]) == {"T": 25, "B": 24}
    assert parse_sensor_report(["ok ADC0:1e999"]) == {}


@pytest.mark.parametrize("source", ["T.adc", "T0.adc", "B.adc", "P", "M.adc", "ADC2"])
def test_adc_sources_are_validated(source):
    assert AnalogInput(name="signal", source=source).source == source


@pytest.mark.parametrize("source", ["PA0", "ADC0.adc", "T0\nG1 X1", "T0.bad"])
def test_pins_or_commands_cannot_be_sensor_sources(source):
    with pytest.raises(ValidationError):
        AnalogInput(name="signal", source=source)


@pytest.fixture
def sensor_client(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    with TestClient(create_app(AppSettings(mock=True, log_level="WARNING"))) as client:
        data = {"name": "sensors_bench", **client.get("/api/config").json()}
        data["device_map"]["analog_inputs"] = [
            {"name": "temperature", "source": "T0"},
            {"name": "signal", "source": "T0.adc", "quantity": "analog", "units": "V", "scale": .01, "offset": -1},
            {"name": "not_reported", "source": "ADC7", "quantity": "analog", "units": "counts"},
        ]
        assert client.put("/api/config/profiles/sensors_bench", json=data).status_code == 200
    with TestClient(create_app(AppSettings(mock=True, profile="sensors_bench", log_level="WARNING"))) as client:
        yield client


def test_sensor_page_is_passive_and_reads_are_readonly(sensor_client):
    client = sensor_client
    ctx = client.app.state.ctx
    before = len(ctx.transport.commands)
    assert "sensor-start" in client.get("/sensors").text
    assert len(ctx.transport.commands) == before
    report = client.get("/api/workbench/sensors").json()
    assert ctx.transport.commands[before:] == ["M105"]
    assert report["transport"] == "mock" and report["profile"] == "sensors_bench"
    assert report["timestamp"]
    assert report["reported_sources"]["T0.adc"] == 512
    signal = report["values"][1]
    assert signal["raw_value"] == 512 and signal["value"] == pytest.approx(4.12)
    assert report["values"][2]["available"] is False
    assert report["values"][2]["value"] is None


def test_monitor_never_reads_during_manual_session(sensor_client):
    ctx = sensor_client.app.state.ctx
    ctx.sender.manual_session = {"axis": "X"}
    before = list(ctx.transport.commands)
    assert sensor_client.get("/api/workbench/sensors").status_code == 409
    assert ctx.transport.commands == before
    ctx.sender.manual_session = None


def test_overflow_in_scale_is_reported_as_unavailable(sensor_client):
    ctx = sensor_client.app.state.ctx
    ctx.sender._map = ctx.device_map.model_copy(update={"analog_inputs": [
        AnalogInput(name="overflow", source="T0.adc", scale=1e308),
    ]})
    response = sensor_client.get("/api/workbench/sensors")
    assert response.status_code == 200
    value = response.json()["values"][0]
    assert value["raw_value"] == 512 and value["value"] is None
    assert value["available"] is False and "scale" in value["reason"]


def test_readonly_run_does_not_enable_motion_or_restore_parking(sensor_client, monkeypatch):
    client = sensor_client
    ctx = client.app.state.ctx
    save(client, {"name": "readonly", "steps": [{"op": "read_sensors"}]})
    save(client, {"name": "moving", "steps": [{"op": "move", "axis": "X", "by": 1}]})
    import flowengine.parking as parking

    async def forbidden_restore(*args):
        raise AssertionError("read-only run must not restore parking")

    monkeypatch.setattr(parking, "restore_parking", forbidden_restore)
    with monkeypatch.context() as patch:
        patch.setattr(type(ctx.transport), "name", "marlin")
        assert not ctx.runtime.motion.enabled
        before = len(ctx.transport.commands)
        assert client.post("/api/procedures/readonly/run", json={}).status_code == 200
        client.portal.call(wait_done, ctx)
        assert ctx.runner.status["error"] is None
        assert ctx.transport.commands[before:] == ["M105"]
        assert client.post("/api/procedures/readonly/steps/1/run", json={}).status_code == 200
        response = client.post("/api/procedures/moving/run", json={})
        assert response.status_code == 409 and "interlock" in response.text


async def wait_done(ctx):
    for _ in range(300):
        if not ctx.runner.status["running"]:
            return
        await asyncio.sleep(.01)
    raise AssertionError("procedure did not finish")


def save(client, procedure):
    response = client.put("/api/procedures/" + procedure["name"], json=procedure)
    assert response.status_code == 200, response.text


def test_nested_cycles_capture_every_sample_and_persist_to_profile(sensor_client):
    client = sensor_client
    ctx = client.app.state.ctx
    save(client, {"name": "atom", "parameters": {"message": {"type": "string", "default": "hello"}},
                  "steps": [{"op": "read_sensors", "names": ["signal"], "samples": 2, "interval_s": .1, "require_available": True},
                            {"op": "log", "message": "${message}"}]})
    save(client, {"name": "cycle", "steps": [{"op": "repeat", "procedure": "atom", "times": 2, "parameters": {"message": "child"}}]})
    save(client, {"name": "experiment", "parameters": {"cycles": {"type": "integer", "default": 2}},
                  "steps": [{"op": "repeat", "procedure": "cycle", "times": "${cycles}"}]})
    entries = client.get("/api/procedures").json()
    assert next(p for p in entries if p["name"] == "experiment")["composite"]
    assert next(p for p in entries if p["name"] == "atom")["parameter_definitions"]["message"]["type"] == "string"
    before = len(ctx.transport.commands)
    report = client.post("/api/procedures/experiment/preview", json={}).json()
    assert not report["execution_issues"] and len(report["steps"]) == 8
    assert "repeat 2/2" in report["step_paths"][-1]
    assert len(ctx.transport.commands) == before
    response = client.post("/api/procedures/experiment/run", json={})
    assert response.status_code == 200, response.text
    client.portal.call(wait_done, ctx)
    assert ctx.runner.status["error"] is None
    rows = ctx.runner.status["results"]
    assert len(rows) == 8 and all(r["result"]["values"][0]["name"] == "signal" for r in rows)
    assert ctx.transport.commands[before:] == ["M105"] * 8
    saved = client.get("/api/config/profiles/sensors_bench").json()
    history = saved["commissioning"][-1]
    assert history["kind"] == "procedure_run"
    assert len(history["data"]["status"]["results"]) == 8


def test_missing_required_source_stops_before_later_motion(sensor_client):
    client = sensor_client
    ctx = client.app.state.ctx
    save(client, {"name": "required", "steps": [
        {"op": "read_sensors", "names": ["signal"], "require_available": True},
        {"op": "read_sensors", "names": ["not_reported"], "require_available": True},
        {"op": "log", "message": "must not reach"},
    ]})
    before = len(ctx.transport.commands)
    assert client.post("/api/procedures/required/run", json={}).status_code == 200
    client.portal.call(wait_done, ctx)
    assert "not_reported" in ctx.runner.status["error"]
    assert ctx.runner.status["step"] == 2
    assert len(ctx.runner.status["results"]) == 1
    assert ctx.transport.commands[before:] == ["M105", "M105"]


def test_unknown_sensor_is_rejected_before_any_command(sensor_client):
    save(sensor_client, {"name": "bad_sensor", "steps": [{"op": "read_sensors", "names": ["unknown"]}]})
    ctx = sensor_client.app.state.ctx
    before = list(ctx.transport.commands)
    response = sensor_client.post("/api/procedures/bad_sensor/run", json={})
    assert response.status_code == 409
    assert ctx.transport.commands == before


def test_shipped_nested_example_previews_without_commands(sensor_client):
    ctx = sensor_client.app.state.ctx
    before = list(ctx.transport.commands)
    response = sensor_client.post("/api/procedures/example_sensor_experiment/preview", json={})
    assert response.status_code == 200, response.text
    report = response.json()
    assert len(report["steps"]) == 14
    assert not report["execution_issues"] and not report["readiness_issues"]
    assert sum(s["op"] == "read_sensors" for s in report["steps"]) == 6
    assert ctx.transport.commands == before


def test_abort_series_preserves_completed_samples(sensor_client):
    client = sensor_client
    ctx = client.app.state.ctx
    save(client, {"name": "long_series", "steps": [{"op": "read_sensors", "samples": 1000, "interval_s": 1}]})
    assert client.post("/api/procedures/long_series/run", json={}).status_code == 200

    async def first_sample():
        for _ in range(100):
            if ctx.runner.status["results"]:
                return
            await asyncio.sleep(.01)
        raise AssertionError("no sample")

    client.portal.call(first_sample)
    assert client.get("/api/workbench/sensors").status_code == 409
    assert client.post("/api/procedures/abort").status_code == 200
    assert not ctx.runner.status["running"]
    assert ctx.runner.status["results"]
    assert "aborted" in ctx.runner.status["error"]


def test_expansion_budget_also_bounds_nested_empty_procedures():
    empty = Procedure(name="empty", steps=[])
    cycle = Procedure(name="cycle", steps=[{"op": "repeat", "procedure": "empty", "times": 1000}])
    with pytest.raises(ProcedureError, match="calls"):
        expand_procedure(cycle, lambda _: empty, max_steps=10)


def test_measurement_budget_counts_nested_series_before_execution(sensor_client):
    save(sensor_client, {"name": "many", "steps": [{"op": "read_sensors", "samples": 1000}]})
    save(sensor_client, {"name": "too_many", "steps": [{"op": "repeat", "procedure": "many", "times": 11}]})
    ctx = sensor_client.app.state.ctx
    before = list(ctx.transport.commands)
    response = sensor_client.post("/api/procedures/too_many/run", json={})
    assert response.status_code == 409 and "10000" in response.text
    assert ctx.transport.commands == before


@pytest.mark.parametrize("samples,interval", [(0, 1), (1001, 1), (True, 1), (1.5, 1), (1, .01)])
def test_series_limits(samples, interval):
    with pytest.raises(ValidationError):
        Procedure(name="bad", steps=[{"op": "read_sensors", "samples": samples, "interval_s": interval}])
