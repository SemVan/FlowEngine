import asyncio
import re
from types import SimpleNamespace

import pytest

from flowengine.api.deps import AppContext
from flowengine.api.router_axis_calibration import _auto_job_runner
from flowengine.config_store import save_yaml, find_saved, load_yaml
from flowengine.errors import StateError
from flowengine.events import EventBus
from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.homing import EndstopHoming
from flowengine.hardware.group_seek import seek_group
from flowengine.schemas import AxisConfig, ConfigurationProfile, DeviceMap, RuntimeParams
from flowengine.schemas.device import AxisCalibration
from flowengine.state import State, StateMachine
from flowengine.transport import MockTransport


def pair_map():
    axes = []
    for axis, motor, far, direction in [("X", 0, 20, -1), ("Y", 60, 40, 1)]:
        cal = AxisCalibration(channel=f"{axis.lower()}_min",
            motor={"position": motor, "approach_direction": direction, "step_count": motor * 80},
            far={"position": far, "approach_direction": -direction, "step_count": far * 80},
            positive_toward_motor=direction == 1, travel=20, travel_steps=1600,
            seek_feedrate=1200, latch_feedrate=120, backoff_mm=1,
            recorded_at="2026-10-29", measurement_source="marlin_count")
        axes.append(AxisConfig(marlin_axis=axis, name=axis, kind="syringe_pump",
            steps_per_unit=80, travel=None, home_direction="min" if direction == -1 else "max",
            feedrate_default=1200, feedrate_max=1200, axis_calibration=cal))
    return DeviceMap(instrument_id="two-syringes", axes=axes)


class PairController(MockTransport):
    def __init__(self):
        super().__init__(latency_s=0)
        self._positions.update(X=2, Y=50)
        self._counts.update(X=160, Y=4000)
        self.physical = {"X": 2.0, "Y": 50.0}
        self.limits = {"X": (0, 20), "Y": (40, 60)}

    async def _handle(self, payload):
        p = payload.upper()
        if p.startswith("G1 "):
            for axis, value in re.findall(r"\b([XY])(-?\d+(?:\.\d+)?)", p):
                target = float(value) if self._absolute else self._positions[axis] + float(value)
                delta = target - self._positions[axis]
                self.physical[axis] += delta
                self._counts[axis] += round(delta * self._steps_per_unit[axis])
                self._positions[axis] = target
                self.driver_enabled[axis] = True
            await self._inbox.put("ok")
            return
        if p == "M119":
            for axis, position in self.physical.items():
                low, high = self.limits[axis]
                await self._inbox.put(f"{axis.lower()}_min: {'TRIGGERED' if position <= low or position >= high else 'open'}")
            await self._inbox.put("ok")
            return
        await super()._handle(payload)


class StreamingPairController(PairController):
    name = "marlin"

    def __init__(self):
        super().__init__()
        self.targets = {}
        self.realtime_counts = True

    async def _handle(self, payload):
        p = payload.upper()
        if p == "M114 R" and not self.realtime_counts:
            await self._inbox.put("X:0 Y:0")
            await self._inbox.put("ok")
            return
        if p.startswith("G1 "):
            for a, v in re.findall(r"\b([XY])(-?\d+(?:\.\d+)?)", p):
                delta = float(v) if not self._absolute else float(v) - self._positions[a]
                self.targets[a] = self.physical[a] + delta
                self._positions[a] += delta
            await self._inbox.put("ok")
            return
        if p == "M119":
            for a, target in self.targets.items():
                delta = max(-.25, min(.25, target - self.physical[a]))
                self.physical[a] += delta
                self._counts[a] += round(delta * self._steps_per_unit[a])
        if p == "M410":
            self.targets.clear()
        if p == "M400":
            assert not self.targets, "нельзя блокировать опрос M119 ожиданием длинного хода"
        await super()._handle(payload)


async def stack():
    device, runtime = pair_map(), RuntimeParams()
    transport = PairController()
    await transport.open()
    motion = MotionModel(device, 1200)
    sender = GcodeSender(CommandQueue(transport), motion, device, EndstopHoming(device, 2), runtime.timeouts)
    return sender, transport, runtime


