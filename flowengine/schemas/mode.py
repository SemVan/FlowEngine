"""Operating modes. Three named modes are exposed as buttons in the UI.

A mode bundles: a set of runtime-param overrides and a list of procedures it
makes available. Phase 1 ships the schema; the manager wires up in Phase 2.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    label: str = Field(min_length=1, description="Short label for the button.")
    description: str = ""
    runtime_overrides: dict[str, float | int | str | bool] = Field(default_factory=dict)
    procedures: list[str] = Field(
        default_factory=list, description="Procedure names visible in this mode."
    )
    color: str = Field(default="#888", description="CSS color for the mode button.")


class ModesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default: str
    modes: list[ModeDefinition]

    def get(self, name: str) -> ModeDefinition:
        for m in self.modes:
            if m.name == name:
                return m
        raise KeyError(f"mode {name!r} not defined")
