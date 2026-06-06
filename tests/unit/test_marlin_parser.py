"""Parser must classify every Marlin response variant we care about."""

from __future__ import annotations

import pytest

from flowengine.transport.parser import (
    BusyEcho,
    EchoLine,
    EndstopsResponse,
    ErrorResponse,
    FirmwareCapsResponse,
    KillResponse,
    OkResponse,
    OtherResponse,
    PositionResponse,
    ResendRequest,
    TemperatureResponse,
    aggregate_endstops,
    frame_with_line_number,
    make_checksum,
    parse_line,
)


def test_ok_plain():
    assert isinstance(parse_line("ok"), OkResponse)


def test_ok_with_buffer_hint():
    assert isinstance(parse_line("ok B12"), OkResponse)


def test_resend_simple():
    r = parse_line("Resend: 42")
    assert isinstance(r, ResendRequest) and r.line_number == 42


def test_resend_with_last_line_form():
    r = parse_line("Error:checksum mismatch, Last Line: 41")
    # This one is classified as Error first; the Resend line is parsed separately.
    assert isinstance(r, ErrorResponse)
    assert "checksum" in r.message.lower()


def test_error_generic():
    r = parse_line("Error:Printer halted. kill() called!")
    assert isinstance(r, ErrorResponse) and "halted" in r.message.lower()


def test_busy():
    assert isinstance(parse_line("echo:busy: processing"), BusyEcho)


def test_echo_other():
    r = parse_line("echo:M203 X1000")
    assert isinstance(r, EchoLine) and "M203" in r.message


def test_temperature():
    assert isinstance(parse_line("T:23.5 /0.0"), TemperatureResponse)


def test_position():
    r = parse_line("X:10.50 Y:0.00 Z:0.00 E:0.00")
    assert isinstance(r, PositionResponse)
    assert r.positions["X"] == 10.5 and r.positions["E"] == 0.0


def test_firmware_caps():
    r = parse_line("FIRMWARE_NAME:Marlin 2.1.2 Cap:CHECKSUM:1 Cap:AUTOREPORT_TEMP:0")
    assert isinstance(r, FirmwareCapsResponse)
    assert "CHECKSUM" in r.capabilities
    assert "AUTOREPORT_TEMP" not in r.capabilities


def test_kill():
    assert isinstance(parse_line("!!"), KillResponse)


def test_endstops_aggregate():
    lines = ["x_min: TRIGGERED", "y_min: open", "z_max: open"]
    agg = aggregate_endstops(lines)
    assert isinstance(agg, EndstopsResponse)
    assert agg.triggered["x_min"] is True
    assert agg.triggered["y_min"] is False


def test_unknown_returns_other():
    assert isinstance(parse_line("zwxyzwxyz"), OtherResponse)


def test_blank_returns_other():
    assert isinstance(parse_line(""), OtherResponse)


@pytest.mark.parametrize(
    "line,expected_cs",
    [
        ("N3 G1 X10.0 F600", make_checksum("N3 G1 X10.0 F600")),
        ("N0 M110 N0", make_checksum("N0 M110 N0")),
    ],
)
def test_framing(line, expected_cs):
    framed = frame_with_line_number(int(line[1:].split()[0]), line.split(" ", 1)[1])
    # body and *cs must round-trip
    body, cs = framed.rsplit("*", 1)
    assert int(cs) == make_checksum(body)


def test_checksum_xor():
    assert make_checksum("N0 M110 N0") == 27  # known XOR result
