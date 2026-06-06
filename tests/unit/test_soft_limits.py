"""Soft-limit enforcement in MotionModel."""

from __future__ import annotations

import pytest

from flowengine.errors import SoftLimitError
from flowengine.hardware.motion import MotionModel


def test_refuses_motion_before_home(device_map):
    m = MotionModel(device_map, feedrate_cap=3000)
    with pytest.raises(SoftLimitError, match="not homed"):
        m.plan_relative("X", 1.0, None)


def test_within_bounds(device_map):
    m = MotionModel(device_map, feedrate_cap=3000)
    m.mark_homed(["X"])
    p = m.plan_relative("X", 10.0, None)
    assert p.target == pytest.approx(10.0)
    assert p.feedrate == pytest.approx(600.0)  # axis default


def test_above_max_rejected(device_map):
    m = MotionModel(device_map, feedrate_cap=3000)
    m.mark_homed(["X"])
    with pytest.raises(SoftLimitError, match="outside soft limits"):
        m.plan_absolute("X", 9999.0, None)


def test_below_min_rejected(device_map):
    m = MotionModel(device_map, feedrate_cap=3000)
    m.mark_homed(["X"])
    with pytest.raises(SoftLimitError, match="outside soft limits"):
        m.plan_absolute("X", -1.0, None)


def test_max_axis_negative_range(device_map):
    # For home_direction=max axes, range is [-travel, 0].
    # We construct one inline since the fixture's Z isn't present.
    from flowengine.schemas import AxisConfig, DeviceKind, DeviceMap

    z = AxisConfig(
        marlin_axis="Z",
        name="z",
        kind=DeviceKind.AUTOSAMPLER_AXIS,
        steps_per_unit=400.0,
        travel=80.0,
        home_direction="max",
        feedrate_default=300.0,
        feedrate_max=1200.0,
    )
    dm = DeviceMap(instrument_id="t", axes=[z])
    m = MotionModel(dm, feedrate_cap=3000)
    m.mark_homed(["Z"])
    # 0 is at the top; valid range is [-80, 0]
    m.plan_absolute("Z", -40.0, None)
    with pytest.raises(SoftLimitError):
        m.plan_absolute("Z", 10.0, None)


def test_feedrate_clamped_to_cap(device_map):
    m = MotionModel(device_map, feedrate_cap=1000)
    m.mark_homed(["X"])
    p = m.plan_relative("X", 1.0, feedrate=99999)
    # Axis max is 3000, global cap is 1000 → clamp to 1000
    assert p.feedrate == pytest.approx(1000.0)


def test_feedrate_must_be_positive(device_map):
    m = MotionModel(device_map, feedrate_cap=3000)
    m.mark_homed(["X"])
    with pytest.raises(SoftLimitError):
        m.plan_relative("X", 1.0, feedrate=0)
