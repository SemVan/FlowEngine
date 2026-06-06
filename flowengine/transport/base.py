"""Transport interface — abstract serial-link wire layer.

Concrete implementations: `MarlinTransport` (real hardware), `MockTransport`
(simulation), `KlipperTransport` (stub).

Contract:
- `open()` connects, `close()` disconnects. Idempotent.
- `send_line(text)` puts a single line on the wire (no framing — the transport
  adds checksum/line-number if its protocol demands it).
- `read_line()` returns the next line from the device (without trailing CR/LF).
- All operations are awaitable; the implementation must not block the event loop.
- The transport never *interprets* responses beyond what's needed to do framing
  (e.g., the Marlin transport understands checksum-resend at the wire level but
  classifies `ok`/`Error:` at the parser layer above).
"""

from __future__ import annotations

import abc
from typing import Literal


TransportName = Literal["marlin", "mock", "klipper"]


class Transport(abc.ABC):
    name: TransportName

    @abc.abstractmethod
    async def open(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def send_line(self, line: str) -> None:
        """Send one G-code line. The implementation handles framing."""

    @abc.abstractmethod
    async def read_line(self, timeout: float | None = None) -> str:
        """Block until the next line is available. Raises TransportTimeout on deadline."""

    @abc.abstractmethod
    def is_connected(self) -> bool: ...
