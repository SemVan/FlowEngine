"""ModeManager — Phase 2.

Owns the current operating mode and applies its runtime-param overrides on
switch. Three modes are defined in `config/modes.yaml`.
"""

from __future__ import annotations


class ModeManager:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("ModeManager is Phase 2; see docs/ROADMAP.md")
