"""Marlin transport — line-numbered checksum framing over USB-CDC.

What this layer does:
- Owns the serial port (one reader task, one writer task).
- Adds `N<n> ... *<cs>` framing on send.
- Handles `Resend: N` requests by re-emitting the requested line.
- Does NOT classify `ok`/`Error:`. That is the parser's job (one layer up).

Why checksum framing from day one: Marlin will accept un-numbered lines, but the
moment your USB cable has a glitch you get a silently-dropped command and a
silently-drifted motor. With checksum framing, the protocol error becomes
observable and recoverable. See docs/GCODE_PROTOCOL.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import Literal

import serial  # pyserial
import serial_asyncio  # pyserial-asyncio
from serial.tools import list_ports

from flowengine.errors import TransportClosed, TransportProtocolError, TransportTimeout
from flowengine.transport.base import Transport
from flowengine.transport.parser import frame_with_line_number

log = logging.getLogger(__name__)


def discover_marlin_port(configured: str | None = None) -> str:
    """Resolve the USB CDC Marlin port across Linux, macOS, and Windows."""
    ports = list(list_ports.comports())
    if configured and configured.lower() != "auto":
        if any(port.device == configured for port in ports):
            return configured
        log.warning("configured serial port %s is absent; trying auto-discovery", configured)
    matches = [
        port
        for port in ports
        if (port.vid, port.pid) == (0x0483, 0x5740) or "marlin" in (port.product or "").lower()
    ]
    if len(matches) == 1:
        return matches[0].device
    if not matches:
        visible = ", ".join(port.device for port in ports) or "none"
        raise TransportProtocolError(f"Marlin USB serial port not found (visible: {visible})")
    devices = ", ".join(port.device for port in matches)
    raise TransportProtocolError(f"multiple Marlin serial ports found: {devices}")


class MarlinTransport(Transport):
    name: Literal["marlin", "mock", "klipper"] = "marlin"

    def __init__(self, port: str, baud: int = 115_200, *, use_line_numbers: bool = False) -> None:
        self._port = port
        self._baud = baud
        self._use_line_numbers = use_line_numbers
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._line_no = 1
        self._last_sent: dict[int, str] = {}
        self._send_lock = asyncio.Lock()
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._read_task: asyncio.Task[None] | None = None
        self._closed = True

    async def open(self) -> None:
        if not self._closed:
            return
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self._port, baudrate=self._baud
            )
        except (OSError, serial.SerialException) as e:
            raise TransportProtocolError(f"could not open {self._port}: {e}") from e
        self._closed = False
        self._line_no = 1
        self._last_sent.clear()
        self._read_task = asyncio.create_task(self._read_loop(), name="marlin-read")
        # Opening the port toggles DTR, which resets the board. Wait for Marlin's
        # 'start' banner before sending anything, otherwise the first command
        # lands while the bootloader is still running and we lose sync.
        deadline = time.monotonic() + 10.0
        saw_start = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = await asyncio.wait_for(self._inbox.get(), timeout=remaining)
            except TimeoutError:
                break
            if line.strip().lower() == "start":
                saw_start = True
                break
        if not saw_start:
            log.warning("did not see Marlin 'start' banner within 10s, proceeding anyway")
        # Do not leave an unconsumed M110 `ok` in the inbox: it would be
        # mistaken for the acknowledgement of the next command.
        if self._use_line_numbers:
            await self.send_line("M110 N0", _bypass_lineno=True)
            await self._wait_for_reset_ok()
        log.info("marlin transport opened on %s @ %d", self._port, self._baud)

    @property
    def port(self) -> str:
        return self._port

    @property
    def baud(self) -> int:
        return self._baud

    async def close(self) -> None:
        self._closed = True
        if self._read_task:
            self._read_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None
        if self._writer:
            self._writer.close()
            with suppress(Exception):
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None

    def is_connected(self) -> bool:
        return not self._closed and self._writer is not None

    async def send_line(self, line: str, *, _bypass_lineno: bool = False) -> None:
        if self._closed or self._writer is None:
            raise TransportClosed("marlin transport is not open")
        async with self._send_lock:
            payload = line.strip()
            if _bypass_lineno or not self._use_line_numbers:
                # Used only for M110 (reset line counter) — itself unframed.
                framed = payload
            else:
                n = self._line_no
                framed = frame_with_line_number(n, payload)
                self._last_sent[n] = payload
                self._line_no += 1
                # Memory hygiene: keep the last 64 lines for resend.
                if len(self._last_sent) > 64:
                    oldest = min(self._last_sent)
                    self._last_sent.pop(oldest, None)
            assert self._writer is not None
            self._writer.write((framed + "\n").encode("ascii", errors="replace"))
            await self._writer.drain()
            log.debug("→ %s", framed)

    async def _wait_for_reset_ok(self) -> None:
        deadline = time.monotonic() + 3.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeout("no ok after M110 line-number reset")
            line = await self.read_line(timeout=remaining)
            if line.strip().lower().startswith("ok"):
                return
            if line.strip().lower().startswith("error"):
                raise TransportProtocolError(f"M110 line-number reset failed: {line}")

    async def read_line(self, timeout: float | None = None) -> str:
        if self._closed:
            raise TransportClosed("marlin transport is not open")
        try:
            if timeout is None:
                return await self._inbox.get()
            return await asyncio.wait_for(self._inbox.get(), timeout=timeout)
        except TimeoutError as e:
            raise TransportTimeout(f"no response within {timeout}s") from e

    async def resend(self, line_no: int) -> None:
        """Called by the queue layer on `Resend: N`."""
        payload = self._last_sent.get(line_no)
        if payload is None:
            raise TransportProtocolError(
                f"resend requested for line {line_no} that we no longer have"
            )
        async with self._send_lock:
            framed = frame_with_line_number(line_no, payload)
            assert self._writer is not None
            self._writer.write((framed + "\n").encode("ascii", errors="replace"))
            await self._writer.drain()
            log.warning("↺ resent N%d: %s", line_no, payload)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while not self._closed:
                raw = await self._reader.readline()
                if not raw:
                    # EOF: USB disconnect.
                    self._closed = True
                    log.warning("marlin transport read EOF (likely disconnect)")
                    break
                line = raw.decode("latin-1", errors="replace").rstrip("\r\n")
                log.debug("← %s", line)
                await self._inbox.put(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("marlin read loop crashed")
            self._closed = True
