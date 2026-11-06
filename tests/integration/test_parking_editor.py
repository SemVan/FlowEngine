from __future__ import annotations

import asyncio

import pytest
import yaml
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings
from flowengine.config_store import find_saved, load_yaml, save_yaml
from flowengine.parking_store import read_receipt, receipt_path
from flowengine.schemas import ConfigurationProfile, RuntimeParams
from flowengine.schemas.device import DeviceMap
from tests.test_syringe_group_home import pair_map


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    data = pair_map().model_dump()
    data["parking"] = {"positions": {"X": 5, "Y": 15}, "feedrates": {"X": 120, "Y": 60}}
    profile = ConfigurationProfile(name="parking_bench", device_map=DeviceMap.model_validate(data), runtime=RuntimeParams())
    save_yaml("profiles", profile.name, profile)
    return profile


def app_for(profile):
    return create_app(AppSettings(mock=True, profile=profile.name, log_level="WARNING"))


def establish(ctx):
    for axis in "XY":
        ctx.transport._positions[axis] = 10
        ctx.transport._counts[axis] = 800
        ctx.motion.mark_referenced(axis, 10)


def test_park_releases_then_next_process_restores_without_home_or_confirmation(configured):
    with TestClient(app_for(configured)) as client:
        ctx = client.app.state.ctx
        establish(ctx)
        response = client.post("/api/session/park")
        assert response.status_code == 200, response.text
        assert response.json()["parked"] and response.json()["released"]
        assert not any(ctx.motion.homed.values())
        assert read_receipt(ctx)["positions"] == {"X": 5, "Y": 15}
        assert any(c.startswith("M18") for c in ctx.transport.commands)
    with TestClient(app_for(configured)) as client:
        ctx = client.app.state.ctx
        assert all(ctx.motion.homed.values())
        assert ctx.motion.positions == {"X": 5, "Y": 15}
        assert client.get("/api/session/parking").json()["reference_source"] == "saved_parking"
        assert any(c.startswith("G92 ") for c in ctx.transport.commands)
        assert not any(c.startswith(("G1 ", "G28")) for c in ctx.transport.commands)
        assert read_receipt(ctx) is None


def test_run_after_park_restores_once_and_repeated_cycles_keep_references(configured):
    with TestClient(app_for(configured)) as client:
        ctx = client.app.state.ctx
        establish(ctx)
        assert client.post("/api/session/park").status_code == 200
        procedure = {"name": "after_park", "steps": [{"op": "move_multi", "axes": {"X": 1, "Y": -1},
            "relative": True, "axis_feedrates": {"X": 120, "Y": 60}}]}
        assert client.put("/api/procedures/after_park", json=procedure).status_code == 200
        before = len(ctx.transport.commands)
        preview = client.post("/api/procedures/after_park/preview", json={})
        assert not preview.json()["readiness_issues"]
        assert len(ctx.transport.commands) == before
        for _ in range(2):
            response = client.post("/api/procedures/after_park/run", json={})
            assert response.status_code == 200, response.text
            async def done():
                for _ in range(100):
                    if not ctx.runner.status["running"]:
                        return
                    await asyncio.sleep(.02)
                raise AssertionError("procedure did not finish")
            client.portal.call(done)
            assert ctx.runner.status["error"] is None
        commands = ctx.transport.commands[before:]
        assert sum(c.startswith("G92 ") for c in commands) == 1
        assert not any(c.startswith("G28") for c in commands)
        assert all(ctx.motion.homed.values())


@pytest.mark.parametrize("fault", ["stop", "write", "geometry", "position", "profile"])
def test_stale_or_interrupted_parking_is_never_restored(configured, fault):
    with TestClient(app_for(configured)) as client:
        ctx = client.app.state.ctx
        establish(ctx)
        assert client.post("/api/session/park").status_code == 200
        if fault in {"stop", "write"}:
            ctx.queue._record("TX", "M410" if fault == "stop" else "G1 X6")
        elif fault == "geometry":
            data = ctx.device_map.model_dump(); data["axes"][0]["steps_per_unit"] = 160
            ctx.device_map = DeviceMap.model_validate(data)
        elif fault == "position":
            data = ctx.device_map.model_dump(); data["parking"]["positions"]["X"] = 6
            ctx.device_map = DeviceMap.model_validate(data)
        else:
            ctx.profile_name = "other"
        before = list(ctx.transport.commands)
        result = client.post("/api/session/start")
        assert result.status_code == 200 and not result.json()["restored"]
        assert ctx.transport.commands == before
        assert not any(ctx.motion.homed.values())


def test_failed_parking_does_not_write_receipt(configured, monkeypatch):
    with TestClient(app_for(configured)) as client:
        ctx = client.app.state.ctx
        establish(ctx)
        async def failure(*args, **kwargs):
            raise ValueError("test interrupted parking")
        monkeypatch.setattr(ctx.sender, "move_independent", failure)
        response = client.post("/api/session/park")
        assert response.status_code == 400
        assert not receipt_path("mock").exists()
        assert not any(ctx.motion.homed.values())


def test_capture_saves_actual_positions_without_motion_and_applies_to_session(configured):
    with TestClient(app_for(configured)) as client:
        ctx = client.app.state.ctx
        before = len(ctx.transport.commands)
        assert client.post("/api/session/parking/capture").status_code == 409
        assert len(ctx.transport.commands) == before
        establish(ctx)
        response = client.post("/api/session/parking/capture")
        assert response.status_code == 200, response.text
        stored = load_yaml(find_saved("profiles", configured.name), ConfigurationProfile)
        assert stored.device_map.parking.positions == {"X": 10, "Y": 10}
        assert ctx.device_map == stored.device_map
        assert not any(c.startswith(("G1 ", "G28", "G92")) for c in ctx.transport.commands[before:])


