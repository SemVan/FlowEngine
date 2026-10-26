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


class EndstopContactObservation(BaseModel):

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    position: float
    approach_direction: Literal[-1, 1]
    step_count: int | None = None


class AxisCalibration(BaseModel):
    """калибровка X/Y по двум реальным концевикам; общий сигнал различаем по направлению"""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    channel: Literal["x_min", "y_min"]
    motor: EndstopContactObservation
    far: EndstopContactObservation
    positive_toward_motor: bool
    travel: float = Field(gt=0)
    travel_steps: float = Field(gt=0)
    seek_feedrate: float = Field(
        default=1200.0,
        gt=0,
        description="Fast commissioning/home seek feedrate in axis units per minute.",
    )
    latch_feedrate: float = Field(
        default=120.0,
        gt=0,
        description="Slow repeat-touch feedrate used to latch an endstop precisely.",
    )
    backoff_mm: float = Field(
        default=2.0,
        gt=0,
        description="Distance to clear a contacted switch before the slow repeat touch.",
    )
    home_side: Literal["motor", "far"] = Field(
        default="motor",
        description="Physical edge used as the absolute reference during Home.",
    )
    recorded_at: str
    measurement_source: Literal["marlin_count", "m114_x_steps_per_unit"]
    source: Literal["parallel_endstops"] = "parallel_endstops"

    @model_validator(mode="after")
    def consistent_contacts(self):
        if self.latch_feedrate > self.seek_feedrate:
            raise ValueError("latch feedrate must not exceed seek feedrate")
        motor_direction = 1 if self.positive_toward_motor else -1
        if self.motor.approach_direction != motor_direction or self.far.approach_direction != -motor_direction:
            raise ValueError("contact approach directions must oppose and match the physical labels")
        span = abs(self.motor.position - self.far.position)
        if span <= 0 or abs(span - self.travel) > max(0.02, span * 0.002):
            raise ValueError("calibrated travel must match the observed physical endstop span")
        return self

    @property
    def motor_coordinate(self) -> float:
        """координата моторного микрика; начало и конец хода задаём как 0 и travel"""
        return self.travel if self.positive_toward_motor else 0.0

    @property
    def far_coordinate(self) -> float:
        return 0.0 if self.positive_toward_motor else self.travel

    @property
    def home_coordinate(self) -> float:
        return self.motor_coordinate if self.home_side == "motor" else self.far_coordinate

    @property
    def home_approach_direction(self) -> int:
        contact = self.motor if self.home_side == "motor" else self.far
        return contact.approach_direction


class AxisConfig(BaseModel):
    """ось контроллера; сторону общего концевика определяем по направлению"""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    marlin_axis: MarlinAxis
    name: str = Field(min_length=1)
    kind: DeviceKind
    steps_per_unit: float = Field(gt=0, description="steps/mm or steps/deg")
    units: Literal["mm", "deg"] = "mm"
    travel: float | None = Field(default=None, gt=0, description="Declared mechanical travel for axes without X/Y endstop calibration.")
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
    driver_current_max_ma: int | None = Field(default=None, gt=0)
    axis_calibration: AxisCalibration | None = None

    @property
    def mechanical_travel(self) -> float:
        if self.axis_calibration is not None:
            return self.axis_calibration.travel
        if self.travel is None:
            raise ValueError(f"axis {self.marlin_axis} is not physically calibrated")
        return self.travel

    @model_validator(mode="after")
    def _check_caps(self) -> AxisConfig:
        if self.axis_calibration is not None:
            if self.marlin_axis not in {"X", "Y"} or self.kind != DeviceKind.SYRINGE_PUMP or self.units != "mm":
                raise ValueError("axis calibration supports syringe axes X/Y in mm")
            if self.axis_calibration.channel != f"{self.marlin_axis.lower()}_min":
                raise ValueError("axis calibration channel must match its axis")
            if self.travel is not None:
                raise ValueError("calibrated X/Y travel lives only in axis_calibration; remove legacy travel")
        elif self.marlin_axis in {"X", "Y"} and self.kind == DeviceKind.SYRINGE_PUMP and self.endstop_channels_shared:
            if self.travel is not None:
                raise ValueError("uncalibrated X/Y with parallel endstops must not carry a guessed travel")
        elif self.travel is None:
            raise ValueError("axes without X/Y parallel-endstop calibration require declared travel")
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

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    name: str = Field(min_length=1)
    axis: MarlinAxis
    position_a: float
    position_b: float
    home_position: Literal["A", "B"] = "A"
    position_names: dict[str, Literal["A", "B"]] = Field(default_factory=dict)
    calibration_notes: str = ""
    direction_toward_home: Literal[-1, 1] | None = None
    home_seek_speed: float = Field(default=120.0, gt=0)
    home_latch_speed: float = Field(default=30.0, gt=0)
    backoff: float = Field(default=0.5, gt=0)
    pull_off: float = Field(default=0.5, gt=0)
    home_timeout_s: float = Field(default=30.0, gt=0, le=120)
    calibrated: bool = False

    def position_slot(self, name: str) -> Literal["A", "B"]:
        slot = name.upper() if name.upper() in {"A", "B"} else self.position_names.get(name)
        if slot not in {"A", "B"}:
            raise ValueError(f"unknown position {name!r} for valve {self.name!r}")
        return slot

    def position(self, name: str) -> float:
        return self.position_a if self.position_slot(name) == "A" else self.position_b


