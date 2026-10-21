"""REST request/response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis: str
    delta: float = Field(description="Relative move in axis units; sign matters.")
    feedrate: float | None = Field(default=None, gt=0)


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis: str
    target: float
    feedrate: float | None = Field(default=None, gt=0)


class HomeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axes: list[str] | None = Field(default=None, description="None = home all axes per device map.")


class StateResponse(BaseModel):
    state: Literal["disconnected", "connected_idle", "homing", "moving", "aborting", "errored"]
    detail: str
    positions: dict[str, float]
    homed: dict[str, bool]
    calibrated: dict[str, bool]
    reference_status: dict[str, Literal["uncalibrated", "calibrated_unreferenced", "referenced"]]
    reference_counts: dict[str, int] = Field(default_factory=dict)
    reference_sides: dict[str, str] = Field(default_factory=dict)
    queue_depth: int
    firmware: str | None
    transport: Literal["marlin", "mock", "klipper"]
    port: str | None = None
    baud: int | None = None
    motion_enabled: bool
    profile: str = "default"


class FirmwareInfo(BaseModel):
    raw: str
    flavor: str
    features: list[str]


class EndstopsSnapshot(BaseModel):
    triggered: dict[str, bool]
    raw: str


class DiagnosticResponse(BaseModel):
    command: str
    raw: list[str]
    positions: dict[str, float] | None = None
