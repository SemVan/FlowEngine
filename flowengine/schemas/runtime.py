"""Runtime parameters: feedrates, timeouts, firmware expectations, transport choice."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FirmwareExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    flavor: Literal["marlin", "klipper"] = "marlin"
    features_required: list[str] = Field(
        default_factory=lambda: ["CHECKSUM"],
        description="Feature tokens that must appear in M115 capabilities.",
    )


class TransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    port: str = "/dev/ttyACM0"
    baud: int = 250000
    reconnect_attempts: int = Field(default=5, ge=0)
    reconnect_backoff_s: float = Field(default=1.0, gt=0)


class Timeouts(BaseModel):
    """Per-command-class timeouts in seconds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok_default: float = Field(default=5.0, gt=0)
    move: float = Field(default=30.0, gt=0)
    homing: float = Field(default=60.0, gt=0)
    diagnostics: float = Field(default=3.0, gt=0)


class MotionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feedrate_cap: float = Field(default=3000.0, gt=0, description="Hard cap, units/min.")
    accel_cap: float | None = Field(default=None, gt=0)
    jog_step_default: float = Field(default=1.0, gt=0)


class PressureConfig(BaseModel):
    """How to read pressure. Phase 1 default is `manual`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["manual", "mock", "controller", "usb"] = "manual"
    units: str = "kPa"


class RuntimeParams(BaseModel):
    """Mutable runtime config. Saved/loaded from `config/runtime.yaml`; editable from UI."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    firmware: FirmwareExpectation = Field(default_factory=FirmwareExpectation)
    transport: TransportConfig = Field(default_factory=TransportConfig)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    motion: MotionLimits = Field(default_factory=MotionLimits)
    pressure: PressureConfig = Field(default_factory=PressureConfig)
