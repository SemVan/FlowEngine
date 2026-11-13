
from __future__ import annotations

import pytest

from flowengine.hardware.queue import CommandQueue
from flowengine.schemas.runtime import TransportConfig
from flowengine.transport.marlin import MarlinTransport


class _ScriptedWriter:

    def __init__(self, transport: MarlinTransport) -> None:
        self.transport = transport
        self.writes: list[str] = []
        self.m115_count = 0

    def write(self, data: bytes) -> None:
        line = data.decode("ascii").strip()
        self.writes.append(line)

        if "M110 N0" in line:
            self.transport._inbox.put_nowait("ok")
            return

        if "M115" in line:
            self.m115_count += 1
            if self.m115_count == 1:
                self.transport._inbox.put_nowait(
                    "Error:Line Number is not Last Line Number+1, Last Line: -1"
                )
                self.transport._inbox.put_nowait("Resend: 0")
                # при запросе повтора прошивка может прислать старый ok
                self.transport._inbox.put_nowait("ok")
            else:
                self.transport._inbox.put_nowait(
                    "FIRMWARE_NAME:Marlin 2.1.2 Cap:CHECKSUM:1"
                )
                self.transport._inbox.put_nowait("ok")

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


@pytest.mark.parametrize("use_line_numbers", [False, True])
async def test_resend_zero_resynchronizes_and_retries(use_line_numbers: bool) -> None:
    transport = MarlinTransport("COM4", use_line_numbers=use_line_numbers)
    transport._closed = False
    writer = _ScriptedWriter(transport)
    transport._writer = writer  # type: ignore[assignment]

    queue = CommandQueue(transport, default_timeout_s=2.0)
    result = await queue.send("M115")

    assert result.ok is True
    assert result.firmware_raw is not None
    assert "Marlin" in result.firmware_raw
    assert "N0 M110 N0*125" in writer.writes
    assert writer.m115_count == 2


async def test_sync_frame_resets_next_numbered_command_to_n1() -> None:
    transport = MarlinTransport("COM4", use_line_numbers=True)
    transport._closed = False
    writer = _ScriptedWriter(transport)
    transport._writer = writer  # type: ignore[assignment]

    await transport.resynchronize()
    await transport.send_line("G90")

    assert writer.writes[0] == "N0 M110 N0*125"
    assert writer.writes[1].startswith("N1 G90*")


def test_transport_default_is_cross_platform_auto() -> None:
    assert TransportConfig().port == "auto"
