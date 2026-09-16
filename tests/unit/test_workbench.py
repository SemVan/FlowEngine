from __future__ import annotations

import asyncio

import pytest

from flowengine.calibration import PumpMeasurement, calibrate_pump, reference_error
from flowengine.errors import ControllerError, TransportTimeout
from flowengine.events import EventBus
from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.homing import EndstopHoming
from flowengine.procedures.runner import ProcedureRunner
from flowengine.schemas import AxisConfig, DeviceMap, Procedure, RuntimeParams
from flowengine.schemas.device import EndstopInput, PumpConfig
from flowengine.schemas.procedure import PumpDose
from flowengine.schemas.runtime import FirmwareExpectation
from flowengine.state import State, StateMachine
from flowengine.transport import MockTransport
from flowengine.transport.parser import parse_line


def dm():
    return DeviceMap(
        instrument_id="bench",
        axes=[
            AxisConfig(
                marlin_axis=a,
                name=a,
                kind="syringe_pump",
                steps_per_unit=80,
                travel=30,
                home_direction="min",
                feedrate_default=60,
                feedrate_max=600,
                accel_max=100,
            )
            for a in ["X", "Y"]
        ],
        pumps=[
            PumpConfig(
                name="pump", axis="X", kind="syringe", volume_per_unit_ul=25, calibrated=True
            )
        ],
    )


async def stack(device=None, probe=False):
    device = device or dm()
    transport = MockTransport(latency_s=0, probe_strokes={"X": 20} if probe else None)
    await transport.open()
    queue = CommandQueue(transport)
    motion = MotionModel(device, 600)
    sender = GcodeSender(
        queue,
        motion,
        device,
        EndstopHoming(device, timeout_s=2),
        RuntimeParams().timeouts,
        FirmwareExpectation(
            probe_target_verified=probe,
            verified_probe_axes=["X"] if probe else [],
            probe_channel="probe" if probe else None,
        ),
    )
    return sender, motion, transport


def test_m114_counts_do_not_overwrite_position():
    response = parse_line("X:1.25 Y:2.50 A:3.00 Count X:100 Y:200 A:240")
    assert response.positions == {"X": 1.25, "Y": 2.5, "A": 3}


def test_pump_measurements_volume_and_mass():
    report = calibrate_pump(
        80,
        [
            PumpMeasurement(step_pulses=80, volume_ul=25),
            PumpMeasurement(step_pulses=160, mass_g=0.05, density_g_ml=1),
        ],
    )
    assert report["volume_per_unit_ul"] == 25
    assert report["relative_stddev"] == 0
    with pytest.raises(ValueError):
        PumpMeasurement(step_pulses=1, volume_ul=1, mass_g=1)
    with pytest.raises(ValueError):
        PumpMeasurement(step_pulses=float("nan"), volume_ul=1)
    assert reference_error(80, [81, 100], 5)["suspected_position_error"]


async def test_conversion_and_pump():
    sender, motion, transport = await stack()
    motion.mark_homed(["X"])
    assert sender.convert_move("X", 160, 80, "steps", "steps/s") == (2, 60)
    await sender.pump("pump", 50, 25)
    await sender.wait_idle()
    assert motion.positions["X"] == 2
    assert any(c == "G1 X2.0000 F1.00" for c in transport.commands)
    with pytest.raises(ValueError, match="exceeds"):
        await sender.pump("pump", 1, 999999)


async def test_uncalibrated_pump_refuses_motion():
    device = dm().model_copy(
        update={"pumps": [dm().pumps[0].model_copy(update={"calibrated": False})]}
    )
    sender, motion, transport = await stack(device)
    motion.mark_homed(["X"])
    with pytest.raises(ValueError, match="calibrated"):
        await sender.pump("pump", 1, 1)
    assert transport.commands == []


async def test_coordinated_single_g1_and_limits():
    sender, motion, transport = await stack()
    motion.mark_homed(["X", "Y"])
    await sender.move_multi({"X": 1, "Y": 2}, relative=True, duration_s=10)
    await sender.wait_idle()
    assert motion.positions["X"] == 1 and motion.positions["Y"] == 2
    assert len([c for c in transport.commands if c.startswith("G1 ")]) == 1
    assert "X1.0000 Y2.0000" in transport.commands[1]
    before = len(transport.commands)
    with pytest.raises(ValueError, match="limit"):
        await sender.move_multi({"X": 20, "Y": 20}, duration_s=0.001)
    assert len(transport.commands) == before


