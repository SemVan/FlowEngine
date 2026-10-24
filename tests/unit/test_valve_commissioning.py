from __future__ import annotations

import pytest

from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.homing import EndstopHoming
from flowengine.schemas import AxisConfig, DeviceMap, RuntimeParams, ValveConfig
from flowengine.transport import MockTransport


def valve_map() -> DeviceMap:
    return DeviceMap(
        instrument_id="valve-bench",
        axes=[
            AxisConfig(
                marlin_axis="A",
                name="wash_valve_axis",
                kind="valve",
                steps_per_unit=80,
                travel=10,
                home_direction="max",
                feedrate_default=30,
                feedrate_max=120,
            )
        ],
        valves=[ValveConfig(name="wash_valve", axis="A", position_a=0, position_b=-9)],
    )


async def stack():
    device = valve_map()
    transport = MockTransport(
        latency_s=0, probe_strokes={"A": 10}, probe_channel="valve_endstop"
    )
    await transport.open()
    queue = CommandQueue(transport)
    motion = MotionModel(device, 120)
    sender = GcodeSender(
        queue,
        motion,
        device,
        EndstopHoming(device, timeout_s=2),
        RuntimeParams().timeouts,
    )
    return sender, motion, transport


async def test_stepwise_home_finds_releases_and_references_valve():
    sender, motion, transport = await stack()
    result = await sender.valve_commissioning.home_stepwise(
        "wash_valve", "valve_endstop", 1, 0.25, 30, 6, 0.5, False
    )
    assert result["search_travel"] == 5
    assert result["position"] == pytest.approx(-0.75)
    assert result["endstop"] is False
    assert motion.homed["A"] is True
    assert motion.positions["A"] == pytest.approx(-0.75)
    assert transport._soft_endstops is True
    assert transport.driver_enabled.get("A") is not False


async def test_test_home_releases_motor_and_invalidates_reference():
    sender, motion, transport = await stack()
    result = await sender.valve_commissioning.home_stepwise(
        "wash_valve", "valve_endstop", 1, 0.25, 30, 6, 0.5, True
    )
    assert result["motor_released"] is True
    assert motion.homed["A"] is False
    assert transport.driver_enabled["A"] is False
    assert transport._soft_endstops is True


async def test_service_jog_is_bounded_and_always_releases_motor():
    sender, motion, transport = await stack()
    result = await sender.valve_commissioning.service_jog(
        "wash_valve", -0.1, 30, "valve_endstop"
    )
    assert result["position_trusted"] is False
    assert transport.mechanical["A"] == pytest.approx(4.9)
    assert transport.driver_enabled["A"] is False
    assert motion.homed["A"] is False
    with pytest.raises(ValueError, match=r"20 axis"):
        await sender.valve_commissioning.service_jog(
            "wash_valve", 20.1, 30, "valve_endstop"
        )


async def test_service_jog_can_hold_motor_after_successful_move():
    sender, motion, transport = await stack()
    result = await sender.valve_commissioning.service_jog_axis(
        "A", 1.0, 30, "valve_endstop", release_motor=False
    )
    assert result["motor_released"] is False
    assert "M18 A" not in transport.commands
    assert motion.homed["A"] is False


async def test_failed_search_restores_soft_endstops_and_releases_motor():
    sender, motion, transport = await stack()
    with pytest.raises(ValueError, match="not reached"):
        await sender.valve_commissioning.home_stepwise(
            "wash_valve", "valve_endstop", -1, 0.25, 30, 2, 0.5, False
        )
    assert transport._soft_endstops is True
    assert transport.driver_enabled["A"] is False
    assert motion.homed["A"] is False


async def test_stepwise_home_accepts_larger_bounded_search_steps():
    sender, motion, transport = await stack()
    result = await sender.valve_commissioning.home_stepwise(
        "wash_valve", "valve_endstop", 1, 1.0, 90, 6, 0.5, False
    )
    assert result["search_travel"] == 5
    assert result["endstop"] is False
    assert motion.homed["A"] is True
    assert transport._soft_endstops is True


async def test_stepwise_home_recovers_when_selected_valve_starts_on_switch():
    sender, motion, transport = await stack()
    transport.mechanical["A"] = 10
    result = await sender.valve_commissioning.home_stepwise(
        "wash_valve", "valve_endstop", 1, 0.5, 90, 6, 0.5, False
    )
    assert result["initial_release"] == -0.5
    assert result["endstop"] is False
    assert motion.homed["A"] is True


async def test_unknown_endstop_refuses_before_motion():
    sender, _motion, transport = await stack()
    with pytest.raises(ValueError, match="available"):
        await sender.valve_commissioning.service_jog("wash_valve", 0.1, 30, "wrong")
    assert not any(command.startswith("G1") for command in transport.commands)


async def test_release_distance_is_independent_of_search_and_placeholder_travel(monkeypatch):
    sender, motion, _transport = await stack()
    states = iter([False, False, True] + [True] * 22 + [False, False])

    async def endstop(_channel):
        return next(states)

    monkeypatch.setattr(sender.valve_commissioning, "endstop", endstop)
    result = await sender.valve_commissioning.home_stepwise(
        "wash_valve", "valve_endstop", 1, 0.5, 90, 2, 0.5, False,
        release_distance=12,
    )
    assert result["search_travel"] == 1
    assert result["position"] == -11.5
    assert result["endstop"] is False
    assert motion.homed["A"]


async def test_release_distance_limit_stops_and_releases_stuck_switch(monkeypatch):
    sender, motion, transport = await stack()
    states = iter([False, True])

    async def endstop(_channel):
        return next(states, True)

    monkeypatch.setattr(sender.valve_commissioning, "endstop", endstop)
    with pytest.raises(ValueError, match="configured release distance"):
        await sender.valve_commissioning.home_stepwise(
            "wash_valve", "valve_endstop", 1, 0.5, 90, 2, 0.5, False,
            release_distance=3,
        )
    assert not motion.homed["A"]
    assert transport.driver_enabled["A"] is False
    assert transport._soft_endstops is True


async def test_period_measures_two_rising_edges_and_releases_motor(monkeypatch):
    sender, motion, transport = await stack()
    axis = sender.valve_commissioning._map.axis("A").model_copy(update={"kind": "peristaltic_pump"})
    device = sender.valve_commissioning._map.model_copy(update={"axes": [axis]})
    sender.valve_commissioning._map = device
    states = iter([False, True, True, False, False, True])

    async def endstop(_channel):
        return next(states)

    monkeypatch.setattr(sender.valve_commissioning, "endstop", endstop)
    result = await sender.valve_commissioning.measure_period("A", "sensor", 1, 0.5, 90, 6)
    assert result["period_axis_units"] == 2
    assert result["commanded_pulses_per_period"] == 160
    assert not motion.homed["A"]
    assert transport.driver_enabled["A"] is False
    assert transport._soft_endstops is True
