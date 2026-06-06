"""PressureSensor interface — Phase 1 uses ManualSensor by default."""

from __future__ import annotations

import abc


class PressureSensor(abc.ABC):
    name: str
    units: str = "kPa"

    @abc.abstractmethod
    async def read(self) -> float | None:
        """Return current pressure, or None if no fresh sample is available."""

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...
