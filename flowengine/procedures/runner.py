"""Procedure runner — Phase 2.

Scaffold only. The runner walks `procedure.steps` and dispatches each typed
step to the sender / valve / pump / state machine. Designed to support pause /
resume / abort and to publish per-step telemetry through the event bus.
"""

from __future__ import annotations

from flowengine.errors import ProcedureError
from flowengine.schemas import Procedure


class ProcedureRunner:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("ProcedureRunner is Phase 2; see docs/ROADMAP.md")

    async def run(self, proc: Procedure) -> None:  # pragma: no cover
        raise ProcedureError("Phase 2")
