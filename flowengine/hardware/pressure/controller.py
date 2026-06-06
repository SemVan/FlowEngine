"""Read pressure off the controller — Phase 3, stub.

The controller-side path will piggy-back on Marlin's `M105` (or an `M260`/`M261`
I²C exchange depending on the actual sensor wiring). Decision and integration
are deferred until the sensor source is confirmed; see docs/HARDWARE.md.
"""

from __future__ import annotations

from flowengine.hardware.pressure.base import PressureSensor


class ControllerSensor(PressureSensor):
    name = "controller"

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "ControllerSensor is Phase 3. Decide the wiring path with the hardware "
            "developer (analog through Monster8 AUX vs I²C vs separate USB) and "
            "document it in HARDWARE.md before implementing."
        )

    async def read(self) -> float | None:  # pragma: no cover
        raise NotImplementedError

    async def start(self) -> None:  # pragma: no cover
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover
        raise NotImplementedError
