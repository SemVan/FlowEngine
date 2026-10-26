"""Command queue — single-writer, single-line `ok` handshake.

This is where serial discipline lives. Cardinal rules:
1. One command in flight at a time. The next send blocks until `ok` for the
   current command lands. We *do not* pipeline.
2. Every response line is classified. Unknown lines log WARNING and are dropped;
   they are never treated as an implicit `ok`.
3. Every command class has a timeout. A timeout is an error, not a hang.
4. после M410 очередь заблокирована. старые ok не читаем; нужна новая связь.
5. On `Resend: N`, the transport layer (Marlin) re-frames + resends; we keep
   waiting for our `ok`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

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
        self._release_lock = asyncio.Lock()
        self._abort_releases: dict[str, bool] = {}
        self._depth = 0
        self.trace: deque[dict[str, str]] = deque(maxlen=1000)

    def _record(self, direction: str, line: str) -> None:
        self.trace.append(
            {"timestamp": datetime.now(UTC).isoformat(), "direction": direction, "line": line}
        )

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def faulted(self) -> bool:
        return self._aborted.is_set()

    def configure_default_timeout(self, timeout: float) -> None:
        if self._depth or timeout <= 0:
            raise ValueError("positive timeout and idle queue required")
        self._default_timeout = timeout

    @property
    def transport_name(self) -> str:
        return self._transport.name

    async def send(self, gcode: str, *, timeout: float | None = None) -> CommandResult:
        """Send a single G-code line. Returns when `ok` arrives or timeout/error fires."""
        if self._aborted.is_set():
            raise ControllerError("queue is aborted; reset before sending more commands")
        timeout = timeout if timeout is not None else self._default_timeout
        self._depth += 1
        try:
            async with self._send_lock:
                if self._aborted.is_set():
                    raise ControllerError("queue is faulted; reconnect before sending commands")
                self._record("TX", gcode)
                await self._transport.send_line(gcode)
                return await self._await_ok(gcode, timeout)
        except (ControllerError, TransportClosed, TransportTimeout, TransportProtocolError):
            # A late ACK must never be mistaken for the ACK of the next command.
            self._aborted.set()
            raise
        except asyncio.CancelledError:
            self._aborted.set()
            raise
        finally:
            self._depth -= 1

    async def abort(self) -> None:
        """Out-of-band quickstop: send `M410` immediately and discard the in-flight wait."""
        self._aborted.set()
        try:
            self._record("TX", "M410 (out-of-band stop request)")
            await self._transport.send_line("M410")
        except TransportClosed:
            # Loss of USB is NOT evidence that physical motion stopped.
            log.warning("abort attempted while transport closed")

    async def request_motor_release(self, axis: str) -> bool:
        """запрашиваем M18 после Stop; успешная отправка ещё не доказывает отключение мотора"""
        if len(axis) != 1 or not "A" <= axis <= "Z":
            raise ValueError("one uppercase motor axis required")
        if not self._aborted.is_set():
            raise ControllerError("out-of-band motor release requires an aborted queue")
        async with self._release_lock:
            if axis in self._abort_releases:
                return self._abort_releases[axis]
            self._abort_releases[axis] = False
            try:
                self._record("TX", f"M18 {axis} (out-of-band release request; unconfirmed)")
                await self._transport.send_line(f"M18 {axis}")
            except Exception:
                log.warning("motor %s release could not be sent; power state UNKNOWN", axis, exc_info=True)
            else:
                self._abort_releases[axis] = True
            return self._abort_releases[axis]

    def reset_abort(self) -> None:
        """Operator acknowledged the abort; the queue is usable again."""
        self._aborted.clear()

    async def _read_or_abort(self, timeout: float) -> str:
        """прерываем ожидание по Stop; для дальнейшей работы нужна новая связь"""
        if self._aborted.is_set():
            raise ControllerError("command interrupted by Stop; reconnect before continuing")
        reader = asyncio.create_task(self._transport.read_line(timeout=timeout))
        stopped = asyncio.create_task(self._aborted.wait())
        try:
            await asyncio.wait({reader, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (reader, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, stopped, return_exceptions=True)
        # Stop важнее ok, даже если они пришли одновременно
        if self._aborted.is_set():
            raise ControllerError("command interrupted by Stop; reconnect before continuing")
        return reader.result()

    async def _await_ok(self, sent: str, timeout: float) -> CommandResult:
        """Read response lines until we see `ok` (or fail). Aggregate context."""
        result = CommandResult(ok=False)
        deadline = asyncio.get_running_loop().time() + timeout
        endstop_lines: list[str] = []
        firmware_lines: list[str] = []
        firmware_caps: set[str] = set()
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TransportTimeout(f"no ok for {sent!r} within {timeout}s")
            try:
                line = await self._read_or_abort(remaining)
            except TransportTimeout as exc:
                raise TransportTimeout(
                    f"{sent}: no response within {timeout:.3f}s"
                ) from exc
            result.raw.append(line)
            self._record("RX", line)
            resp = parse_line(line)
            match resp:
                case OkResponse():
                    if endstop_lines:
                        agg = aggregate_endstops(endstop_lines)
                        result.endstops = agg.triggered
                    if firmware_lines:
                        result.firmware_raw = "\n".join(firmware_lines)
                        result.firmware_caps = frozenset(firmware_caps)
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
                    firmware_lines.append(raw)
                    firmware_caps.update(caps)
                case ErrorResponse(message=msg):
                    if "checksum" in msg.lower() or "line number" in msg.lower():
                        # The transport gets the resend request *separately* on the next line.
                        continue
                    result.error = msg
                    raise ControllerError(f"Marlin error: {msg}")
                case ResendRequest(line_number=n):
                    if isinstance(self._transport, MarlinTransport):
                        if n == 0:
                            await self._transport.resynchronize()
                            await self._transport.send_line(sent)
                        else:
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