async def test_shared_input_blocks_g28_and_joint_move():
    device = dm().model_copy(
        update={"endstop_inputs": [EndstopInput(channel="probe", axes=["X", "Y"])]}
    )
    sender, motion, transport = await stack(device)
    motion.mark_homed(["X", "Y"])
    with pytest.raises(ValueError, match="shared-input"):
        await sender.home(["X"])
    with pytest.raises(ValueError, match="share input"):
        await sender.move_multi({"X": 1, "Y": 1})
    assert not transport.commands


async def test_release_invalidates_only_selected_axis():
    sender, motion, transport = await stack()
    motion.mark_homed(["X", "Y"])
    await sender.motors(False, ["X"])
    assert motion.homed == {"X": False, "Y": True}
    assert transport.driver_enabled["X"] is False


async def test_acceleration_restored_on_success_and_error():
    sender, motion, transport = await stack()
    motion.mark_homed(["X"])
    async with sender.acceleration(500, ["X"]):
        assert transport._acceleration["P"] == 100
    assert transport._acceleration["P"] == 3000
    with pytest.raises(ValueError):
        async with sender.acceleration(50, ["X"]):
            raise ValueError("failure")
    assert transport._acceleration["P"] == 3000


async def test_probe_missing_or_stuck_refuses():
    sender, _motion, _transport = await stack()
    with pytest.raises(ValueError, match="not verified"):
        await sender.seek("X", "probe", -10, 30, True)
    sender, _motion, transport = await stack(probe=True)
    transport.probe_stuck = True
    with pytest.raises(ValueError, match="already triggered"):
        await sender.seek("X", "probe", -20, 30, True)
    assert not any(c.startswith("G38") for c in transport.commands)


async def test_valve_span_uses_step_counter():
    sender, motion, _transport = await stack(probe=True)
    result = await sender.calibrate_valve("X", "probe", 30, 30, 1, 3)
    assert result["spans_step_pulses"] == [1600] * 3
    assert result["suggested_travel_units"] == 20
    assert result["applied"] is False
    assert motion.homed["X"]
    assert not (await sender.read_endstops())["probe"]


async def test_reference_error_detects_directional_loss_not_counters_alone():
    sender, _motion, transport = await stack(probe=True)
    report = await sender.test_reference("X", "probe", 5, 60, 30, 1, 5, 2)
    assert not report["suspected_position_error"]
    transport.mechanical["X"] = 10
    transport.step_loss_fraction = 0.05
    transport.step_loss_direction = 1
    report = await sender.test_reference("X", "probe", 5, 60, 30, 1, 5, 2)
    assert report["suspected_position_error"]


async def test_selected_step_abort_is_tracked_and_blocks_position():
    sender, motion, _transport = await stack()
    state = StateMachine()
    await state.transition(State.CONNECTED_IDLE)
    runner = ProcedureRunner(sender, dm(), state, EventBus())
    procedure = Procedure(name="wait", draft=True, steps=[{"op": "dwell", "seconds": 10}])
    task = asyncio.create_task(runner.run_one("wait", 1, procedure.steps[0]))
    await asyncio.sleep(0.01)
    assert runner.status["running"]
    await runner.abort()
    with pytest.raises(Exception, match="aborted"):
        await task
    assert state.state == State.ERRORED
    assert not any(motion.homed.values())


async def test_pump_multi_ratios_and_incompatible_durations():
    device = dm().model_copy(
        update={
            "pumps": [
                dm().pumps[0],
                PumpConfig(
                    name="sheath", axis="Y", kind="syringe", calibrated=True, volume_per_unit_ul=50
                ),
            ]
        }
    )
    sender, motion, transport = await stack(device)
    motion.mark_homed(["X", "Y"])
    await sender.pump_multi(
        {
            "pump": PumpDose(volume_ul=25, flow_ul_min=25),
            "sheath": PumpDose(volume_ul=100, flow_ul_min=100),
        }
    )
    await sender.wait_idle()
    assert motion.positions == {"X": 1, "Y": 2}
    assert len([c for c in transport.commands if c.startswith("G1 ")]) == 1
    before = len(transport.commands)
    with pytest.raises(ValueError, match="different finish times"):
        await sender.pump_multi(
            {
                "pump": PumpDose(volume_ul=25, flow_ul_min=25),
                "sheath": PumpDose(volume_ul=100, flow_ul_min=200),
            }
        )
    assert len(transport.commands) == before


async def test_faulted_queue_does_not_consume_late_ack():
    transport = MockTransport(latency_s=0)
    await transport.open()
    queue = CommandQueue(transport, default_timeout_s=0.01)
    transport.inject_error()
    with pytest.raises(TransportTimeout):
        await queue.send("G90")
    count = len(transport.commands)
    with pytest.raises(ControllerError, match="aborted"):
        await queue.send("G1 X1")
    assert len(transport.commands) == count
