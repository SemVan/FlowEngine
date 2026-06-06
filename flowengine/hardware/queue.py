"""Command queue — single-writer, single-line `ok` handshake.

This is where serial discipline lives. Cardinal rules:
1. One command in flight at a time. The next send blocks until `ok` for the
   current command lands. We *do not* pipeline.
2. Every response line is classified. Unknown lines log WARNING and are dropped;
   they are never treated as an implicit `ok`.
3. Every command class has a timeout. A timeout is an error, not a hang.
4. Abort (`M410`) bypasses the queue: it's emitted out-of-band, then we drain
   pending `ok`s and mark the queue as aborted. The current command's awaiter
   gets `TransportTimeout`/cancellation.
5. On `Resend: N`, the transport layer (Marlin) re-frames + resends; we keep
   waiting for our `ok`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from flowengine.errors import (
    ControllerError,
    TransportClosed,
    TransportProtocolError,
    TransportTimeout,
)
from flowengine.transport.base import Transport
from flowengine.transport.marlin import MarlinTransport
from flowengine.transport.parser import (
    BusyEcho,
    EchoLine,
    EndstopsResponse,
    ErrorResponse,
    FirmwareCapsResponse,
    KillResponse,
    OkResponse,
    PositionResponse,
    ResendRequest,
    TemperatureResponse,
    aggregate_endstops,
    parse_line,
)

log = logging.getLogger(__name__)


@dataclass
class CommandResult:
    ok: bool
    raw: list[str] = field(default_factory=list)
    position: dict[str, float] | None = None
    endstops: dict[str, bool] | None = None
    firmware_caps: frozenset[str] | None = None
    firmware_raw: str | None = None
    error: str | None = None


class CommandQueue:
    """Owns the await-ok loop. Hand it commands; get results back."""

    def __init__(self, transport: Transport, *, default_timeout_s: float = 5.0) -> None:
        self._transport = transport
        self._default_timeout = default_timeout_s
        self._send_lock = asyncio.Lock()
        self._aborted = asyncio.Event()
        self._depth = 0

    @property
    def depth(self) -> int:
        return self._depth

    async def send(self, gcode: str, *, timeout: float | None = None) -> CommandResult:
        """Send a single G-code line. Returns when `ok` arrives or timeout/error fires."""
        if self._aborted.is_set():
            raise ControllerError("queue is aborted; reset before sending more commands")
        timeout = timeout if timeout is not None else self._default_timeout
        self._depth += 1
        try:
            async with self._send_lock:
                await self._transport.send_line(gcode)
                return await self._await_ok(gcode, timeout)
        finally:
            self._depth -= 1

    async def abort(self) -> None:
        """Out-of-band quickstop: send `M410` immediately and discard the in-flight wait."""
        self._aborted.set()
        try:
            await self._transport.send_line("M410")
        except TransportClosed:
            # If we're disconnected anyway, that's fine — the device will stop on its own.
            log.warning("abort attempted while transport closed")

    def reset_abort(self) -> None:
        """Operator acknowledged the abort; the queue is usable again."""
        self._aborted.clear()

    async def _await_ok(self, sent: str, timeout: float) -> CommandResult:
        """Read response lines until we see `ok` (or fail). Aggregate context."""
        result = CommandResult(ok=False)
        deadline = asyncio.get_running_loop().time() + timeout
        endstop_lines: list[str] = []
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TransportTimeout(f"no ok for {sent!r} within {timeout}s")
            try:
                line = await self._transport.read_line(timeout=remaining)
            except TransportTimeout:
                raise
            result.raw.append(line)
            resp = parse_line(line)
            match resp:
                case OkResponse():
                    if endstop_lines:
                        agg = aggregate_endstops(endstop_lines)
                        result.endstops = agg.triggered
                    result.ok = True
                    return result
                case EchoLine() | BusyEcho() | TemperatureResponse():
                    # Informational; keep waiting.
                    if isinstance(resp, BusyEcho):
                        # Marlin says "still working" — restart the deadline.
                        deadline = asyncio.get_running_loop().time() + timeout
                case PositionResponse(positions=pos):
                    result.position = pos
                case EndstopsResponse():
                    endstop_lines.append(line)
                case FirmwareCapsResponse(raw=raw, capabilities=caps):
                    result.firmware_raw = raw
                    result.firmware_caps = caps
                case ErrorResponse(message=msg):
                    if "checksum" in msg.lower() or "line number" in msg.lower():
                        # The transport gets the resend request *separately* on the next line.
                        continue
                    result.error = msg
                    raise ControllerError(f"Marlin error: {msg}")
                case ResendRequest(line_number=n):
                    if isinstance(self._transport, MarlinTransport):
                        await self._transport.resend(n)
                        continue
                    raise TransportProtocolError(
                        f"transport asked for resend of N{n} but transport doesn't support it"
                    )
                case KillResponse():
                    result.error = "controller killed (!!)"
                    raise TransportProtocolError("Marlin issued kill (!!) — reset required")
                case _:
                    # Unknown line: log and drop, but never treat as ok.
                    log.warning("unclassified line while awaiting ok for %r: %r", sent, line)
