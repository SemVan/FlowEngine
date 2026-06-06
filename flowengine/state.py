"""Controller state machine.

Six states; every legal transition is enumerated explicitly. Illegal transitions
raise `StateError`. This is the cheap defense against "weird behavior" bugs that
emerge when commands race with state.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum

from flowengine.errors import StateError

log = logging.getLogger(__name__)


class State(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED_IDLE = "connected_idle"
    HOMING = "homing"
    MOVING = "moving"
    ABORTING = "aborting"
    ERRORED = "errored"


_LEGAL: dict[State, set[State]] = {
    State.DISCONNECTED: {State.CONNECTED_IDLE, State.ERRORED},
    State.CONNECTED_IDLE: {State.HOMING, State.MOVING, State.DISCONNECTED, State.ERRORED, State.ABORTING},
    State.HOMING: {State.CONNECTED_IDLE, State.ABORTING, State.ERRORED, State.DISCONNECTED},
    State.MOVING: {State.CONNECTED_IDLE, State.ABORTING, State.ERRORED, State.DISCONNECTED},
    State.ABORTING: {State.CONNECTED_IDLE, State.ERRORED, State.DISCONNECTED},
    State.ERRORED: {State.CONNECTED_IDLE, State.DISCONNECTED},
}


class StateMachine:
    """Owns the controller's current state and the rules for moving between states."""

    def __init__(self) -> None:
        self._state = State.DISCONNECTED
        self._detail = ""
        self._listeners: set[asyncio.Queue[tuple[State, str]]] = set()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> State:
        return self._state

    @property
    def detail(self) -> str:
        return self._detail

    async def transition(self, target: State, *, detail: str = "") -> None:
        async with self._lock:
            if target not in _LEGAL.get(self._state, set()) and target != self._state:
                raise StateError(f"illegal transition {self._state} → {target}")
            old = self._state
            self._state = target
            self._detail = detail
            if old != target:
                log.info("state: %s → %s (%s)", old.value, target.value, detail or "-")
            for q in list(self._listeners):
                try:
                    q.put_nowait((target, detail))
                except asyncio.QueueFull:
                    pass

    def require(self, *allowed: State) -> None:
        if self._state not in allowed:
            raise StateError(
                f"operation requires state {{{', '.join(s.value for s in allowed)}}}, "
                f"currently {self._state.value}"
            )

    def subscribe(self) -> asyncio.Queue[tuple[State, str]]:
        q: asyncio.Queue[tuple[State, str]] = asyncio.Queue(maxsize=64)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[tuple[State, str]]) -> None:
        self._listeners.discard(q)
