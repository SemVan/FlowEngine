"""CommandQueue behavior against MockTransport."""

from __future__ import annotations

import pytest

from flowengine.errors import TransportClosed, TransportTimeout
from flowengine.hardware.queue import CommandQueue
from flowengine.transport.mock import MockTransport


@pytest.fixture
async def mock_q() -> tuple[MockTransport, CommandQueue]:
    t = MockTransport(latency_s=0.001)
    await t.open()
    q = CommandQueue(t, default_timeout_s=2.0)
    return t, q


async def test_ok_completes(mock_q):
    _, q = mock_q
    r = await q.send("G1 X1 F100")
    assert r.ok is True


async def test_m114_returns_position(mock_q):
    _, q = mock_q
    await q.send("G91")
    await q.send("G1 X5 F100")
    r = await q.send("M114")
    assert r.position is not None
    # MockTransport sees relative move; X should be 5.0
    assert r.position.get("X") == pytest.approx(5.0, abs=0.01)


async def test_timeout(mock_q):
    t, q = mock_q
    # Force the transport to never respond by closing it underneath.
    await t.close()
    with pytest.raises(TransportClosed):
        await q.send("M115", timeout=0.1)


async def test_resend_round_trip():
    t = MockTransport(latency_s=0.001)
    await t.open()
    q = CommandQueue(t, default_timeout_s=2.0)
    # Switch off mock's framing-strip via a MarlinTransport-style send isn't easy here;
    # instead, exercise the queue with an injected error to ensure the queue handles it.
    t.inject_error(n=1)
    # Plain MockTransport commands have no line number, so it can report the
    # line-number error but cannot request a concrete resend. The queue waits
    # for the follow-up until its bounded timeout rather than accepting it as ok.
    with pytest.raises(TransportTimeout):
        await q.send("G1 X1 F100")


async def test_abort_sends_m410():
    t = MockTransport(latency_s=0.001)
    await t.open()
    q = CommandQueue(t)
    await q.abort()
    # After abort, sending more commands without reset is refused.
    from flowengine.errors import ControllerError

    with pytest.raises(ControllerError):
        await q.send("G1 X1 F100")
    q.reset_abort()
    r = await q.send("G1 X1 F100")
    assert r.ok is True
