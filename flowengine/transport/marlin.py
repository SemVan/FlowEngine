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
from contextlib import suppress
from typing import Literal

import serial  # pyserial
import serial_asyncio  # pyserial-asyncio

from flowengine.errors import TransportClosed, TransportProtocolError, TransportTimeout
from flowengine.transport.base import Transport
from flowengine.transport.parser import frame_with_line_number

log = logging.getLogger(__name__)


class MarlinTransport(Transport):
    name: Literal["marlin", "mock", "klipper"] = "marlin"

    def __init__(self, port: str, baud: int = 250_000) -> None:
        self._port = port
        self._baud = baud
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
        # Reset Marlin's line-number counter to ours.
        await self.send_line("M110 N0", _bypass_lineno=True)
        log.info("marlin transport opened on %s @ %d", self._port, self._baud)

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
            if _bypass_lineno:
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

    async def read_line(self, timeout: float | None = None) -> str:
        if self._closed:
            raise TransportClosed("marlin transport is not open")
        try:
            if timeout is None:
                return await self._inbox.get()
            return await asyncio.wait_for(self._inbox.get(), timeout=timeout)
        except asyncio.TimeoutError as e:
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
        except Exception:  # noqa: BLE001
            log.exception("marlin read loop crashed")
            self._closed = True
