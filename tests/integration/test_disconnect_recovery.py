"""MockTransport disconnect injection — controller must refuse further commands."""

from __future__ import annotations

import pytest

from flowengine.errors import TransportClosed
from flowengine.hardware.queue import CommandQueue
from flowengine.transport.mock import MockTransport


async def test_disconnect_after_n_commands():
    t = MockTransport(latency_s=0.001, disconnect_after=2)
    await t.open()
    q = CommandQueue(t, default_timeout_s=1.0)
    # First two should land an ok.
    await q.send("G1 X1 F100")
    await q.send("G1 X2 F100")
    # Third send raises TransportClosed (or times out waiting for ok).
    with pytest.raises(Exception):
        await q.send("G1 X3 F100", timeout=0.3)


async def test_force_disconnect_mid_idle():
    t = MockTransport(latency_s=0.001)
    await t.open()
    assert t.is_connected()
    t.force_disconnect()
    assert not t.is_connected()
    with pytest.raises(TransportClosed):
        await t.send_line("G1 X1 F100")
