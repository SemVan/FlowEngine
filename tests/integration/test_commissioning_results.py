from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings
from flowengine.errors import ControllerError
from flowengine.state import State


@pytest.fixture
def bench(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path / "config"))
    app = create_app(AppSettings(mock=True, log_level="WARNING"))
    with TestClient(app) as client:
        yield client, app.state.ctx


def test_opening_sheet_and_cached_snapshot_send_no_commands(bench):
    client, ctx = bench
    before = list(ctx.transport.commands)
    assert client.get("/commissioning/results").status_code == 200
    snapshot = client.get("/api/commissioning/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["profile"] == "default"
    assert snapshot.json()["valves"]["shared_valve_endstop"]["state"] == "UNKNOWN"
    assert ctx.transport.commands == before


def test_diagnostics_use_only_read_commands_without_changing_profile_or_reference(bench):
    client, ctx = bench
    device = ctx.device_map.model_dump()
    runtime = ctx.runtime.model_dump()
    reference = ctx.motion.homed
    before = len(ctx.transport.commands)
    response = client.post("/api/commissioning/diagnostics")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["complete"] is True
    assert ctx.transport.commands[before:] == ["M115", "M503", "M119", "M114", "M211"]
    assert "z_min" in data["readings"]["M119"]["endstops"]
    assert data["context"]["transport"] == "mock"
    assert ctx.motion.homed == reference
    assert ctx.device_map.model_dump() == device
    assert ctx.runtime.model_dump() == runtime


def test_single_input_capture_sends_only_m119(bench):
    client, ctx = bench
    before = len(ctx.transport.commands)
    response = client.post("/api/commissioning/endstops")
    assert response.status_code == 200, response.text
    assert response.json()["complete"] is True
    assert list(response.json()["readings"]) == ["M119"]
    assert ctx.transport.commands[before:] == ["M119"]


@pytest.mark.parametrize("busy_state", [State.MOVING, State.HOMING, State.ERRORED])
def test_busy_board_is_not_read_but_cached_snapshot_is_available(bench, busy_state):
    client, ctx = bench
    client.portal.call(ctx.state.transition, busy_state)
    before = list(ctx.transport.commands)
    assert client.post("/api/commissioning/diagnostics").status_code == 409
    assert client.post("/api/commissioning/endstops").status_code == 409
    assert client.get("/api/commissioning/snapshot").status_code == 200
    assert ctx.transport.commands == before


def test_partial_diagnostics_preserve_readings_and_stop_at_error(bench, monkeypatch):
    client, ctx = bench
    read = ctx.sender.read_diagnostic

    async def fail_at_inputs(command):
        if command == "M119":
            raise ControllerError("input read failed")
        return await read(command)

    monkeypatch.setattr(ctx.sender, "read_diagnostic", fail_at_inputs)
    before = len(ctx.transport.commands)
    data = client.post("/api/commissioning/diagnostics").json()
    assert data["complete"] is False
    assert list(data["readings"]) == ["M115", "M503"]
    assert data["errors"] == ["M119: input read failed"]
    assert ctx.transport.commands[before:] == ["M115", "M503"]


def test_profile_change_during_read_does_not_mix_profiles(bench, monkeypatch):
    client, ctx = bench
    original = ctx.sender.read_firmware
    instrument = ctx.device_map.instrument_id

    async def change_profile():
        reply = await original()
        ctx.device_map = ctx.device_map.model_copy(update={"instrument_id": "changed"})
        return reply

    monkeypatch.setattr(ctx.sender, "read_firmware", change_profile)
    before = len(ctx.transport.commands)
    data = client.post("/api/commissioning/diagnostics").json()
    assert data["complete"] is False
    assert data["context"]["instrument"] == instrument
    assert ctx.transport.commands[before:] == ["M115"]
