"""Operator-entered pressure — default Phase 1 sensor."""

from __future__ import annotations

from flowengine.hardware.pressure.base import PressureSensor


class ManualSensor(PressureSensor):
    name = "manual"

    def __init__(self, units: str = "kPa") -> None:
        self.units = units
        self._value: float | None = None

    def set_value(self, value: float) -> None:
        self._value = value

    async def read(self) -> float | None:
        return self._value

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return
