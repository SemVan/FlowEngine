from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    with TestClient(create_app(AppSettings(mock=True, log_level="WARNING"))) as client:
        yield client


def create_profile(client):
    profile = client.get("/api/commissioning/active").json()["profile"]
    assert profile["commissioning"] == []
    assert client.put("/api/config/profiles/bench_instrument", json=profile).status_code == 200
    return profile


def test_measurements_are_saved_raw_and_apply_requires_new_reference(client):
    assert client.get("/commissioning/guide").status_code == 200
    assert "allowed_routes" in client.get("/commissioning/guide").text
    original = create_profile(client)
    response = client.post(
        "/api/commissioning/profiles/bench_instrument/pump",
        json={
            "name": "sample_pump",
            "dispense_direction": -1,
            "notes": "water, 60 units/min, syringe 1",
            "confirmed": True,
            "measurements": [{"step_pulses": 80, "volume_ul": v} for v in [24, 25, 26]],
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()["profile"]
    assert saved["device_map"]["pumps"][0]["volume_per_unit_ul"] == 25
    assert len(saved["commissioning"][0]["data"]["measurements"]) == 3
    assert not client.get("/api/config").json()["device_map"]["pumps"][0]["calibrated"]
    # Saving a previously opened editor cannot erase the new measurement journal.
    assert client.put("/api/config/profiles/bench_instrument", json=original).status_code == 200
    assert len(client.get("/api/config/profiles/bench_instrument").json()["commissioning"]) == 1
    # Re-save measured hardware settings, then apply without serial writes.
    client.put("/api/config/profiles/bench_instrument", json=saved)
    before = client.get("/api/workbench/report").json()["serial_trace"]
    response = client.post("/api/commissioning/profiles/bench_instrument/apply", json={})
    assert response.status_code == 200, response.text
    assert not any(response.json()["homed"].values())
    assert client.get("/api/workbench/report").json()["serial_trace"] == before
    assert client.get("/api/config").json()["device_map"]["pumps"][0]["volume_per_unit_ul"] == 25


def test_manual_reference_and_procedure_result_persist_without_restoring_homed(client):
    create_profile(client)
    client.post("/api/commissioning/profiles/bench_instrument/apply", json={})
    body = {
        "axis": "U",
        "position": 0,
        "notes": "rotary pump free travel origin",
        "confirmed": True,
    }
    assert (
        client.post("/api/commissioning/reference", json={**body, "confirmed": False}).status_code
        == 400
    )
    assert client.post("/api/commissioning/reference", json=body).status_code == 200
    assert client.get("/api/state").json()["homed"]["U"]
    client.put(
        "/api/procedures/check_reference",
        json={
            "name": "check_reference",
            "draft": True,
            "steps": [{"op": "require_reference", "axes": ["U"]}],
        },
    )
    assert client.post("/api/procedures/check_reference/steps/1/run", json={}).status_code == 200
    saved = client.get("/api/config/profiles/bench_instrument").json()
    assert saved["commissioning"][-1]["kind"] == "procedure_run"
    assert saved["commissioning"][-1]["passed"]
    assert (
        client.post("/api/commissioning/profiles/bench_instrument/apply", json={}).status_code
        == 200
    )
    assert not client.get("/api/state").json()["homed"]["U"]


def test_valve_coordinates_validated_and_saved_but_not_applied(client):
    create_profile(client)
    body = {
        "name": "sample_valve",
        "position_a": 1,
        "position_b": 8,
        "notes": "A tube1-tube2, B tube1-tube3, verified with water",
        "confirmed": True,
    }
    assert (
        client.post(
            "/api/commissioning/profiles/bench_instrument/valve", json={**body, "position_b": -1}
        ).status_code
        == 400
    )
    response = client.post("/api/commissioning/profiles/bench_instrument/valve", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["profile"]["device_map"]["valves"][2]["calibrated"]
    assert not client.get("/api/config").json()["device_map"]["valves"][2]["calibrated"]


def test_bundle_roundtrip_and_dependency_validation_before_writes(client):
    create_profile(client)
    bundle = client.get("/api/commissioning/profiles/bench_instrument/bundle").json()
    bundle["profile"]["name"] = "imported"
    assert client.post("/api/commissioning/import", json=bundle).status_code == 200
    assert client.get("/api/state").json()["profile"] == "default"
    assert client.get("/api/config/profiles/imported").status_code == 200
    broken = {
        **bundle,
        "profile": {**bundle["profile"], "name": "broken"},
        "procedures": [
            {"name": "missing_parent", "steps": [{"op": "call", "procedure": "absent"}]}
        ],
    }
    assert client.post("/api/commissioning/import", json=broken).status_code == 400
    assert client.get("/api/config/profiles/broken").status_code == 404


def test_readiness_and_stale_checks_never_claim_unmeasured_device_ready(client):
    profile = create_profile(client)
    response = client.post(
        "/api/commissioning/profiles/bench_instrument/observations",
        json={"kind": "wiring", "passed": True, "notes": "all drivers physically identified"},
    )
    assert response.status_code == 200
    readiness = client.get("/api/commissioning/profiles/bench_instrument/readiness").json()
    assert not readiness["ready"]
    assert next(c for c in readiness["checks"] if c["key"] == "wiring")["passed"]
    profile["runtime"]["timeouts"]["diagnostics"] += 1
    client.put("/api/config/profiles/bench_instrument", json=profile)
    readiness = client.get("/api/commissioning/profiles/bench_instrument/readiness").json()
    assert next(c for c in readiness["checks"] if c["key"] == "wiring")["stale"]


def test_transport_change_requires_restart_and_apply_is_blocked_while_busy(client):
    profile = create_profile(client)
    profile["runtime"]["transport"]["baud"] = 250000
    client.put("/api/config/profiles/bench_instrument", json=profile)
    assert (
        client.post("/api/commissioning/profiles/bench_instrument/apply", json={}).status_code
        == 409
    )
    assert client.get("/api/state").json()["profile"] == "default"


def test_pump_route_blocks_dose_until_referenced_valve_is_at_verified_position(client):
    profile = create_profile(client)
    profile["device_map"]["pumps"][0].update(
        {
            "calibrated": True,
            "volume_per_unit_ul": 25,
            "allowed_routes": {"dispense": {"wash_valve": ["КП-ПРОКАЧКА"]}},
        }
    )
    profile["device_map"]["valves"][0].update(
        {"calibrated": True, "position_a": 1, "position_b": 2}
    )
    assert client.put("/api/config/profiles/bench_instrument", json=profile).status_code == 200
    assert (
        client.post("/api/commissioning/profiles/bench_instrument/apply", json={}).status_code
        == 200
    )
    for axis, coordinate in [("X", 0), ("A", 2)]:
        client.app.state.ctx.motion.mark_referenced(axis, coordinate)
    procedure = {
        "name": "route_check",
        "draft": True,
        "steps": [{"op": "pump", "name": "sample_pump", "volume_ul": 25, "flow_ul_min": 25}],
    }
    client.put("/api/procedures/route_check", json=procedure)
    before = client.get("/api/workbench/report").json()["serial_trace"]
    result = client.post("/api/procedures/route_check/steps/1/run", json={})
    assert result.status_code == 409
    assert "allowed route" in result.json()["detail"]
    after = client.get("/api/workbench/report").json()["serial_trace"]
    assert not any(
        c["direction"] == "TX" and c["line"].startswith("G1 ") for c in after[len(before) :]
    )
    journal = client.get("/api/config/profiles/bench_instrument").json()["commissioning"]
    assert journal[-1]["kind"] == "procedure_run" and not journal[-1]["passed"]


def test_diagnostic_fault_is_visible_and_profile_apply_cannot_clear_it(client):
    create_profile(client)
    ctx = client.app.state.ctx
    ctx.motion.mark_homed(["X"])
    ctx.queue._aborted.set()  # Simulate the queue after a missing ACK.
    response = client.get("/api/diagnostics/endstops")
    assert response.status_code == 503
    state = client.get("/api/state").json()
    assert state["state"] == "errored" and not state["homed"]["X"]
    assert client.post("/api/commissioning/profiles/bench_instrument/apply", json={}).status_code == 409
