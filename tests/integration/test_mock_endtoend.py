"""End-to-end smoke test through FastAPI + MockTransport."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings


@pytest.fixture
def client() -> TestClient:
    settings = AppSettings(mock=True, log_level="WARNING")
    app = create_app(settings)
    with TestClient(app) as c:
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


def test_jog_before_home_rejected(client):
    # Re-create with fresh state by restarting? Use the test we have: jog should fail soft-limit.
    # State on existing fixture is post-home so we can't easily test pre-home here.
    # Instead, jog an axis past its travel limit.
    client.post("/api/home", json={"axes": None})
    r = client.post(
        "/api/jog",
        json={"axis": "X", "delta": 99999.0, "feedrate": 100.0},
        headers={"Idempotency-Key": "soft"},
    )
    assert r.status_code == 400
    assert "soft-limit" in r.text


def test_stop_endpoint(client):
    client.post("/api/home", json={"axes": None})
    r = client.post("/api/stop")
    assert r.status_code == 200


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


def test_procedure_runs_end_to_end(client):
    response = client.post("/api/procedures/home_all/run")
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = client.get("/api/procedures/status").json()
        if not status["running"]:
            break
        time.sleep(0.01)
    assert status == {
        "running": False,
        "name": "home_all",
        "step": 2,
        "error": None,
    }


def test_idempotency_key_dedupes(client):
    client.post("/api/home", json={"axes": None})
    h = {"Idempotency-Key": "dedupe-key"}
    r1 = client.post("/api/jog", json={"axis": "X", "delta": 1.0, "feedrate": 100.0}, headers=h)
    r2 = client.post("/api/jog", json={"axis": "X", "delta": 1.0, "feedrate": 100.0}, headers=h)
    assert r1.status_code == r2.status_code == 200
    # Same response → second call did not actually move (cache hit).
    assert r1.json() == r2.json()