class SharedValveEndstop(BaseModel):
    """общий вход микриков кранов; имя задаётся явно"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    channel: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_]+$")


class PumpConfig(BaseModel):
    """A pump bound to one axis. `volume_per_unit` lets us speak in µL or mL in the UI."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

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
    dispense_direction: Literal[-1, 1] = 1
    calibrated: bool = False
    calibration_notes: str = ""
    allowed_routes: dict[Literal["aspirate", "dispense"], dict[str, list[str]]] = Field(default_factory=dict)


class EndstopInput(BaseModel):
    """Logical switches sharing one firmware input; NOT a firmware pin remapping."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    channel: str = Field(pattern=r"^[a-zA-Z0-9_]+$")
    axes: list[MarlinAxis] = Field(min_length=1)
    active_high: bool = True
    notes: str = ""


class AnalogInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    name: str = Field(min_length=1)
    source: str = Field(pattern=r"^(T\d*|B|C|ADC\d+)$")
    quantity: Literal["temperature", "analog"] = "temperature"
    units: str = "°C"
    scale: float = 1.0
    offset: float = 0.0
    poll_hz: float = Field(default=1.0, gt=0, le=10)
    notes: str = ""


class AutosamplerConfig(BaseModel):
    """The XYZ capillary positioner. Each axis is also listed in `axes`."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

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

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    instrument_id: str = Field(min_length=1, description="Free-form identifier of this instrument.")
    axes: list[AxisConfig]
    valves: list[ValveConfig] = Field(default_factory=list)
    shared_valve_endstop: SharedValveEndstop = Field(default_factory=SharedValveEndstop)
    pumps: list[PumpConfig] = Field(default_factory=list)
    autosampler: AutosamplerConfig | None = None
    endstop_inputs: list[EndstopInput] = Field(default_factory=list)
    analog_inputs: list[AnalogInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_axes(self) -> DeviceMap:
        seen: set[str] = set()
        for a in self.axes:
            if a.marlin_axis in seen:
                raise ValueError(f"axis {a.marlin_axis} declared twice")
            seen.add(a.marlin_axis)
        axis_set = seen
        for group in self.endstop_inputs:
            if len(set(group.axes)) != len(group.axes) or not set(group.axes) <= axis_set:
                raise ValueError("endstop input axes must be unique known axes")
        for items, field in (
            (self.endstop_inputs, "channel"),
            (self.analog_inputs, "name"),
            (self.pumps, "name"),
            (self.valves, "name"),
        ):
            values = [getattr(item, field) for item in items]
            if len(set(values)) != len(values):
                raise ValueError(f"duplicate {field} in device map")
        for v in self.valves:
            if v.axis not in axis_set:
                raise ValueError(f"valve {v.name!r} references unknown axis {v.axis}")
            direction = v.direction_toward_home or (-1 if self.axis(v.axis).home_direction == "min" else 1)
            if v.calibrated and (v.position_a * direction >= 0 or v.position_b * direction >= 0
                                 or abs(v.position_a - v.position_b) < 0.001):
                raise ValueError("calibrated valve A/B must be distinct HOME-relative positions away from HOME")
        if len({v.axis for v in self.valves}) != len(self.valves):
            raise ValueError("each valve must have its own motor axis")
        for p in self.pumps:
            if p.axis not in axis_set:
                raise ValueError(f"pump {p.name!r} references unknown axis {p.axis}")
            for route in p.allowed_routes.values():
                for name, positions in route.items():
                    valve = next((v for v in self.valves if v.name == name), None)
                    if valve is None or not positions:
                        raise ValueError(f"pump {p.name}: route must name known valves and positions")
                    for position in positions:
                        valve.position(position)
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
