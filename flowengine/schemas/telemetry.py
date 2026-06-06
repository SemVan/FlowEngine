"""WebSocket message envelopes. The browser deserializes these as typed events."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _WsBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PositionUpdate(_WsBase):
    type: Literal["position"] = "position"
    positions: dict[str, float]
    settled: bool = Field(default=False, description="True if last M114 was preceded by M400.")


class StateUpdate(_WsBase):
    type: Literal["state"] = "state"
    state: Literal[
        "disconnected", "connected_idle", "homing", "moving", "aborting", "errored"
    ]
    detail: str = ""


class EndstopUpdate(_WsBase):
    type: Literal["endstops"] = "endstops"
    triggered: dict[str, bool]


class PressureUpdate(_WsBase):
    type: Literal["pressure"] = "pressure"
    value: float
    units: str
    t_ms: int


class LogLine(_WsBase):
    type: Literal["log"] = "log"
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    message: str
    t_ms: int


class QueueDepth(_WsBase):
    type: Literal["queue"] = "queue"
    depth: int


TelemetryMessage = Annotated[
    Union[PositionUpdate, StateUpdate, EndstopUpdate, PressureUpdate, LogLine, QueueDepth],
    Field(discriminator="type"),
]
