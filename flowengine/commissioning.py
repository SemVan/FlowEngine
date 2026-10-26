"""Persisted commissioning evidence and transferable instrument configurations."""

# ruff: noqa: RUF001
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from flowengine.config_store import save_yaml
from flowengine.schemas import ConfigurationProfile
from flowengine.schemas.configuration import CommissioningRecord

CHECKS = {
    "wiring": "Проверены драйверы X/Y/A/B/C/U и трубки",
    "firmware_steps": "M92 и пределы скорости сверены с профилем",
    "endstops": "Проверены сигналы X/Y/U и общий вход A/B/C",
    "valve_home": "Три крана по очереди повторяют HOME и отпускание",
    "valve_paths": "Оба положения каждого крана проверены по трубкам",
    "pump_routes": "Проверены разрешённые маршруты всасывания и подачи",
    "dose_validation": "Независимые контрольные дозы X/Y/U в допуске",
    "wash_period": "Проверен период датчика насоса U, либо согласовано отсутствие датчика",
    "startup": "Запуск выполнен по шагам и целиком",
    "wash": "Оба участка промывки проверены с жидкостью",
    "sample_intake": "Забор обоими шприцами проверен",
    "analysis_delivery": "Совместная подача и соотношение расходов проверены",
    "cyclic_measurement": "Полный цикл повторён не менее двух раз",
}


def fingerprint(profile: ConfigurationProfile) -> str:
    payload = {
        "device_map": profile.device_map.model_dump(mode="json"),
        "runtime": profile.runtime.model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def procedures_fingerprint() -> str:
    from flowengine.loaders import list_procedures

    # Record evidence even when an unrelated procedure currently has invalid YAML.
    values = [(p.name, p.read_text(encoding="utf-8")) for p in list_procedures()]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def record(profile, kind, device, notes, data, passed=True):
    item = CommissioningRecord(
        timestamp=datetime.now(UTC),
        kind=kind,
        device=device,
        passed=passed,
        notes=notes,
        data={
            **data,
            "config_fingerprint": fingerprint(profile),
            "procedures_fingerprint": procedures_fingerprint(),
        },
    )
    updated = profile.model_copy(update={"commissioning": [*profile.commissioning, item]})
    path = save_yaml("profiles", profile.name, updated)
    return {
        "ok": True,
        "profile": updated.model_dump(mode="json"),
        "file": str(path),
        "applied": False,
    }
