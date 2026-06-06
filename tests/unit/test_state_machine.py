"""State machine transitions."""

from __future__ import annotations

import pytest

from flowengine.errors import StateError
from flowengine.state import State, StateMachine


async def test_initial_disconnected():
    sm = StateMachine()
    assert sm.state == State.DISCONNECTED


async def test_legal_connect():
    sm = StateMachine()
    await sm.transition(State.CONNECTED_IDLE)
    assert sm.state == State.CONNECTED_IDLE


async def test_illegal_moving_from_disconnected():
    sm = StateMachine()
    with pytest.raises(StateError):
        await sm.transition(State.MOVING)


async def test_idempotent_transition_allowed():
    sm = StateMachine()
    await sm.transition(State.CONNECTED_IDLE)
    await sm.transition(State.CONNECTED_IDLE)  # same state — allowed


async def test_require_checks():
    sm = StateMachine()
    await sm.transition(State.CONNECTED_IDLE)
    sm.require(State.CONNECTED_IDLE)
    with pytest.raises(StateError):
        sm.require(State.MOVING)


async def test_errored_to_idle():
    sm = StateMachine()
    await sm.transition(State.CONNECTED_IDLE)
    await sm.transition(State.ERRORED, detail="oops")
    await sm.transition(State.CONNECTED_IDLE, detail="recovered")
    assert sm.state == State.CONNECTED_IDLE
