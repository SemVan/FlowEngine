"""Mock pressure: returns scripted values or a noisy sine — useful for dev/CI."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable

from flowengine.hardware.pressure.base import PressureSensor


class MockSensor(PressureSensor):
    name = "mock"

    def __init__(
        self,
        *,
        scripted: Iterable[float] | None = None,
        base: float = 0.0,
        amplitude: float = 5.0,
        period_s: float = 4.0,
        units: str = "kPa",
    ) -> None:
        self.units = units
        self._iter = iter(scripted) if scripted is not None else None
        self._base = base
        self._amp = amplitude
        self._period = period_s
        self._t0 = time.monotonic()

    async def read(self) -> float:
        if self._iter is not None:
            try:
                return next(self._iter)
            except StopIteration:
                self._iter = None
        t = time.monotonic() - self._t0
        return self._base + self._amp * math.sin(2 * math.pi * t / self._period)

    async def start(self) -> None:
        self._t0 = time.monotonic()

    async def stop(self) -> None:
        return