def test_yaml_editor_keeps_comments_nulls_and_history_and_detects_conflicts(configured):
    with TestClient(app_for(configured)) as client:
        ctx = client.app.state.ctx
        path = find_saved("profiles", configured.name)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["commissioning"] = [{"timestamp":"2026-11-03T10:00:00Z", "kind":"observation", "device":"X", "passed":True,
            "notes":"keep history", "data":{"serial_trace":[{"line":"G1 X5", "direction":"TX"}]}}]
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        before = list(ctx.transport.commands)
        source = client.get(f"/api/config/text/profiles/{configured.name}").json()
        assert "commissioning:" not in source["text"]
        text = "# скорость для инженера\n" + source["text"].replace("feedrate_cap: 3000.0", "feedrate_cap: 2500.0")
        url = f"/api/config/text/profiles/{configured.name}"
        response = client.put(url, json={"text": text, "revision":source["revision"]})
        assert response.status_code == 200, response.text
        assert "# скорость для инженера" in response.json()["text"]
        saved = load_yaml(path, ConfigurationProfile)
        assert len(saved.commissioning) == 1 and saved.commissioning[0].notes == "keep history"
        assert client.put(url, json={"text":source["text"], "revision":source["revision"]}).status_code == 409
        malformed = {"text":"name: [broken", "revision":response.json()["revision"]}
        assert client.put(url, json=malformed).status_code == 400
        assert ctx.transport.commands == before
        assert saved.runtime.motion.feedrate_cap == 2500
        saved = saved.model_copy(update={"commissioning":[*saved.commissioning, saved.commissioning[0]]})
        save_yaml("profiles", saved.name, saved)
        assert "# скорость для инженера" in path.read_text(encoding="utf-8")
        assert len(load_yaml(path, ConfigurationProfile).commissioning) == 2


def test_yaml_apply_speeds_and_parking_preserves_references_without_serial_writes(configured):
    with TestClient(app_for(configured)) as client:
        ctx = client.app.state.ctx
        establish(ctx)
        data = configured.model_dump(mode="json")
        data["device_map"]["parking"]["positions"]["X"] = 6
        data["runtime"]["motion"]["feedrate_cap"] = 1000
        client.put(f"/api/config/profiles/{configured.name}", json=data)
        before = list(ctx.transport.commands)
        response = client.post(f"/api/config/editor/apply/{configured.name}")
        assert response.status_code == 200, response.text
        assert response.json()["references_preserved"]
        assert all(ctx.motion.homed.values())
        assert ctx.device_map.parking.positions["X"] == 6
        assert ctx.motion.max_feedrate("X") == 1000
        assert ctx.transport.commands == before


def test_terminal_reads_queue_with_cursors_and_never_sends_commands(configured):
    with TestClient(app_for(configured)) as client:
        ctx = client.app.state.ctx
        before = list(ctx.transport.commands)
        response = client.get("/api/diagnostics/trace").json()
        assert response["reset"] and response["entries"]
        after = client.get("/api/diagnostics/trace", params={"trace_id":response["trace_id"], "after":response["latest"]}).json()
        assert not after["entries"] and not after["reset"]
        for i in range(1005):
            ctx.queue._record("RX", f"ok {i}")
        overflow = client.get("/api/diagnostics/trace", params={"trace_id":response["trace_id"], "after":response["latest"]}).json()
        assert overflow["gap"] and len(overflow["entries"]) == 1000
        assert client.get("/config/editor").status_code == 200
        assert client.get("/terminal").status_code == 200
        assert ctx.transport.commands == before


def test_parking_rejects_missing_axes_and_endstop_positions(configured):
    for positions in ({"X":5}, {"X":0,"Y":15}, {"X":5,"Y":20}, {"X":5,"Y":15,"U":0}):
        data = configured.device_map.model_dump(); data["parking"]["positions"] = positions
        with pytest.raises(ValueError):
            DeviceMap.model_validate(data)


def test_valve_parking_can_return_more_than_twenty_units_without_rehoming(configured):
    from tests.test_valves import valve_map
    data = valve_map().model_dump()
    data["parking"] = {"positions": {"A":-1,"B":-2,"C":-3}, "feedrates": {"A":120,"B":120,"C":120}}
    profile = configured.model_copy(update={"device_map":DeviceMap.model_validate(data)})
    save_yaml("profiles", profile.name, profile)
    with TestClient(app_for(profile)) as client:
        ctx = client.app.state.ctx
        for axis in "ABC":
            ctx.motion.mark_referenced(axis, -30)
            ctx.transport._positions[axis] = -30
            ctx.transport.valve_mechanical[axis] = -30
        before = len(ctx.transport.commands)
        response = client.post("/api/session/park")
        assert response.status_code == 200, response.text
        assert response.json()["parked"]
        commands = ctx.transport.commands[before:]
        moves = [c for c in commands if c.startswith("G1 ")]
        assert len(moves) == 3
        assert "A-1.000000" in moves[0] and "B-2.000000" in moves[1] and "C-3.000000" in moves[2]
        assert not any(c.startswith(("G28", "G92")) for c in commands)
