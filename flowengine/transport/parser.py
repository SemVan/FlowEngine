"""Classify Marlin response lines into typed records.

The set of variants we care about is documented in docs/GCODE_PROTOCOL.md.
Unknown lines come back as `Other` — the caller decides what to do (typically
log at WARNING and discard, *never* treat as an implicit `ok`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class OkResponse:
    kind: Literal["ok"] = "ok"
    raw: str = ""


@dataclass(frozen=True, slots=True)
class ResendRequest:
    """Marlin asks us to resend starting at line N."""

    line_number: int
    kind: Literal["resend"] = "resend"
    raw: str = ""


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    message: str
    kind: Literal["error"] = "error"
    raw: str = ""


@dataclass(frozen=True, slots=True)
class BusyEcho:
    kind: Literal["busy"] = "busy"
    raw: str = ""


@dataclass(frozen=True, slots=True)
class EchoLine:
    """An informational `echo:` line that isn't an `ok` or `busy`."""

    message: str
    kind: Literal["echo"] = "echo"
    raw: str = ""


@dataclass(frozen=True, slots=True)
class PositionResponse:
    positions: dict[str, float]
    kind: Literal["position"] = "position"
    raw: str = ""


@dataclass(frozen=True, slots=True)
class EndstopsResponse:
    triggered: dict[str, bool]
    kind: Literal["endstops"] = "endstops"
    raw: str = ""


@dataclass(frozen=True, slots=True)
class FirmwareCapsResponse:
    """`M115` response: parsed into raw plus a flat capability set."""

    raw: str
    capabilities: frozenset[str]
    kind: Literal["firmware"] = "firmware"


@dataclass(frozen=True, slots=True)
class TemperatureResponse:
    """`T:` / `B:` line. We don't run heaters but keep this so we don't mis-classify."""

    raw: str
    kind: Literal["temperature"] = "temperature"


@dataclass(frozen=True, slots=True)
class KillResponse:
    """Marlin's `!!` — printer halted; reset required."""

    kind: Literal["kill"] = "kill"
    raw: str = ""


@dataclass(frozen=True, slots=True)
class OtherResponse:
    raw: str
    kind: Literal["other"] = "other"


Response = (
    OkResponse
    | ResendRequest
    | ErrorResponse
    | BusyEcho
    | EchoLine
    | PositionResponse
    | EndstopsResponse
    | FirmwareCapsResponse
    | TemperatureResponse
    | KillResponse
    | OtherResponse
)


_RESEND_RE = re.compile(r"^\s*Resend\s*:?\s*N?(\d+)", re.IGNORECASE)
_RESEND_ALT_RE = re.compile(r"Last Line:\s*N?(\d+)", re.IGNORECASE)
_POS_TOKEN_RE = re.compile(r"([A-Z]):\s*(-?\d+(?:\.\d+)?)")
_ENDSTOP_RE = re.compile(r"^\s*([a-z]_(?:min|max)|_(?:min|max))\s*:\s*(\S+)", re.IGNORECASE)
_CAP_RE = re.compile(r"Cap:([A-Z0-9_]+):(\d)")


def _parse_endstop_block(raw: str) -> dict[str, bool]:
    """`M119` returns one endstop per line, e.g. `x_min: open` / `y_max: TRIGGERED`."""
    out: dict[str, bool] = {}
    for line in raw.splitlines():
        m = re.match(r"\s*([a-z]_(?:min|max))\s*:\s*(\S+)", line, re.IGNORECASE)
        if m:
            out[m.group(1).lower()] = m.group(2).strip().lower() in {"triggered", "true", "1"}
    return out


def parse_line(line: str) -> Response:
    """Classify a *single* response line. Multi-line responses (M119, M115) are
    typically aggregated by the caller (it concatenates until the trailing `ok`)."""
    stripped = line.strip()
    if not stripped:
        return OtherResponse(raw=line)

    low = stripped.lower()

    if low == "ok" or low.startswith("ok "):
        return OkResponse(raw=line)

    if low == "!!" or low.startswith("!! "):
        return KillResponse(raw=line)

    if low.startswith("error:") or low.startswith("error "):
        return ErrorResponse(message=stripped.split(":", 1)[-1].strip(), raw=line)

    if low.startswith("resend") or "last line" in low:
        m = _RESEND_RE.search(stripped) or _RESEND_ALT_RE.search(stripped)
        if m:
            return ResendRequest(line_number=int(m.group(1)), raw=line)

    if low.startswith("echo:busy"):
        return BusyEcho(raw=line)

    if low.startswith("echo:"):
        return EchoLine(message=stripped[5:].strip(), raw=line)

    if stripped.startswith("T:") or stripped.startswith("B:"):
        return TemperatureResponse(raw=line)

    # `FIRMWARE_NAME:Marlin ...`
    if stripped.startswith("FIRMWARE_NAME"):
        caps = {m.group(1) for m in _CAP_RE.finditer(stripped) if m.group(2) == "1"}
        return FirmwareCapsResponse(raw=line, capabilities=frozenset(caps))

    # Many Marlin builds print capabilities on separate lines after the main
    # FIRMWARE_NAME line.
    if stripped.startswith("Cap:"):
        caps = {m.group(1) for m in _CAP_RE.finditer(stripped) if m.group(2) == "1"}
        return FirmwareCapsResponse(raw=line, capabilities=frozenset(caps))

    # M114 position response: "X:10.00 Y:0.00 Z:0.00 E:0.00 Count X:80 Y:0 Z:0"
    if _POS_TOKEN_RE.search(stripped) and " Count " not in stripped[:3]:
        tokens = dict(_POS_TOKEN_RE.findall(stripped))
        if tokens:
            return PositionResponse(positions={k: float(v) for k, v in tokens.items()}, raw=line)

    if _ENDSTOP_RE.match(stripped):
        return EndstopsResponse(triggered=_parse_endstop_block(stripped), raw=line)

    return OtherResponse(raw=line)


def aggregate_endstops(lines: list[str]) -> EndstopsResponse:
    """Combine the multi-line M119 block (one endstop per line) into one record."""
    triggered: dict[str, bool] = {}
    raw_parts: list[str] = []
    for ln in lines:
        triggered.update(_parse_endstop_block(ln))
        raw_parts.append(ln)
    return EndstopsResponse(triggered=triggered, raw="\n".join(raw_parts))


def make_checksum(line: str) -> int:
    """Marlin XOR-based line checksum. Skip the `*` and anything after it."""
    cs = 0
    for ch in line:
        if ch == "*":
            break
        cs ^= ord(ch)
    return cs & 0xFF


def frame_with_line_number(line_no: int, payload: str) -> str:
    """Wrap a command for the Marlin checksum protocol: `N<n> <payload>*<cs>`."""
    body = f"N{line_no} {payload}"
    return f"{body}*{make_checksum(body)}"