@pytest.mark.asyncio
async def test_pair_home_has_joint_moves_opposite_directions_and_both_switches_open():
    sender, transport, _ = await stack()
    result = await sender.home(["X", "Y"])
    assert all(sender._motion.homed.values())
    assert all(r["simultaneous"] for r in result.values())
    assert not any((await sender.read_endstops()).values())
    assert sender._motion.positions["X"] == pytest.approx(1, abs=.1)
    assert sender._motion.positions["Y"] == pytest.approx(19, abs=.1)
    assert 0 < transport.physical["X"] < 20
    assert 40 < transport.physical["Y"] < 60
    moves = [c for c in transport.commands if c.startswith("G1 ")]
    assert re.search(r"X-.*Y[\d]", moves[0])
    assert any("X" not in c and "Y" in c for c in moves)
    assert not any(c.startswith("G28") for c in transport.commands)


@pytest.mark.asyncio
async def test_pair_cancel_invalidates_both_without_moving_valves():
    sender, transport, _ = await stack()

    async def cancel(_detail):
        sender._home_cancel.set()

    with pytest.raises(StateError, match="остановлен"):
        await sender.home(["X", "Y"], progress=cancel)
    assert not any(sender._motion.homed.values())
    assert transport.soft_endstops
    assert not any(re.search(r"G1.*\b[ABC]", c) for c in transport.commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("realtime_counts", [True, False])
async def test_streaming_seek_stops_at_each_separate_switch_and_resyncs_actual_counts(realtime_counts):
    device = pair_map()
    transport = StreamingPairController()
    transport.realtime_counts = realtime_counts
    await transport.open()
    runtime = RuntimeParams()
    sender = GcodeSender(CommandQueue(transport), MotionModel(device, 1200), device,
                         EndstopHoming(device, 2), runtime.timeouts)
    result = await seek_group(sender, ["X", "Y"], {"X": -1, "Y": 1},
                              {"X": 1200, "Y": 1200}, True, {"X": 4, "Y": 4},
                              cancelled=lambda: False)
    assert result["X"]["step_count"] == 0
    assert result["Y"]["step_count"] == 4800
    assert transport.physical == {"X": 0, "Y": 60}
    assert transport._positions["X"] == 0
    assert transport._positions["Y"] == 60
    moves = [c for c in transport.commands if c.startswith("G1 ")]
    assert len(moves) == 2
    assert "X" in moves[0] and "Y" in moves[0]
    assert "X" not in moves[1] and "Y" in moves[1]


@pytest.mark.asyncio
async def test_full_pair_calibration_measures_both_spans_then_leaves_both_referenced(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    sender, transport, runtime = await stack()
    device = sender._map
    save_yaml("profiles", "pair", ConfigurationProfile(name="pair", device_map=device, runtime=runtime))
    state = StateMachine()
    await state.transition(State.CONNECTED_IDLE)
    await state.transition(State.MOVING)
    ctx = AppContext(device, runtime, None, transport, sender._q, sender._motion, sender,
                     state, EventBus(), SimpleNamespace(status={"running": False}, _map=device), profile_name="pair")
    job = {"axes": ["X", "Y"], "cancel": asyncio.Event(), "results": {}}
    await _auto_job_runner(ctx, job)
    assert job["state"] == "completed", job
    for axis in "XY":
        assert job["results"][axis]["travel"] == pytest.approx(20, abs=.15)
        assert job["results"][axis]["simultaneous"]
        assert sender._motion.homed[axis]
    assert not any((await sender.read_endstops()).values())
    profile = load_yaml(find_saved("profiles", "pair"), ConfigurationProfile)
    assert profile.device_map.axis("X").axis_calibration.positive_toward_motor is False
    assert profile.device_map.axis("Y").axis_calibration.positive_toward_motor is True
    assert sum(bool(re.search(r"G1.*\bX.*\bY", c)) for c in transport.commands) > 5
