"""Procedure DSL — a deliberately small, declarative format.

Procedures are YAML files in `config/procedures/`. No variables, no loops, no
conditionals beyond `wait_pressure`. If a procedure needs branching, it's two
procedures. If it needs arithmetic, write a Python script that calls REST.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

ParameterReference = Annotated[str, Field(pattern=r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")]
NumberValue = float | ParameterReference


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MoveStep(_Step):
    op: Literal["move"]
    axis: str
    to: NumberValue | None = None
    by: NumberValue | None = None
    feedrate: NumberValue | None = None

    @model_validator(mode="after")
    def _check_exactly_one(self) -> MoveStep:
        if (self.to is None) == (self.by is None):
            raise ValueError("`move` requires exactly one of `to` or `by`")
        return self


class MoveMultiStep(_Step):
    op: Literal["move_multi"]
    axes: dict[str, NumberValue]
    feedrate: NumberValue | None = None
    relative: bool = False


class HomeStep(_Step):
    op: Literal["home"]
    axes: list[str] | None = Field(default=None, description="None = home all axes.")


class DwellStep(_Step):
    op: Literal["dwell"]
    seconds: NumberValue

    @model_validator(mode="after")
    def positive_seconds(self) -> DwellStep:
        if isinstance(self.seconds, (int, float)) and self.seconds <= 0:
            raise ValueError("dwell seconds must be positive")
        return self


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


class CallStep(_Step):
    """Invoke another named procedure with an explicit parameter mapping."""

    op: Literal["call"]
    procedure: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    parameters: dict[str, float | int | str | bool] = Field(default_factory=dict)


class ProcedureParameter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["number", "integer", "string", "boolean"]
    description: str = ""
    default: float | int | str | bool | None = None
    minimum: float | None = None
    maximum: float | None = None

    def validate_value(self, name: str, value: Any) -> float | int | str | bool:
        if self.type == "number" and not isinstance(value, (int, float)):
            raise ValueError(f"parameter {name!r} must be a number")
        if self.type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"parameter {name!r} must be an integer")
        if self.type == "string" and not isinstance(value, str):
            raise ValueError(f"parameter {name!r} must be a string")
        if self.type == "boolean" and not isinstance(value, bool):
            raise ValueError(f"parameter {name!r} must be a boolean")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"parameter {name!r} is below {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"parameter {name!r} is above {self.maximum}")
        return cast("float | int | str | bool", value)


Step = Annotated[
    MoveStep
    | MoveMultiStep
    | HomeStep
    | DwellStep
    | SetParamStep
    | SetValveStep
    | WaitPressureStep
    | LogStep
    | CheckpointStep
    | CallStep,
    Field(discriminator="op"),
]


class Procedure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str = ""
    version: int = 1
    draft: bool = False
    parameters: dict[str, ProcedureParameter] = Field(default_factory=dict)
    steps: list[Step]

    def resolve(
        self,
        supplied: dict[str, Any] | None = None,
        *,
        allow_draft: bool = False,
    ) -> Procedure:
        """Resolve exact `${name}` placeholders and revalidate executable steps.

        Drafts are rejected for a complete run, but the editor may resolve them
        for preview and deliberate one-step-at-a-time commissioning.
        """
        if self.draft and not allow_draft:
            raise ValueError(f"procedure {self.name!r} is a draft")
        values = supplied or {}
        unknown = set(values) - set(self.parameters)
        if unknown:
            raise ValueError("unknown parameters: " + ", ".join(sorted(unknown)))
        resolved: dict[str, float | int | str | bool] = {}
        for name, definition in self.parameters.items():
            value = values.get(name, definition.default)
            if value is None:
                raise ValueError(f"required parameter {name!r} is missing")
            resolved[name] = definition.validate_value(name, value)

        marker = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

        def substitute(value: Any) -> Any:
            if isinstance(value, str):
                match = marker.fullmatch(value)
                if match:
                    name = match.group(1)
                    if name not in resolved:
                        raise ValueError(f"step references undefined parameter {name!r}")
                    return resolved[name]
                return value
            if isinstance(value, list):
                return [substitute(item) for item in value]
            if isinstance(value, dict):
                return {key: substitute(item) for key, item in value.items()}
            return value

        data = self.model_dump()
        data["steps"] = substitute(data["steps"])
        data["parameters"] = {}
        data["draft"] = False
        return Procedure.model_validate(data)
