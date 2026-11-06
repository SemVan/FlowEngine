import pytest

from flowengine.hardware.coordinated import timed_pump_moves
from flowengine.procedures.preflight import execution_issues
from flowengine.schemas import Procedure, PumpConfig
from flowengine.schemas.procedure import AxisFeed, PumpDose
from tests.test_independent_pumps import bench


def rates():
    return {"X": AxisFeed(direction=1, feedrate=120), "Y": AxisFeed(direction=-1, feedrate=60)}


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_common_time_keeps_both_pumps_in_one_motion_entire_stage(streaming):
    sender, transport, _ = await bench(streaming)
    initial = sender._motion.positions
    start = len(transport.commands)
    result = await sender.move_multi_for(rates(), 2)
    commands = [c for c in transport.commands[start:] if c.startswith("G1 ")]
    assert len(commands) == 1
    assert "X" in commands[0] and "Y" in commands[0]
    assert result["common_interval"]
    assert all(a["nominal_duration_s"] == 2 for a in result["axes"].values())
    assert sender._motion.positions["X"] == pytest.approx(initial["X"] + 4)
    assert sender._motion.positions["Y"] == pytest.approx(initial["Y"] - 2)
    assert all(sender._motion.homed.values())


def test_common_time_converts_mm_per_second_and_steps_per_second():
    from tests.test_syringe_group_home import pair_map
    distances, speeds = timed_pump_moves(pair_map(), {
        "X": AxisFeed(direction=1, feedrate=2, speed_units="axis/s"),
        "Y": AxisFeed(direction=-1, feedrate=80, speed_units="steps/s"),
    }, 2, 1200)
    assert distances == {"X": 4, "Y": -2}
    assert speeds == {"X": 120, "Y": 60}


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["time", "speed", "substep"])
async def test_invalid_flow_time_step_is_rejected_before_commands(fault):
    sender, transport, _ = await bench()
    request, seconds = rates(), 2
    if fault == "time":
        seconds = 0
    elif fault == "speed":
        request["X"] = AxisFeed(feedrate=9999)
    else:
        seconds = .0001
    start = len(transport.commands)
    with pytest.raises(ValueError):
        await sender.move_multi_for(request, seconds)
    assert transport.commands[start:] == []


def calibrated_pumps(sender):
    device = sender._map.model_copy(update={"pumps": [
        PumpConfig(name="sample", axis="X", kind="syringe", volume_per_unit_ul=10,
                   calibrated=True, dispense_direction=1),
        PumpConfig(name="sheath", axis="Y", kind="syringe", volume_per_unit_ul=40,
                   calibrated=True, dispense_direction=-1),
    ]})
    sender._map = sender._motion._map = device


@pytest.mark.asyncio
@pytest.mark.parametrize("second_volume,expected_segments,second_duration", [(80, 1, 2), (160, 2, 4)])
async def test_different_volumes_and_flows_use_one_or_more_segments_as_needed(second_volume, expected_segments, second_duration):
    sender, transport, _ = await bench()
    calibrated_pumps(sender)
    initial = sender._motion.positions
    start = len(transport.commands)
    result = await sender.pump_multi({
        "sample": PumpDose(volume_ul=40, flow_ul_min=1200),
        "sheath": PumpDose(volume_ul=second_volume, flow_ul_min=2400),
    })
    lines = [c for c in transport.commands[start:] if c.startswith("G1 ")]
    assert len(lines) == expected_segments
    assert "X" in lines[0] and "Y" in lines[0]
    assert result["pumps"]["sample"]["nominal_duration_s"] == 2
    assert result["pumps"]["sheath"]["nominal_duration_s"] == second_duration
    assert result["axes"]["X"]["feedrate"] == 120
    assert result["axes"]["Y"]["feedrate"] == 60
    assert sender._motion.positions["X"] == pytest.approx(initial["X"] + 4)
    assert sender._motion.positions["Y"] == pytest.approx(initial["Y"] - second_volume / 40)
    assert all(sender._motion.homed.values())


@pytest.mark.asyncio
async def test_unmeasured_coefficients_and_excessive_flow_are_reported_before_home():
    sender, _, runtime = await bench()
    calibrated_pumps(sender)
    procedure = Procedure.model_validate({"name": "flow", "steps": [
        {"op": "home", "axes": ["X", "Y"]},
        {"op": "pump_multi", "pumps": {
            "sample": {"volume_ul": 40, "flow_ul_min": 1200},
            "sheath": {"volume_ul": 80, "flow_ul_min": 999999},
        }},
    ]})
    assert any("предела" in issue for issue in execution_issues(procedure, sender._map, runtime))
    device = sender._map.model_copy(update={"pumps": [
        p.model_copy(update={"calibrated": False}) for p in sender._map.pumps]})
    assert any("uncalibrated" in issue for issue in execution_issues(procedure, device, runtime))


def test_time_parameters_resolve_in_nested_pump_settings():
    procedure = Procedure.model_validate({"name": "two_flows", "parameters": {
        "speed": {"type": "number", "default": 120}, "time": {"type": "number", "default": 2}},
        "steps": [{"op": "move_multi_for", "seconds": "${time}", "axes": {
            "X": {"feedrate": "${speed}"}, "Y": {"direction": -1, "feedrate": 60}}}]})
    step = procedure.resolve({"speed": 180, "time": 3}).steps[0]
    assert step.seconds == 3
    assert step.axes["X"].feedrate == 180
