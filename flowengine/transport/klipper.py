"""Klipper transport — intentionally unimplemented.

If `runtime.yaml` selects `flavor: klipper`, the app must refuse to start with a
clear message pointing to docs. Migration is non-trivial: Klipper sits on a
Linux host and exposes Moonraker (HTTP/WebSocket), not a `ok`-handshake serial.

When we eventually implement this:
- Talk to Moonraker over WS, not the MCU's serial.
- Replace the command-queue layer (Klipper has its own queueing).
- Re-express our G-code as Klipper macros where appropriate.
"""

from __future__ import annotations

from typing import Literal

from flowengine.transport.base import Transport


class KlipperTransport(Transport):
    name: Literal["marlin", "mock", "klipper"] = "klipper"

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "KlipperTransport is not implemented yet. "
            "Either flash Marlin onto the MKS Monster8 MK2, or contribute a Moonraker-backed "
            "transport — see docs/ARCHITECTURE.md (Transport layer) and docs/GCODE_PROTOCOL.md "
            "for the contract you must satisfy."
        )

    async def open(self) -> None:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        raise NotImplementedError

    async def send_line(self, line: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def read_line(self, timeout: float | None = None) -> str:  # pragma: no cover
        raise NotImplementedError

    def is_connected(self) -> bool:  # pragma: no cover
        return False
