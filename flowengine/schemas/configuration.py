"""Persisted configuration profiles and autosampler rack geometry."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flowengine.schemas.device import DeviceMap
from flowengine.schemas.mode import ModesConfig
from flowengine.schemas.runtime import RuntimeParams


class RackCellOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cell: str = Field(pattern=r"^[A-Za-z]+[1-9][0-9]*$")
    enabled: bool = True
    x: float | None = None
    y: float | None = None
    z: float | None = None
    parameters: dict[str, float | int | str | bool] = Field(default_factory=dict)


class RackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    description: str = ""
    rows: int = Field(ge=1, le=64)
    columns: int = Field(ge=1, le=64)
    row_pitch: float = Field(gt=0)
    column_pitch: float = Field(gt=0)
    origin_x: float = 0.0
    origin_y: float = 0.0
    safe_z: float | None = None
    x_axis: str | None = None
    y_axis: str | None = None
    z_axis: str | None = None
    cells: list[RackCellOverride] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_cells(self) -> RackConfig:
        names = [cell.cell.upper() for cell in self.cells]
        if len(names) != len(set(names)):
            raise ValueError("rack cell overrides must be unique")
        return self

    def coordinates(self, cell_name: str) -> dict[str, float | str]:
        match = re.fullmatch(r"([A-Za-z]+)([1-9][0-9]*)", cell_name)
        if not match:
            raise ValueError(f"invalid cell name {cell_name!r}")
        row = 0
        for character in match.group(1).upper():
            row = row * 26 + (ord(character) - ord("A") + 1)
        row -= 1
        column = int(match.group(2)) - 1
        if row >= self.rows or column >= self.columns:
            raise ValueError(f"cell {cell_name!r} is outside rack {self.name!r}")
        override = next(
            (item for item in self.cells if item.cell.upper() == cell_name.upper()), None
        )
        if override is not None and not override.enabled:
            raise ValueError(f"cell {cell_name!r} is disabled")
        x = self.origin_x + column * self.column_pitch
        y = self.origin_y + row * self.row_pitch
        return {
            "cell": cell_name.upper(),
            "x": override.x if override and override.x is not None else x,
            "y": override.y if override and override.y is not None else y,
            "z": override.z if override and override.z is not None else (self.safe_z or 0.0),
        }


class ConfigurationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    description: str = ""
    device_map: DeviceMap
    runtime: RuntimeParams
    modes: ModesConfig | None = None
    rack: RackConfig | None = None
