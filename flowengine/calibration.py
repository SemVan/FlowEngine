"""Calibration math. No controller counter is treated as an encoder measurement."""

from __future__ import annotations

import math
from statistics import mean, pstdev

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PumpMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    step_pulses: float = Field(gt=0)
    volume_ul: float | None = Field(default=None, gt=0)
    mass_g: float | None = Field(default=None, gt=0)
    density_g_ml: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def one_measurement(self):
        if (self.volume_ul is None) == (self.mass_g is None):
            raise ValueError("provide volume_ul OR mass_g, not both")
        return self


def calibrate_pump(steps_per_unit: float, measurements: list[PumpMeasurement]):
    if not math.isfinite(steps_per_unit) or steps_per_unit <= 0 or not measurements:
        raise ValueError("positive steps_per_unit and measurements required")
    ratios = [
        (m.volume_ul if m.volume_ul is not None else m.mass_g / m.density_g_ml * 1000)
        / m.step_pulses
        for m in measurements
    ]
    factor = mean(ratios)
    return {
        "volume_per_unit_ul": factor * steps_per_unit,
        "ul_per_step_pulse": factor,
        "repeats": len(ratios),
        "relative_stddev": pstdev(ratios) / factor,
        "warning": "valid only for measured direction, speed, fluid and pressure; confirm before saving",
    }


def reference_error(baseline_pulses: float, measured_pulses: list[float], tolerance_pulses: float):
    if (
        not measured_pulses
        or tolerance_pulses < 0
        or not all(math.isfinite(v) for v in [baseline_pulses, tolerance_pulses, *measured_pulses])
    ):
        raise ValueError(
            "finite baseline, nonempty measurements and nonnegative tolerance required"
        )
    errors = [v - baseline_pulses for v in measured_pulses]
    return {
        "errors_step_pulses": errors,
        "max_abs_error": max(abs(v) for v in errors),
        "suspected_position_error": any(abs(v) > tolerance_pulses for v in errors),
        "warning": "reference error is not proof of skipped steps; includes switch repeatability and backlash",
    }
