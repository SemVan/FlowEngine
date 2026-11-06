"""совместный ход с отдельной скоростью и временем завершения каждой оси"""
from __future__ import annotations

import math


def timed_pump_moves(device, axes, seconds, speed_cap):
    seconds = float(seconds)
    if len(axes) < 2 or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Выберите минимум два насоса и задайте положительное время")
    distances, speeds, units = {}, {}, set()
    for axis, move in axes.items():
        if axis not in {a.marlin_axis for a in device.axes}:
            raise ValueError(f"Неизвестная ось: {axis}")
        cfg = device.axis(axis)
        if cfg.kind not in {"syringe_pump", "peristaltic_pump"}:
            raise ValueError(f"{axis}: совместная подача по времени предназначена для насосов")
        speed = float(move.feedrate)
        if move.speed_units == "axis/s":
            speed *= 60
        elif move.speed_units == "steps/s":
            speed *= 60 / cfg.steps_per_unit
        cap = min(cfg.feedrate_max, speed_cap)
        if not math.isfinite(speed) or not 0 < speed <= cap or move.direction not in {-1, 1}:
            raise ValueError(f"{axis}: задайте направление и скорость больше 0, не выше {cap:g} ед/мин")
        distance = move.direction * speed * seconds / 60
        if abs(distance) < 1 / cfg.steps_per_unit - 1e-9:
            raise ValueError(f"{axis}: за указанное время получается меньше одного шага привода")
        distances[axis], speeds[axis] = distance, speed
        units.add(cfg.units)
    if len(units) != 1:
        raise ValueError("Для совместной подачи нужны одинаковые единицы осей")
    if any(len(set(axes) & set(g.axes)) > 1 for g in device.endstop_inputs):
        raise ValueError("Насосы используют общий вход концевиков")
    return distances, speeds


def independent_segments(starts, targets, speeds, common_duration_s=None):
    if common_duration_s is not None:
        length = math.sqrt(sum((targets[a] - starts[a]) ** 2 for a in targets))
        return ([{"targets": dict(targets), "feedrate": 60 * length / common_duration_s,
                  "nominal_duration_s": common_duration_s}] if length > 1e-9 else [],
                dict.fromkeys(targets, common_duration_s))
    durations = {a: 60 * abs(targets[a] - starts[a]) / speeds[a] for a in targets}
    boundaries = sorted(set(t for t in durations.values() if t > 1e-9))
    segments = []
    previous = dict(starts)
    elapsed = 0.0
    for end in boundaries:
        positions = {a: starts[a] + (targets[a] - starts[a]) * min(1, end / durations[a])
                     for a in targets if durations[a] > elapsed + 1e-9}
        distance = math.sqrt(sum((target - previous[a]) ** 2 for a, target in positions.items()))
        if distance > 1e-9:
            segments.append({"targets": positions, "feedrate": 60 * distance / (end - elapsed),
                             "nominal_duration_s": end - elapsed})
            previous.update(positions)
        elapsed = end
    return segments, durations
