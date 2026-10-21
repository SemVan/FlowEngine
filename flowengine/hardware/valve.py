"""старые данные крана для совместимости; движением управляет sender.valves"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from flowengine.schemas.device import ValveConfig

log = logging.getLogger(__name__)


@dataclass
class Valve:
    config: ValveConfig
    current: Literal["A", "B", "unknown"] = "unknown"

    def target_for(self, position: Literal["A", "B"]) -> float:
        return self.config.position_a if position == "A" else self.config.position_b

    def mark(self, position: Literal["A", "B"]) -> None:
        self.current = position
        log.debug("valve %s → %s", self.config.name, position)
