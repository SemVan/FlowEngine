"""Mock transport — runs the entire stack without hardware.

Mirrors Marlin's response semantics including:
- Configurable per-command latency.
- Injected `Error:checksum mismatch, Last Line: N` so resend handling exercises.
- Injected disconnect so reconnect / re-home flows exercise.
- A simulated position model that updates on G0/G1/G28 and answers M114 consistently.
- Multi-line M119 / M115 responses.

This module is the foundation of the test suite. If you change Marlin's
real-wire behavior, mirror it here too — otherwise the bench will surface
bugs that CI didn't.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import defaultdict
from typing import Literal

from flowengine.errors import TransportClosed, TransportTimeout
from flowengine.transport.base import Transport

log = logging.getLogger(__name__)

_AXIS_TOKEN = re.compile(r"([XYZABCUVWIJK]|E\d*)([-+]?\d+(?:\.\d+)?)")
_HOME_AXIS_TOKEN = re.compile(r"\b([XYZABCUVWIJKE]\d*)\b")
_LINE_PREFIX = re.compile(r"^\s*N(\d+)\s+(.*?)\*\d+\s*$")


class MockTransport(Transport):
    name: Literal["marlin", "mock", "klipper"] = "mock"

    def __init__(
        self,
        *,
        latency_s: float = 0.005,
        error_every_n: int = 0,
        disconnect_after: int | None = None,
        probe_strokes: dict[str, float] | None = None,
        probe_channel: str = "probe",
    ) -> None:
        self._latency = latency_s
        self._error_every_n = error_every_n
        self._disconnect_after = disconnect_after
        self._closed = True
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._sent_count = 0
        self._positions: dict[str, float] = defaultdict(float)
        self._homed: dict[str, bool] = {}
        self._absolute = True
        self._injected_errors_remaining = 0
        self.commands: list[str] = []
        self.probe_strokes = probe_strokes or {}
        self.probe_channel = probe_channel
        self.mechanical = {a: s/2 for a,s in self.probe_strokes.items()}
        self.step_loss_fraction = 0.0
        self.step_loss_direction: int | None = None
        self.probe_stuck: bool | None = None
        self.driver_enabled: dict[str, bool] = {}
        self._steps_per_unit: dict[str, float] = defaultdict(lambda: 80.0)
        self._counts: dict[str, int] = defaultdict(int)
        self._acceleration = {"P": 3000.0, "R": 3000.0, "T": 3000.0}

    async def open(self) -> None:
        self._closed = False
        self._sent_count = 0
        log.info("mock transport opened")

    async def close(self) -> None:
        self._closed = True

    def is_connected(self) -> bool:
        return not self._closed

    # --- testing knobs ---------------------------------------------------

    def inject_error(self, n: int = 1) -> None:
        """Next `n` commands will respond with checksum-mismatch on first attempt."""
        self._injected_errors_remaining += n

    def force_disconnect(self) -> None:
        self._closed = True

    @property
    def positions(self) -> dict[str, float]:
        return dict(self._positions)

    # --- Transport protocol ---------------------------------------------

    async def send_line(self, line: str, *, _bypass_lineno: bool = False) -> None:
        if self._closed:
            raise TransportClosed("mock transport is not open")
        async with self._send_lock:
            self._sent_count += 1
            await asyncio.sleep(self._latency)
            if self._disconnect_after is not None and self._sent_count > self._disconnect_after:
                self._closed = True
                log.warning("mock transport simulated disconnect at command %d", self._sent_count)
                return

            n, payload = self._strip_framing(line)
            self.commands.append(payload)

            if self._injected_errors_remaining > 0:
                self._injected_errors_remaining -= 1
                # Pretend checksum was wrong → ask for resend.
                if n is not None:
                    await self._inbox.put(f"Error:checksum mismatch, Last Line: {n - 1}")
                    await self._inbox.put(f"Resend: {n}")
                else:
                    await self._inbox.put("Error:Line Number is not Last Line Number+1")
                return

            await self._handle(payload)

    async def read_line(self, timeout: float | None = None) -> str:
        if self._closed and self._inbox.empty():
            raise TransportClosed("mock transport is not open")
        try:
            if timeout is None:
                return await self._inbox.get()
            return await asyncio.wait_for(self._inbox.get(), timeout=timeout)
        except TimeoutError as e:
            raise TransportTimeout(f"no response within {timeout}s") from e

    # --- private --------------------------------------------------------

    @staticmethod
    def _strip_framing(line: str) -> tuple[int | None, str]:
        m = _LINE_PREFIX.match(line)
        if m:
            return int(m.group(1)), m.group(2).strip()
        return None, line.strip()

    async def _handle(self, payload: str) -> None:
        p = payload.upper()

        if p.startswith("M110"):
            await self._inbox.put("ok")
            return
        if p == "M115" or p.startswith("M115"):
            await self._inbox.put(
                "FIRMWARE_NAME:MockMarlin 2.x SOURCE_CODE_URL:mock Cap:CHECKSUM:1 Cap:M114_DETAIL:1"
            )
            await self._inbox.put("ok")
            return
        if p.startswith("M119"):
            for axis in ("x", "y", "z"):
                await self._inbox.put(
                    f"{axis}_min: {'TRIGGERED' if self._homed.get(axis.upper(), False) else 'open'}"
                )
            if self.probe_strokes:
                triggered = self.probe_stuck if self.probe_stuck is not None else any(
                    self.mechanical[a] <= 0 or self.mechanical[a] >= stroke for a,stroke in self.probe_strokes.items())
                await self._inbox.put(f"{self.probe_channel}: {'TRIGGERED' if triggered else 'open'}")
            await self._inbox.put("ok")
            return
        if p.startswith("M114"):
            x = self._positions.get("X", 0.0)
            y = self._positions.get("Y", 0.0)
            z = self._positions.get("Z", 0.0)
            e = self._positions.get("E0", 0.0)
            extra = " ".join(f"{a}:{v:.4f}" for a,v in self._positions.items() if a not in {"X", "Y", "Z", "E0"} and len(a) == 1)
            counts = " ".join(f"{a}:{v}" for a,v in self._counts.items() if len(a) == 1)
            await self._inbox.put(f"X:{x:.4f} Y:{y:.4f} Z:{z:.4f} E:{e:.4f} {extra} Count {counts}")
            await self._inbox.put("ok")
            return
        if p.startswith("M105"):
            await self._inbox.put("ok T:25.0 /0.0 T0:25.0 /0.0 B:24.5 /0.0")
            return
        if p.startswith("M503"):
            await self._inbox.put("echo: M204 " + " ".join(f"{k}{v:.3f}" for k,v in self._acceleration.items()))
            await self._inbox.put("ok")
            return
        if p.startswith("M204"):
            for k,v in re.findall(r"([PRT])([0-9.]+)", p):
                self._acceleration[k] = float(v)
            await self._inbox.put("ok")
            return
        if p.startswith(("M17", "M18", "M84")):
            axes = _HOME_AXIS_TOKEN.findall(p[3:]) or list(self._positions) or ["X", "Y", "Z"]
            for a in axes:
                self.driver_enabled[a] = p.startswith("M17")
            await self._inbox.put("ok")
            return
        if p.startswith("G38.2"):
            tokens = _AXIS_TOKEN.findall(p)
            for a,v in tokens:
                if a not in self.probe_strokes:
                    await self._inbox.put("Error:unsupported mock probe axis")
                    return
                delta = float(v) - self._positions[a]
                contact = 0.0 if delta < 0 else self.probe_strokes[a]
                needed = contact - self.mechanical[a]
                if self.probe_stuck is False or abs(needed) > abs(delta):
                    await self._inbox.put("Error:Failed to reach probe")
                    return
                self._positions[a] += needed
                self._counts[a] += round(needed*self._steps_per_unit[a])
                self.mechanical[a] = contact
            await self._inbox.put("ok")
            return
        if (
            p.startswith("M400")
            or p.startswith("M105")
            or p.startswith("M201")
            or p.startswith("M203")
            or p.startswith("M906")
        ):
            await self._inbox.put("ok")
            return
        if p.startswith("M410"):
            await self._inbox.put("ok")
            return
        if p.startswith("M92"):
            for a,v in _AXIS_TOKEN.findall(p):
                self._steps_per_unit[a] = float(v)
            await self._inbox.put("ok")
            return
        if p.startswith("G90"):
            self._absolute = True
            await self._inbox.put("ok")
            return
        if p.startswith("G91"):
            self._absolute = False
            await self._inbox.put("ok")
            return
        if p.startswith("G92"):
            for axis, val in _AXIS_TOKEN.findall(payload):
                self._positions[axis] = float(val)
            await self._inbox.put("ok")
            return
        if p.startswith("G28"):
            tokens = (
                _HOME_AXIS_TOKEN.findall(payload[3:])
                or list(self._positions.keys())
                or ["X", "Y", "Z"]
            )
            for axis in tokens:
                self._positions[axis] = 0.0
                self._homed[axis] = True
            # Simulate some homing latency (above the per-line latency).
            await asyncio.sleep(self._latency * 4)
            await self._inbox.put("ok")
            return
        if p.startswith(("G0", "G1")):
            for axis, val in _AXIS_TOKEN.findall(payload):
                v = float(val)
                old = self._positions.get(axis, 0.0)
                if self._absolute:
                    self._positions[axis] = v
                else:
                    self._positions[axis] = self._positions.get(axis, 0.0) + v
                delta = self._positions[axis]-old
                self._counts[axis] += round(delta*self._steps_per_unit[axis])
                if axis in self.mechanical:
                    loss = self.step_loss_fraction if self.step_loss_direction is None or (delta > 0) == (self.step_loss_direction > 0) else 0
                    self.mechanical[axis] += delta * (1-loss)
            # Tiny random jitter so tests can't depend on exact micro-timing.
            await asyncio.sleep(self._latency * (1 + random.random()))
            await self._inbox.put("ok")
            return

        # Unknown command — Marlin echoes ok by default.
        await self._inbox.put("ok")
