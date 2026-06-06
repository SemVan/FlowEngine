"""Standard endstop homing via `G28`.

Marlin honors `home_direction` from firmware-side config. We pass only axis
letters; we never assume which physical switch fired (wired-OR concern).
"""

from __future__ import annotations

import logging

from flowengine.hardware.homing.base import HomingStrategy
from flowengine.schemas.device import DeviceMap

log = logging.getLogger(__name__)


class EndstopHoming(HomingStrategy):
    name = "endstop"

    def __init__(self, device_map: DeviceMap, timeout_s: float) -> None:
        self._map = device_map
        self._timeout = timeout_s

    async def home(self, queue, axes: list[str] | None) -> list[str]:
        targets = axes or [a.marlin_axis for a in self._map.axes]
        # Marlin only knows X/Y/Z directly for G28; E axes home via different mechanisms.
        # For fluidics, we issue G28 per axis letter, then track ourselves.
        for ax in targets:
            await queue.send(f"G28 {ax}", timeout=self._timeout)
        log.info("homed axes via endstop strategy: %s", targets)
        return targets
