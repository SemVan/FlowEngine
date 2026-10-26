"""проверяем привязку, числа и ограничение скорости"""

from __future__ import annotations

import pytest

from flowengine.errors import MotionValidationError
from flowengine.hardware.motion import MotionModel


def test_refuses_motion_before_home(device_map):
    model = MotionModel(device_map, feedrate_cap=3000)
    with pytest.raises(MotionValidationError, match="not referenced"):
        model.plan_relative("X", 1.0, None)


def test_uses_current_position_and_default_speed(device_map):
    model = MotionModel(device_map, feedrate_cap=3000)
    model.mark_referenced("X", 5.0)
    plan = model.plan_relative("X", 10.0, None)
    assert plan.target == pytest.approx(15.0)
    assert plan.feedrate == pytest.approx(600.0)


def test_invalidation_requires_new_reference(device_map):
    model = MotionModel(device_map, feedrate_cap=3000)
    model.mark_homed(["X"])
    model.invalidate(["X"])
    with pytest.raises(MotionValidationError, match="not referenced"):
        model.plan_absolute("X", 1.0, None)


@pytest.mark.parametrize("target", [-1.0, 9999.0])
def test_positions_are_not_checked_against_legacy_travel(device_map, target):
    model = MotionModel(device_map, feedrate_cap=3000)
    model.mark_homed(["X"])
    assert model.plan_absolute("X", target, None).target == target


def test_feedrate_clamped_to_cap(device_map):
    model = MotionModel(device_map, feedrate_cap=1000)
    model.mark_homed(["X"])
    assert model.plan_relative("X", 1.0, 99999).feedrate == pytest.approx(1000.0)


@pytest.mark.parametrize("speed", [0, -1, float("inf"), float("nan")])
def test_invalid_feedrate_is_rejected(device_map, speed):
    model = MotionModel(device_map, feedrate_cap=3000)
    model.mark_homed(["X"])
    with pytest.raises(MotionValidationError):
        model.plan_relative("X", 1.0, speed)


@pytest.mark.parametrize("target", [float("inf"), float("nan")])
def test_nonfinite_position_is_rejected(device_map, target):
    model = MotionModel(device_map, feedrate_cap=3000)
    model.mark_homed(["X"])
    with pytest.raises(MotionValidationError):
        model.plan_absolute("X", target, None)
