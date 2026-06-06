"""Procedure DSL — a deliberately small, declarative format.

Procedures are YAML files in `config/procedures/`. No variables, no loops, no
conditionals beyond `wait_pressure`. If a procedure needs branching, it's two
procedures. If it needs arithmetic, write a Python script that calls REST.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MoveStep(_Step):
    op: Literal["move"]
    axis: str
    to: float | None = None
    by: float | None = None
    feedrate: float | None = None

    @model_validator(mode="after")
    def _check_exactly_one(self) -> MoveStep:
        if (self.to is None) == (self.by is None):
            raise ValueError("`move` requires exactly one of `to` or `by`")
        return self


class MoveMultiStep(_Step):
    op: Literal["move_multi"]
    axes: dict[str, float]
    feedrate: float | None = None
    relative: bool = False


class HomeStep(_Step):
    op: Literal["home"]
    axes: list[str] | None = Field(default=None, description="None = home all axes.")


class DwellStep(_Step):
    op: Literal["dwell"]
    seconds: float = Field(gt=0)


class SetParamStep(_Step):
    op: Literal["set_param"]
    name: str
    value: float | int | str | bool


class SetValveStep(_Step):
    op: Literal["set_valve"]
    name: str
    position: Literal["A", "B"]


class WaitPressureStep(_Step):
    """Phase 3. Loader accepts this in Phase 2; runner refuses to execute it."""

    op: Literal["wait_pressure"]
    cmp: Literal["<", ">", "between"]
    value: float | None = None
    min: float | None = None
    max: float | None = None
    timeout: float = Field(gt=0)


class LogStep(_Step):
    op: Literal["log"]
    message: str


class CheckpointStep(_Step):
    op: Literal["checkpoint"]
    name: str


Step = Annotated[
    Union[
        MoveStep,
        MoveMultiStep,
        HomeStep,
        DwellStep,
        SetParamStep,
        SetValveStep,
        WaitPressureStep,
        LogStep,
        CheckpointStep,
    ],
    Field(discriminator="op"),
]


class Procedure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str = ""
    version: int = 1
    steps: list[Step]
