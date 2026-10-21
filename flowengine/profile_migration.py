"""читаем старые личные профили в новом формате"""
from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any


_OBSOLETE_AXIS_KEYS = {
    "safe_min",
    "safe_max",
    "manual_range",
    "allowed_min",
    "allowed_max",
    "soft_travel_min",
    "soft_travel_max",
}


def migrate_configuration_profile_data(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    data = copy.deepcopy(value)
    device_map = data.get("device_map")
    if not isinstance(device_map, dict):
        return data
    axes = device_map.get("axes")
    if not isinstance(axes, list):
        return data

    for axis in axes:
        if not isinstance(axis, dict):
            continue
        for key in _OBSOLETE_AXIS_KEYS:
            axis.pop(key, None)

        manual_setup = axis.pop("manual_setup", None)
        observations = axis.pop("endstop_observations", None)
        name = axis.get("marlin_axis")
        is_parallel_xy = (
            name in {"X", "Y"}
            and axis.get("kind") == "syringe_pump"
            and axis.get("endstop_channels_shared", True)
        )

        calibration = axis.get("axis_calibration")
        if isinstance(calibration, dict):
            # старое имя away заменяем на far
            if "away" in calibration and "far" not in calibration:
                calibration["far"] = calibration.pop("away")
            calibration.setdefault("channel", f"{str(name).lower()}_min")
            calibration.setdefault("source", "parallel_endstops")
            if calibration.get("source") not in {None, "parallel_endstops"}:
                calibration["source"] = "parallel_endstops"
            calibration.setdefault("recorded_at", datetime.now(UTC).isoformat())
            calibration.setdefault("measurement_source", "m114_x_steps_per_unit")
            if not calibration.get("travel"):
                try:
                    calibration["travel"] = abs(
                        float(calibration["motor"]["position"])
                        - float(calibration["far"]["position"])
                    )
                except (KeyError, TypeError, ValueError):
                    pass
            if not calibration.get("travel_steps") and calibration.get("travel"):
                try:
                    calibration["travel_steps"] = abs(
                        float(calibration["travel"]) * float(axis.get("steps_per_unit", 0))
                    )
                except (TypeError, ValueError):
                    pass

        elif isinstance(observations, dict) and all(
            isinstance(observations.get(edge), dict) for edge in ("motor", "far")
        ):
            # сохраняем реальные контакты, старый условный диапазон отбрасываем
            try:
                motor = copy.deepcopy(observations["motor"])
                far = copy.deepcopy(observations["far"])
                travel = abs(float(motor["position"]) - float(far["position"]))
                steps_per_unit = float(axis.get("steps_per_unit", 0))
                positive = bool(observations["positive_toward_motor"])
                if travel > 0 and steps_per_unit > 0:
                    motor.setdefault("step_count", None)
                    far.setdefault("step_count", None)
                    axis["axis_calibration"] = {
                        "channel": observations.get("channel", f"{str(name).lower()}_min"),
                        "motor": motor,
                        "far": far,
                        "positive_toward_motor": positive,
                        "travel": travel,
                        "travel_steps": travel * steps_per_unit,
                        "home_side": (
                            manual_setup.get("home_edge", "motor")
                            if isinstance(manual_setup, dict)
                            else "motor"
                        ),
                        "recorded_at": observations.get(
                            "recorded_at", datetime.now(UTC).isoformat()
                        ),
                        "measurement_source": "m114_x_steps_per_unit",
                        "source": "parallel_endstops",
                    }
            except (KeyError, TypeError, ValueError):
                # если данных не хватает, оставляем проверку модели; ничего не угадываем
                pass

        if is_parallel_xy:
            axis["travel"] = None

    return data
