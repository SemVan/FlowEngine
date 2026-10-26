"""End-to-end smoke test through FastAPI + MockTransport."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings
from tests.conftest import use_firmware_home_axes


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    settings = AppSettings(mock=True, log_level="WARNING")
    app = create_app(settings)
    with TestClient(app) as c:
        use_firmware_home_axes(app.state.ctx)
        device = c.get("/api/config").json()["device_map"]
        configured = c.put("/api/valves/config", json={"valves": device["valves"],
                            "shared_valve_endstop": {"channel": "z_min"}})
        assert configured.status_code == 200, configured.text
        yield c


def test_state_reports_connected(client):
    r = client.get("/api/state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] in {"connected_idle", "errored"}
    assert body["transport"] == "mock"


def test_home_all_and_jog(client):
    # Home everything first.
    r = client.post("/api/home", json={"axes": None})
    assert r.status_code == 200, r.text

    # Jog X +1.0 mm.
    r = client.post(
        "/api/jog",
        json={"axis": "X", "delta": 1.0, "feedrate": 100.0},
        headers={"Idempotency-Key": "abc"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["positions"]["X"] == pytest.approx(1.0, abs=0.01)


def test_normal_motion_requires_reference(client):
    assert client.post("/api/stop").status_code == 200
    assert client.post("/api/reconnect").status_code == 200
    r = client.post(
        "/api/jog",
        json={"axis": "X", "delta": 1.0, "feedrate": 100.0},
        headers={"Idempotency-Key": "unreferenced"},
    )
    assert r.status_code in {400, 409}
    assert "refer" in r.text.lower() or "state" in r.text.lower()


def test_stop_endpoint(client):
    client.post("/api/home", json={"axes": None})
    r = client.post("/api/stop")
    assert r.status_code == 200


def test_core_reconnect_after_stop_without_desktop_wrapper(client):
    client.post("/api/home", json={"axes": None})
    assert client.post("/api/stop").status_code == 200
    assert client.get("/api/state").json()["state"] == "errored"

    r = client.post("/api/reconnect")
    assert r.status_code == 200, r.text
    assert r.json()["references_lost"] is True
    state = client.get("/api/state").json()
    assert state["state"] == "connected_idle"
    assert not any(state["homed"].values())
    assert client.get("/api/diagnostics/firmware").status_code == 200


def test_config_endpoint(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert "device_map" in body and "runtime" in body


def test_schemas_endpoint(client):
    r = client.get("/api/schemas")
    assert r.status_code == 200
    s = r.json()
    assert "DeviceMap" in s


def test_diagnostics_firmware(client):
    r = client.get("/api/diagnostics/firmware")
    assert r.status_code == 200
    body = r.json()
    assert "MockMarlin" in body["raw"]
    assert "CHECKSUM" in body["features"]


def test_procedures_list(client):
    r = client.get("/api/procedures")
    assert r.status_code == 200
    procs = r.json()
    assert isinstance(procs, list)
    # At least one of the shipped sample procedures should parse cleanly.
    assert any(p.get("ok") for p in procs)
    assert any(p.get("draft") for p in procs)


def test_save_and_reload_procedure(client):
    procedure = {
        "name": "operator_copy",
        "description": "editable copy",
        "version": 1,
        "draft": True,
        "parameters": {"distance": {"type": "number", "default": 1.0, "minimum": 0.1}},
        "steps": [{"op": "move", "axis": "X", "by": "${distance}"}],
    }
    response = client.put("/api/procedures/operator_copy", json=procedure)
    assert response.status_code == 200, response.text
    assert client.get("/api/procedures/operator_copy").json()["description"] == "editable copy"
    response = client.post("/api/procedures/operator_copy/run", json={})
    assert response.status_code == 409
    assert "draft" in response.json()["detail"]

    preview = client.post("/api/procedures/operator_copy/preview", json={})
    assert preview.status_code == 200, preview.text
    assert preview.json()["draft"] is True
    assert preview.json()["steps"][0]["by"] == 1.0

    home = client.post("/api/home", json={"axes": ["X"]})
    assert home.status_code == 200, home.text
    response = client.post("/api/procedures/operator_copy/steps/1/run", json={"distance": 2.0})
    assert response.status_code == 200, response.text
    assert response.json()["single_step"] is True
    assert response.json()["step"] == 1

    response = client.post("/api/procedures/operator_copy/steps/2/run", json={})
    assert response.status_code == 409
    assert "between 1 and 1" in response.json()["detail"]


def test_nested_draft_can_be_previewed_and_run_as_one_selected_step(client):
    child = {
        "name": "wash_pump_test",
        "description": "reusable child",
        "version": 1,
        "draft": True,
        "parameters": {"message": {"type": "string"}},
        "steps": [{"op": "log", "message": "${message}"}],
    }
    parent = {
        "name": "analysis_with_wash_test",
        "description": "composite draft",
        "version": 1,
        "draft": True,
        "parameters": {"wash_message": {"type": "string", "default": "wash done"}},
        "steps": [
            {
                "op": "call",
                "procedure": "wash_pump_test",
                "parameters": {"message": "${wash_message}"},
            }
        ],
    }
    assert client.put("/api/procedures/wash_pump_test", json=child).status_code == 200
    assert client.put("/api/procedures/analysis_with_wash_test", json=parent).status_code == 200

    preview = client.post("/api/procedures/analysis_with_wash_test/preview", json={})
    assert preview.status_code == 200, preview.text
    assert preview.json()["steps"] == [{"op": "log", "message": "wash done"}]
    assert preview.json()["step_paths"] == [
        "analysis_with_wash_test step 1 → wash_pump_test step 1"
    ]

    full_run = client.post("/api/procedures/analysis_with_wash_test/run", json={})
    assert full_run.status_code == 409
    assert "draft" in full_run.json()["detail"]

    selected = client.post(
        "/api/procedures/analysis_with_wash_test/steps/1/run",
        json={"wash_message": "commissioned"},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["operation"] == "call"
    assert "wash_pump_test step 1" in selected.json()["step_path"]


def test_profiles_and_racks_can_be_saved(client):
    active = client.get("/api/config").json()
    profile = {
        "name": "bench_copy",
        "description": "test profile",
        **active,
        "rack": None,
    }
    response = client.put("/api/config/profiles/bench_copy", json=profile)
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is True
    assert client.get("/api/config/profiles/bench_copy").json()["name"] == "bench_copy"

    profiled_app = create_app(AppSettings(mock=True, profile="bench_copy", log_level="WARNING"))
    with TestClient(profiled_app) as profiled_client:
        loaded = profiled_client.get("/api/config").json()
        assert loaded["device_map"]["instrument_id"] == active["device_map"]["instrument_id"]

    rack = client.get("/api/config/racks/example_8x12").json()
    rack["name"] = "my_rack"
    response = client.put("/api/config/racks/my_rack", json=rack)
    assert response.status_code == 200, response.text
    assert client.get("/api/config/racks/my_rack").json()["columns"] == 12
    plan = client.post("/api/config/racks/my_rack/plan", json=["A1", "B2"])
    assert plan.status_code == 200, plan.text
    assert plan.json()["positions"] == [
        {"cell": "A1", "x": 0.0, "y": 0.0, "z": 0.0},
        {"cell": "B2", "x": 9.0, "y": 9.0, "z": 0.0},
    ]
    assert plan.json()["executable"] is False


def test_procedure_runs_end_to_end(client):
    client.put("/api/procedures/smoke_home", json={"name": "smoke_home", "steps": [
        {"op": "home", "axes": ["X"]}, {"op": "log", "message": "done"}]})
    response = client.post("/api/procedures/smoke_home/run")
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get("/api/procedures/status").json()
        if not status["running"]:
            break
        time.sleep(0.01)
    assert not status["running"] and status["error"] is None
    assert status["name"] == "smoke_home" and status["step"] == 2
    assert status["results"][0]["result"]["X"]["strategy"] == "g28"


def test_idempotency_key_dedupes(client):
    client.post("/api/home", json={"axes": None})
    h = {"Idempotency-Key": "dedupe-key"}
    r1 = client.post("/api/jog", json={"axis": "X", "delta": 1.0, "feedrate": 100.0}, headers=h)
    r2 = client.post("/api/jog", json={"axis": "X", "delta": 1.0, "feedrate": 100.0}, headers=h)
    assert r1.status_code == r2.status_code == 200
    # Same response → second call did not actually move (cache hit).
    assert r1.json() == r2.json()


def test_async_home_job_returns_immediately_and_completes(client, monkeypatch):
    import asyncio
    import time

    sender = client.app.state.ctx.sender
    original_home = sender.home

    async def delayed_home(axes=None, progress=None):
        await asyncio.sleep(0.15)
        return await original_home(axes, progress=progress)

    monkeypatch.setattr(sender, "home", delayed_home)
    started_at = time.monotonic()
    response = client.post("/api/home/start", json={"axes": ["X"]})
    elapsed = time.monotonic() - started_at
    assert response.status_code == 200, response.text
    assert elapsed < 0.12
    job_id = response.json()["job_id"]
    assert response.json()["state"] in {"starting", "running"}

    deadline = time.monotonic() + 2.0
    status = None
    while time.monotonic() < deadline:
        status = client.get("/api/home/status", params={"job_id": job_id})
        assert status.status_code == 200, status.text
        if status.json()["state"] == "completed":
            break
        time.sleep(0.03)
    assert status is not None
    assert status.json()["state"] == "completed", status.text
    assert status.json()["result"]["homed"]["X"] is True
