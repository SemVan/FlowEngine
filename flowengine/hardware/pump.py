"""Pump abstraction — semantic wrapper around an axis.

Translates between µL (UI-friendly) and axis units. Phase 1 ships the wrapper;
procedures in Phase 2 use it to express "dispense 500 µL" without hardcoding
steps.
"""

from __future__ import annotations

from dataclasses import dataclass

from flowengine.schemas.device import PumpConfig


@dataclass
class Pump:
    config: PumpConfig

    def units_for_volume_ul(self, volume_ul: float) -> float:
        return volume_ul / self.config.volume_per_unit_ul

    def volume_for_units(self, axis_units: float) -> float:
        return axis_units * self.config.volume_per_unit_ul
