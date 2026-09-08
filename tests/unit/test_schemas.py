"""Pydantic schema coverage — round-trips and rejection of bad input."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from flowengine.schemas import (
    AxisConfig,
    DeviceKind,
    DeviceMap,
    Procedure,
    RuntimeParams,
)


def test_axis_rejects_defaults_above_max():
    with pytest.raises(ValidationError):
        AxisConfig(
            marlin_axis="X",
            name="x",
            kind=DeviceKind.AUTOSAMPLER_AXIS,
            steps_per_unit=80,
            travel=200,
            home_direction="min",
            feedrate_default=9999,
            feedrate_max=3000,
        )


def test_device_map_rejects_duplicate_axes():
    a = AxisConfig(
        marlin_axis="X",
        name="x",
        kind=DeviceKind.AUTOSAMPLER_AXIS,
        steps_per_unit=80,
        travel=200,
        home_direction="min",
        feedrate_default=100,
        feedrate_max=500,
    )
    with pytest.raises(ValidationError):
        DeviceMap(instrument_id="t", axes=[a, a])


def test_runtime_defaults_load():
    rp = RuntimeParams()
    assert rp.transport.baud == 115_200
    assert rp.motion.enabled is False
    assert rp.motion.configure_firmware is False
    assert rp.timeouts.move > rp.timeouts.diagnostics


def test_custom_marlin_axis_is_supported():
    axis = AxisConfig(
        marlin_axis="U",
        name="wash",
        kind=DeviceKind.PERISTALTIC_PUMP,
        steps_per_unit=1,
        travel=100,
        home_direction="min",
        feedrate_default=10,
        feedrate_max=20,
    )
    assert axis.marlin_axis == "U"


def test_procedure_discriminated_union():
    p = Procedure(
        name="t",
        steps=[
            {"op": "home", "axes": ["X"]},
            {"op": "move", "axis": "X", "to": 5.0},
            {"op": "dwell", "seconds": 1.0},
            {"op": "log", "message": "ok"},
        ],
    )
    assert len(p.steps) == 4
    assert p.steps[1].op == "move"  # type: ignore[union-attr]


def test_move_requires_to_xor_by():
    with pytest.raises(ValidationError):
        Procedure(name="t", steps=[{"op": "move", "axis": "X"}])  # neither
    with pytest.raises(ValidationError):
        Procedure(name="t", steps=[{"op": "move", "axis": "X", "to": 1, "by": 2}])  # both


def test_procedure_parameters_are_resolved_and_bounded():
    procedure = Procedure.model_validate(
        {
            "name": "parameterized",
            "parameters": {"travel": {"type": "number", "minimum": 0.1, "maximum": 10.0}},
            "steps": [{"op": "move", "axis": "X", "by": "${travel}"}],
        }
    )
    resolved = procedure.resolve({"travel": 2.5})
    assert resolved.steps[0].by == 2.5  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="above"):
        procedure.resolve({"travel": 20})


def test_draft_procedure_only_resolves_for_preview_or_single_step():
    procedure = Procedure(
        name="draft",
        draft=True,
        parameters={"distance": {"type": "number", "default": 1.5}},
        steps=[{"op": "move", "axis": "X", "by": "${distance}"}],
    )
    with pytest.raises(ValueError, match="draft"):
        procedure.resolve()
    resolved = procedure.resolve(allow_draft=True)
    assert resolved.draft is False
    assert resolved.steps[0].by == 1.5  # type: ignore[union-attr]
