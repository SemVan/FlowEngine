"""Device map: physical hardware → controller axes.

Loaded from `config/device_map.yaml`. All fields except clearly optional ones are
required — the loader rejects underspecified maps so that wiring details aren't
silently defaulted. See docs/HARDWARE.md for the meaning of each field.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Marlin can be compiled with additional logical axes (A/B/C/U/etc.).  The
# physical Monster8 socket name (for example E2) is not necessarily the G-code
# axis exposed by that firmware build.
MarlinAxis = Annotated[str, Field(pattern=r"^[A-Z](?:[0-9]+)?$")]
HomeDirection = Literal["min", "max"]
HomingStrategyName = Literal["endstop", "sensorless", "crash"]


class DeviceKind(StrEnum):
    SYRINGE_PUMP = "syringe_pump"
    PERISTALTIC_PUMP = "peristaltic_pump"
    VALVE = "valve"
    AUTOSAMPLER_AXIS = "autosampler_axis"


class AxisConfig(BaseModel):
    """One controller axis.

    `endstop_channels_shared` documents that two physical switches (e.g. min+max
    on a syringe) are wired-OR onto a single input. The controller cannot
    distinguish which side fired, so soft limits in software are mandatory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    marlin_axis: MarlinAxis
    name: str = Field(min_length=1)
    kind: DeviceKind
    steps_per_unit: float = Field(gt=0, description="steps/mm or steps/deg")
    units: Literal["mm", "deg"] = "mm"
    travel: float = Field(gt=0, description="Total mechanical travel in `units`.")
    home_direction: HomeDirection
    homing_strategy: HomingStrategyName = "endstop"
    feedrate_default: float = Field(gt=0, description="Default feedrate, units/min.")
    feedrate_max: float = Field(gt=0)
    accel_max: float | None = Field(default=None, gt=0, description="Optional accel cap, units/s².")
    dir_invert: bool = False
    endstop_channels_shared: bool = Field(
        default=True,
        description="True if min+max endstops are wired-OR onto a single controller input.",
    )
    endstop_inverted: bool = False
    notes: str = ""

    @model_validator(mode="after")
    def _check_caps(self) -> AxisConfig:
        if self.feedrate_default > self.feedrate_max:
            raise ValueError(
                f"feedrate_default ({self.feedrate_default}) exceeds feedrate_max ({self.feedrate_max})"
            )
        return self


class ValveConfig(BaseModel):
    """A two-position valve driven by one axis.

    `position_a` and `position_b` are axis positions (in axis units) where the
    valve reaches each mechanical detent. After homing we trust the software
    position model — wired-OR endstops can't tell us which side fired.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    axis: MarlinAxis
    position_a: float
    position_b: float
    home_position: Literal["A", "B"] = "A"


class PumpConfig(BaseModel):
    """A pump bound to one axis. `volume_per_unit` lets us speak in µL or mL in the UI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    axis: MarlinAxis
    kind: Literal["syringe", "peristaltic"]
    volume_per_unit_ul: float = Field(
        gt=0,
        description="Microliters delivered per axis unit (mm for syringe, deg for peristaltic).",
    )
    syringe_volume_ul: float | None = Field(
        default=None, gt=0, description="Nominal syringe capacity if known."
    )
    has_encoder: bool = False


class AutosamplerConfig(BaseModel):
    """The XYZ capillary positioner. Each axis is also listed in `axes`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = "autosampler"
    x_axis: MarlinAxis
    y_axis: MarlinAxis
    z_axis: MarlinAxis
    safe_z: float = Field(
        description="Z-position considered safe for X/Y travel (away from samples)."
    )


class DeviceMap(BaseModel):
    """Top-level device map. The single source of truth for which physical thing
    is on which controller axis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str = Field(min_length=1, description="Free-form identifier of this instrument.")
    axes: list[AxisConfig]
    valves: list[ValveConfig] = Field(default_factory=list)
    pumps: list[PumpConfig] = Field(default_factory=list)
    autosampler: AutosamplerConfig | None = None

    @model_validator(mode="after")
    def _check_unique_axes(self) -> DeviceMap:
        seen: set[str] = set()
        for a in self.axes:
            if a.marlin_axis in seen:
                raise ValueError(f"axis {a.marlin_axis} declared twice")
            seen.add(a.marlin_axis)
        axis_set = seen
        for v in self.valves:
            if v.axis not in axis_set:
                raise ValueError(f"valve {v.name!r} references unknown axis {v.axis}")
        for p in self.pumps:
            if p.axis not in axis_set:
                raise ValueError(f"pump {p.name!r} references unknown axis {p.axis}")
        if self.autosampler is not None:
            for slot in (
                self.autosampler.x_axis,
                self.autosampler.y_axis,
                self.autosampler.z_axis,
            ):
                if slot not in axis_set:
                    raise ValueError(f"autosampler references unknown axis {slot}")
        return self

    def axis(self, marlin_axis: str) -> AxisConfig:
        for a in self.axes:
            if a.marlin_axis == marlin_axis:
                return a
        raise KeyError(f"axis {marlin_axis} not in device map")
