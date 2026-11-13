import pytest
import yaml

from flowengine.config_store import load_yaml, save_yaml
from flowengine.schemas import ConfigurationProfile, RuntimeParams
from tests.test_valves import valve_map


@pytest.mark.parametrize("timeout", [None, 12.5, 30.0])
def test_valve_timeout_survives_profile_save_and_reload(tmp_path, monkeypatch, timeout):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    device = valve_map()
    device = device.model_copy(update={"valves": [
        valve.model_copy(update={"home_timeout_s": timeout}) for valve in device.valves
    ]})
    profile = ConfigurationProfile(name="bench", device_map=device, runtime=RuntimeParams())
    path = save_yaml("profiles", "bench", profile)
    restored = load_yaml(path, ConfigurationProfile)
    save_yaml("profiles", "bench", restored.model_copy(update={"description": "измерение сохранено"}))
    again = load_yaml(path, ConfigurationProfile)
    assert all(valve.home_timeout_s == timeout for valve in again.device_map.valves)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert all(valve["home_timeout_s"] == timeout for valve in raw["device_map"]["valves"])
