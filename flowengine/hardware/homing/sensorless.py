"""Sensorless (StallGuard) homing — TODO.

Requires TMC2209/TMC5160 drivers and StallGuard wired through DIAG pins. We
need to:
1. Set sensorless threshold per axis: `M914 X<sgthrs>`.
2. Reduce homing current: `M906 X<mA>` (and restore after).
3. Issue `G28` — Marlin treats DIAG as endstop when configured.

Why this is a TODO and not a feature: driver type is unknown for this build.
Until the hardware partner confirms TMC drivers and gives us the working
StallGuard sensitivity, we should not pretend to support it.
"""

from __future__ import annotations

from flowengine.hardware.homing.base import HomingStrategy


class SensorlessHoming(HomingStrategy):
    name = "sensorless"

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "Sensorless homing requires confirmed TMC driver type and StallGuard "
            "sensitivities per axis. See docs/HARDWARE.md → 'Homing strategy' "
            "for the checklist before implementing this."
        )

    async def home(self, queue, axes: list[str] | None) -> list[str]:  # pragma: no cover
        raise NotImplementedError
