"""Motion model: soft limits + unit conversion + axis lookup.

This layer is the truth about where each axis is allowed to be. Wired-OR
endstops cannot tell min from max, so hardware never enforces these limits —
software does, and every motion command goes through here first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from flowengine.errors import SoftLimitError
from flowengine.schemas.device import DeviceMap

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AxisPosition:
    target: float
    feedrate: float


class MotionModel:
    """Stateful: tracks last-commanded position (assumed equal to actual after wait_idle)."""

    def __init__(self, device_map: DeviceMap, feedrate_cap: float) -> None:
        self._map = device_map
        self._feedrate_cap = feedrate_cap
        self._positions: dict[str, float] = {a.marlin_axis: 0.0 for a in device_map.axes}
        self._homed: dict[str, bool] = {a.marlin_axis: False for a in device_map.axes}

    @property
    def positions(self) -> dict[str, float]:
        return dict(self._positions)

    @property
    def homed(self) -> dict[str, bool]:
        return dict(self._homed)

    def mark_homed(self, axes: list[str]) -> None:
        for a in axes:
            self._homed[a] = True
            self._positions[a] = 0.0

    def update_position(self, positions: dict[str, float]) -> None:
        for k, v in positions.items():
            if k in self._positions:
                self._positions[k] = v

    def accept_target(self, axis: str, target: float) -> None:
        """Commit a planned position only after the controller accepted the move."""
        self._positions[axis] = target

    def plan_relative(self, axis: str, delta: float, feedrate: float | None) -> AxisPosition:
        cfg = self._map.axis(axis)
        target = self._positions[axis] + delta
        return self._validate(axis, target, feedrate, cfg=cfg)

    def plan_absolute(self, axis: str, target: float, feedrate: float | None) -> AxisPosition:
        cfg = self._map.axis(axis)
        return self._validate(axis, target, feedrate, cfg=cfg)

    def _validate(self, axis: str, target: float, feedrate: float | None, cfg) -> AxisPosition:
        if not self._homed[axis]:
            raise SoftLimitError(f"axis {axis} not homed; refuse to move")
        # Convention: home_direction `min` → axis range is [0, travel]; `max` → [-travel, 0].
        lo, hi = (0.0, cfg.travel) if cfg.home_direction == "min" else (-cfg.travel, 0.0)
        if target < lo or target > hi:
            raise SoftLimitError(
                f"axis {axis} target {target:.3f} outside soft limits [{lo:.3f}, {hi:.3f}]"
            )
        fr = feedrate if feedrate is not None else cfg.feedrate_default
        cap = min(cfg.feedrate_max, self._feedrate_cap)
        if fr <= 0:
            raise SoftLimitError(f"feedrate must be positive (got {fr})")
        if fr > cap:
            log.warning("feedrate %.1f clamped to cap %.1f on axis %s", fr, cap, axis)
            fr = cap
        return AxisPosition(target=target, feedrate=fr)
