"""храним привязку и координаты осей, проверяем скорость; границы X/Y задают микрики"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from flowengine.errors import MotionValidationError
from flowengine.schemas.device import DeviceMap

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AxisPosition:
    target: float
    feedrate: float


class MotionModel:

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

    def mark_referenced(self, axis: str, position: float) -> None:
        self._map.axis(axis)
        if not math.isfinite(position):
            raise MotionValidationError("reference position must be finite")
        self._homed[axis] = True
        self._positions[axis] = position

    def mark_homed(self, axes: list[str]) -> None:
        for a in axes:
            self.mark_referenced(a, 0.0)

    def invalidate(self, axes: list[str] | None = None) -> None:
        for axis in axes if axes is not None else self._homed:
            self._map.axis(axis)
            self._homed[axis] = False

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

    def max_feedrate(self, axis: str) -> float:
        return min(self._map.axis(axis).feedrate_max, self._feedrate_cap)

    def plan_absolute(self, axis: str, target: float, feedrate: float | None) -> AxisPosition:
        cfg = self._map.axis(axis)
        return self._validate(axis, target, feedrate, cfg=cfg)

    def _validate(self, axis: str, target: float, feedrate: float | None, cfg) -> AxisPosition:
        if not math.isfinite(target) or (feedrate is not None and not math.isfinite(feedrate)):
            raise MotionValidationError("position and feedrate must be finite")
        if not self._homed[axis]:
            raise MotionValidationError(f"axis {axis} is not referenced; home it before normal motion")
        fr = feedrate if feedrate is not None else cfg.feedrate_default
        cap = min(cfg.feedrate_max, self._feedrate_cap)
        if fr <= 0:
            raise MotionValidationError(f"feedrate must be positive (got {fr})")
        if fr > cap:
            log.warning("feedrate %.1f clamped to cap %.1f on axis %s", fr, cap, axis)
            fr = cap
        return AxisPosition(target=target, feedrate=fr)
