"""Pytest fixtures shared across unit and integration tests."""

from __future__ import annotations

import pytest


def use_firmware_home_axes(ctx):
    from flowengine.api.router_axis_calibration import _apply_runtime_device_map
    from flowengine.schemas import DeviceMap

    data = ctx.device_map.model_dump(mode="json")
    for axis in data["axes"]:
        if axis["marlin_axis"] in {"X", "Y"}:
            axis.update(endstop_channels_shared=False, axis_calibration=None, travel=200)
    _apply_runtime_device_map(ctx, DeviceMap.model_validate(data))

from flowengine.schemas import (
    AxisConfig,
    DeviceKind,
    DeviceMap,
    PumpConfig,
    ValveConfig,
)


@pytest.fixture
def device_map() -> DeviceMap:
    """Minimal but realistic device map used by most tests."""
    axes = [
        AxisConfig(
            marlin_axis="X",
            name="autosampler_x",
            kind=DeviceKind.AUTOSAMPLER_AXIS,
            steps_per_unit=80.0,
            units="mm",
            travel=200.0,
            home_direction="min",
            feedrate_default=600.0,
            feedrate_max=3000.0,
        ),
        AxisConfig(
            marlin_axis="E0",
            name="sample_syringe",
            kind=DeviceKind.SYRINGE_PUMP,
            steps_per_unit=400.0,
            units="mm",
            travel=60.0,
            home_direction="min",
            feedrate_default=200.0,
            feedrate_max=800.0,
        ),
        AxisConfig(
            marlin_axis="E3",
            name="sample_valve_axis",
            kind=DeviceKind.VALVE,
            steps_per_unit=17.78,
            units="deg",
            travel=90.0,
            home_direction="min",
            feedrate_default=300.0,
            feedrate_max=900.0,
        ),
    ]
    return DeviceMap(
        instrument_id="test",
        axes=axes,
        valves=[ValveConfig(name="sample_valve", axis="E3", position_a=0.0, position_b=90.0)],
        pumps=[PumpConfig(name="sample_pump", axis="E0", kind="syringe", volume_per_unit_ul=25.0)],
    )
