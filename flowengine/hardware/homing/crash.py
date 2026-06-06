"""Crash homing — blind drive into mechanical stop with limited current. TODO.

Strategy: reduce driver current via `M906` to a value low enough that the motor
stalls quietly at the stop, command a long move past the expected end, accept
the lost steps, then set zero with `G92`. Requires per-axis safe-current
calibration; without it you damage couplings.
"""

from __future__ import annotations

from flowengine.hardware.homing.base import HomingStrategy


class CrashHoming(HomingStrategy):
    name = "crash"

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "Crash homing requires per-axis safe-current calibration. "
            "See docs/HARDWARE.md → 'Homing strategy' for the calibration procedure. "
            "Do not enable this until each axis has a documented safe homing current."
        )

    async def home(self, queue, axes: list[str] | None) -> list[str]:  # pragma: no cover
        raise NotImplementedError
