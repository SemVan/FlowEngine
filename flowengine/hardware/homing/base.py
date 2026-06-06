"""HomingStrategy interface.

The same `home(axes)` call dispatches to a different physical procedure
depending on which strategy is configured per axis. The strategy owns the
G-code emitted, the timeout, and which `axes` actually get marked homed in the
motion model.
"""

from __future__ import annotations

import abc


class HomingStrategy(abc.ABC):
    name: str

    @abc.abstractmethod
    async def home(self, queue, axes: list[str] | None) -> list[str]:
        """Home the requested axes. Returns the list of axes actually homed."""
