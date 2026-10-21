from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings
from flowengine.config_store import find_saved, load_yaml
from flowengine.loaders import load_device_map, load_modes, load_runtime
from flowengine.schemas import ConfigurationProfile


def test_legacy_parallel_endstop_profile_is_migrated(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    profile = ConfigurationProfile(
        name="legacy",
        device_map=load_device_map(),
        runtime=load_runtime(),
        modes=load_modes(),
    ).model_dump(mode="json")
    x = next(axis for axis in profile["device_map"]["axes"] if axis["marlin_axis"] == "X")
    x["travel"] = 100.0
    x["manual_setup"] = {
        "minimum": 0.0,
        "maximum": 100.0,
        "positive_toward_motor": True,
        "home_edge": "far",
    }
    x["endstop_observations"] = {
        "channel": "x_min",
        "motor": {"position": 100.0, "approach_direction": 1},
        "far": {"position": 0.0, "approach_direction": -1},
        "positive_toward_motor": True,
        "recorded_at": "2026-10-01T00:00:00+00:00",
        "source": "operator_contact",
    }
    path = tmp_path / "profiles" / "legacy.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding="utf-8")

    loaded = load_yaml(path, ConfigurationProfile)
    axis = loaded.device_map.axis("X")
    assert axis.travel is None
    assert axis.axis_calibration is not None
    assert axis.axis_calibration.travel == 100.0
    assert axis.axis_calibration.positive_toward_motor is True
    assert axis.axis_calibration.home_side == "far"


def test_browser_mode_can_save_apply_and_remember_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    app = create_app(AppSettings(mock=True, log_level="WARNING"))
    with TestClient(app) as client:
        profile = client.get("/api/config/default-profile").json()
        profile["name"] = "qa_test"
        profile["device_map"]["axes"][0]["name"] = "Applied X"
        assert client.put("/api/config/profiles/qa_test", json=profile).status_code == 200

        response = client.post("/api/config/apply", json={"name": "qa_test"})
        assert response.status_code == 200, response.text
        assert response.json()["references_lost"] is True
        assert client.get("/api/state").json()["profile"] == "qa_test"
        assert client.get("/api/config").json()["device_map"]["axes"][0]["name"] == "Applied X"
        assert (tmp_path / "desktop.json").is_file()
        assert "qa_test" in (tmp_path / "desktop.json").read_text(encoding="utf-8")
        assert load_yaml(find_saved("profiles", "qa_test"), ConfigurationProfile).name == "qa_test"
