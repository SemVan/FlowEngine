from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings
from flowengine.errors import ControllerError, StateError, TransportClosed, TransportTimeout
from flowengine.hardware import CommandQueue, GcodeSender, MotionModel
from flowengine.hardware.homing import EndstopHoming
from flowengine.schemas import AxisConfig, DeviceMap, RuntimeParams, ValveConfig
from flowengine.schemas.device import SharedValveEndstop
from flowengine.transport import MockTransport


def valve_map():
    return DeviceMap(instrument_id="single-switch-valves", axes=[
        AxisConfig(marlin_axis=axis, name=name, kind="valve", steps_per_unit=80,
                   travel=20, home_direction="max", feedrate_default=120, feedrate_max=300)
        for axis, name in zip("ABC", ("wash", "sheath", "sample"))],
        valves=[ValveConfig(name=name + "_valve", axis=axis, position_a=-1, position_b=-2,
                            direction_toward_home=1, calibrated=True, home_timeout_s=1)
                for axis, name in zip("ABC", ("wash", "sheath", "sample"))],
        shared_valve_endstop=SharedValveEndstop(channel="z_min"))


async def stack(transport=None):
    device, runtime = valve_map(), RuntimeParams()
    transport = transport or MockTransport(latency_s=0, valve_homes={a: 1 for a in "ABC"})
    await transport.open()
    queue = CommandQueue(transport)
    motion = MotionModel(device, 300)
    sender = GcodeSender(queue, motion, device, EndstopHoming(device, 1), runtime.timeouts)
    return sender, transport


async def test_home_from_open_stops_latches_and_pulls_off():
    sender, transport = await stack()
    result = await sender.home_valve("sample")
    assert result["shared_endstop"] == "OPEN"
    assert sender._motion.homed["C"]
    assert not (await sender.read_endstops())["z_min"]
    assert transport.valve_mechanical["C"] < 0
    assert sender._motion.positions["C"] < 0
    assert sum(c == "M410" for c in transport.commands) == 4
    assert not any(c.startswith("G28") for c in transport.commands)
    assert all(not re.search(r"[AB][-\d]", c) for c in transport.commands if c.startswith("G1 "))
    assert transport.soft_endstops


async def test_two_valves_cannot_move_at_once_and_home_all_is_exclusive():
    sender, transport = await stack()
    transport._latency = .001
    home = asyncio.create_task(sender.valves.home_all())
    while not sender.valves.lock.locked():
        await asyncio.sleep(0)
    with pytest.raises(StateError, match="active"):
        await sender.home_valve("sample")
    results = await home
    assert [r["name"] for r in results] == ["wash_valve", "sheath_valve", "sample_valve"]
    moved = [re.search(r"G1 ([ABC])", c).group(1) for c in transport.commands if re.search(r"G1 ([ABC])", c)]
    assert list(dict.fromkeys(moved)) == list("ABC")
    assert moved == sorted(moved)
    assert all(sender._motion.homed.values())
    assert not (await sender.read_endstops())["z_min"]


async def test_startup_pressed_is_unknown_and_no_home_motor_starts():
    sender, transport = await stack()
    transport.valve_mechanical["B"] = 0
    assert await sender.valves.read()
    assert sender.valves.snapshot()["shared_valve_endstop"]["source"] == "UNKNOWN"
    for home in (lambda: sender.valves.home_all(), lambda: sender.home_valve("sample"), lambda: sender.home(["A", "B"])):
        with pytest.raises(StateError, match="UNKNOWN"):
            await home()
    assert not any(c.startswith(("G1", "G28")) for c in transport.commands)
    await sender.valves.jog("sheath", -.1, service=True)
    assert not await sender.valves.read()
    assert not any(sender._motion.homed.values())
    assert not any(c.startswith(("G1 A", "G1 C")) for c in transport.commands)
    await sender.home_valve("sample")
    assert sender._motion.homed["C"]


async def test_go_requires_reference_and_keeps_calibration():
    sender, transport = await stack()
    with pytest.raises(StateError, match="Home required"):
        await sender.set_valve("sample", "A")
    assert not any(c.startswith("G1") for c in transport.commands)
    await sender.home_valve("sample")
    await sender.set_valve("sample", "A")
    await sender.set_valve("sample", "B")
    assert sender.valves.snapshot()["valves"][2]["current"] == "B"
    sender.invalidate_positions()
    value = sender.valves.snapshot()["valves"][2]
    assert value["state"] == "CALIBRATED_UNREFERENCED"
    assert value["current"] == "UNKNOWN"
    assert value["config"]["position_a"] == -1
    assert value["config"]["position_b"] == -2


@pytest.mark.parametrize("speed, expected", [(None, 120), (90, 90), (900, 300)])
async def test_valve_step_speed_uses_default_or_existing_cap(speed, expected):
    sender, transport = await stack()
    await sender.home_valve("sample")
    transport.commands.clear()
    await sender.set_valve("sample", "B", speed)
    moves = [c for c in transport.commands if c.startswith("G1 ")]
    assert moves == [f"G1 C-2.000000 F{expected:.3f}"]
    assert sender.valves.states["sample_valve"].current == "B"
    assert not any(c.startswith(("M906", "M17")) for c in transport.commands)


async def test_valve_procedure_parameters_apply_and_restore_acceleration():
    from flowengine.events import EventBus
    from flowengine.procedures.runner import ProcedureRunner
    from flowengine.schemas import Procedure
    from flowengine.state import State, StateMachine

    sender, transport = await stack()
    await sender.home_valve("sample")
    transport.commands.clear()
    state = StateMachine()
    await state.transition(State.CONNECTED_IDLE)
    runner = ProcedureRunner(sender, sender._map, state, EventBus())
    proc = Procedure.model_validate({
        "name": "valve_speed",
        "parameters": {"speed": {"type": "number", "default": 80}},
        "steps": [{"op": "set_valve", "name": "sample_valve", "position": "B",
                   "feedrate": "${speed}", "acceleration": 50}],
    }).resolve()
    await runner.run(proc)
    assert runner.status["error"] is None
    commands = transport.commands
    accel = commands.index("M204 P50.000 T50.000")
    move = commands.index("G1 C-2.000000 F80.000")
    restore = next(i for i, c in enumerate(commands) if c.startswith("M204 P3000"))
    assert accel < move < restore
    assert transport._acceleration["P"] == 3000


@pytest.mark.parametrize("speed", [0, -1, float("nan"), float("inf")])
async def test_invalid_valve_speed_is_refused_before_commands(speed):
    sender, transport = await stack()
    with pytest.raises(ValueError, match="feedrate"):
        await sender.set_valve("sample", "B", speed)
    assert not transport.commands


async def test_unexpected_endstop_stops_and_faults_without_next_move():
    sender, transport = await stack()
    await sender.home_valve("sample")
    original = transport._handle

    async def hit_on_move(command):
        if command.startswith("G1 C"):
            transport.valve_endstop_stuck = True
        await original(command)

    transport._handle = hit_on_move
    start = len(transport.commands)
    with pytest.raises(ControllerError, match="Unexpected"):
        await sender.set_valve("sample", "B")
    commands = transport.commands[start:]
    move_index = next(i for i, c in enumerate(commands) if c.startswith("G1"))
    stop_index = next(i for i, c in enumerate(commands) if c == "M410")
    assert stop_index > move_index
    assert not any(c.startswith("G1") for c in commands[stop_index + 1:])
    assert sender.valves.states["sample_valve"].state == "FAULT"
    assert not sender._motion.homed["C"]
    assert sender.valves.snapshot()["valves"][2]["current"] == "UNKNOWN"


async def test_legacy_and_raw_paths_cannot_bypass_valve_supervision():
    sender, transport = await stack()
    for command in ("G28", "G28 C", "G028 C", "G1 C10", "G01 C-2", "G1 X1C2", "G38.2 C1"):
        with pytest.raises(StateError):
            await sender.raw(command, confirmed=True)
    with pytest.raises(StateError):
        await sender.move_multi({"A": -1, "B": -1})
    with pytest.raises(StateError):
        await sender.calibrate_valve("C", "z_min", 20, 30, 1)
    with pytest.raises(StateError):
        await sender.jog("C", -.1)
    with pytest.raises(ValueError, match="Valve G28"):
        await sender._homing.home(sender._q, ["A", "C"])
    assert not any(c.startswith(("G1", "G28", "G38")) for c in transport.commands)


async def test_procedure_fault_does_not_run_the_next_valve():
    from flowengine.events import EventBus
    from flowengine.procedures.runner import ProcedureRunner
    from flowengine.schemas import Procedure
    from flowengine.state import State, StateMachine
    sender, transport = await stack()
    state = StateMachine()
    await state.transition(State.CONNECTED_IDLE)
    runner = ProcedureRunner(sender, sender._map, state, EventBus())
    original = transport._handle

    async def unexpected(command):
        if sender.valves.phase == "idle" and command.startswith("G1 C"):
            transport.valve_endstop_stuck = True
        await original(command)

    transport._handle = unexpected
    procedure = Procedure(name="test_valve_procedure", steps=[
        {"op": "home_valve", "name": "sample_valve"},
        {"op": "set_valve", "name": "sample_valve", "position": "B"},
        {"op": "home_valve", "name": "wash_valve"}])
    await runner.run(procedure)
    assert runner.status["error"]
    assert runner.status["step"] == 2
    assert state.state == State.ERRORED
    assert not any(c.startswith("G1 A") for c in transport.commands)
    assert sender.valves.states["sample_valve"].state == "FAULT"


async def test_stop_during_home_does_not_resume_or_reference():
    sender, transport = await stack()
    transport._latency = .001
    transport.valve_endstop_stuck = False
    home = asyncio.create_task(sender.home_valve("sample"))
    while not any(c.startswith("G1 C") for c in transport.commands):
        await asyncio.sleep(0)
    await sender.abort()
    stopped_at = len(transport.commands)
    with pytest.raises(ControllerError):
        await home
    assert not any(c.startswith("G1") for c in transport.commands[stopped_at:])
    assert not any(sender._motion.homed.values())
    assert sender.valves.snapshot()["valves"][2]["current"] == "UNKNOWN"


async def test_service_does_not_invent_source_or_search_other_motors():
    sender, transport = await stack()
    transport.valve_mechanical["B"] = 0
    await sender.valves.jog("sample", -.1, service=True)
    assert await sender.valves.read()
    assert sender.valves.source == "UNKNOWN"
    assert not any(c.startswith(("G1 A", "G1 B")) for c in transport.commands)


async def test_home_watchdog_faults_without_reference():
    sender, transport = await stack()
    transport.valve_endstop_stuck = False
    cfg = sender._map.valves[2].model_copy(update={"home_timeout_s": .01})
    sender._map = sender._map.model_copy(update={"valves": [*sender._map.valves[:2], cfg]})
    with pytest.raises(ControllerError, match="watchdog"):
        await sender.home_valve("sample")
    assert sender.valves.states["sample_valve"].state == "FAULT"
    assert not sender._motion.homed["C"]
    assert "M410" in transport.commands


class StreamingValveMarlin(MockTransport):
    """команда G1 сразу отвечает ok; движение идёт во время опроса"""
    name = "marlin"

    def __init__(self):
        super().__init__(latency_s=0, valve_homes={a: 1 for a in "ABC"})
        self.target = None
        self.triggered_while_moving = []
        self.stopped_contacts = []
        self.inject_on_next_move = False
        self.trigger_position = None

    async def _handle(self, command):
        if command.startswith("G1 "):
            assert self.target is None, "a second motion was queued before the first stopped"
            axis, value = re.search(r"G1 ([ABC])([-\d.]+)", command).groups()
            self.driver_enabled[axis] = True
            target = float(value) if self._absolute else self._positions[axis] + float(value)
            self.target = (axis, target)
            if self.inject_on_next_move:
                self.trigger_position = (axis, self.valve_mechanical[axis] - .25)
                self.inject_on_next_move = False
            await self._inbox.put("ok")
            return
        if command.startswith("M119") and self.target:
            axis, target = self.target
            delta = max(-.25, min(.25, target - self._positions[axis]))
            self._positions[axis] += delta
            self._counts[axis] += round(delta * self._steps_per_unit[axis])
            self.valve_mechanical[axis] += delta
            if self.trigger_position and self.valve_mechanical[axis] <= self.trigger_position[1]:
                self.valve_endstop_stuck = True
            if self.valve_mechanical[axis] >= 0 or self.valve_endstop_stuck:
                self.triggered_while_moving.append((len(self.commands) - 1, delta, self.valve_mechanical[axis]))
            if abs(target - self._positions[axis]) < .00001:
                self.target = None
        if command == "M410":
            if self.target:
                self.stopped_contacts.append((self.target[0], self.valve_mechanical[self.target[0]]))
            self.target = None
        if command == "M400":
            assert self.target is None, "M400 blocked endstop polling while motion was in flight"
        await super()._handle(command)


async def test_streaming_marlin_trigger_is_followed_immediately_by_stop_and_release():
    transport = StreamingValveMarlin()
    sender, _ = await stack(transport)
    sender._map = sender._map.model_copy(update={"valves": [
        v.model_copy(update={"home_timeout_s": 5}) for v in sender._map.valves]})
    await sender.home_valve("sample")
    contacts = [(i, distance) for i, delta, distance in transport.triggered_while_moving if delta > 0]
    assert len(contacts) == 2
    for index, _ in contacts:
        assert transport.commands[index + 1] == "M410"
    assert not await sender.valves.read()
    assert transport.valve_mechanical["C"] < 0
    assert sender._motion.positions["C"] < 0
    assert not any(c.startswith("G28") for c in transport.commands)
    assert transport.target is None
    assert sender._motion.positions["C"] == pytest.approx(
        (sender._counts["C"] - sender.reference_counts["C"]) / 80)
    await sender.set_valve("sample", "A")
    transport.triggered_while_moving.clear()
    transport.inject_on_next_move = True
    with pytest.raises(ControllerError, match="Unexpected"):
        await sender.set_valve("sample", "B")
    index = transport.triggered_while_moving[0][0]
    assert transport.commands[index + 1] == "M410"
    assert transport.target is None
    assert sender.valves.states["sample_valve"].state == "FAULT"


@pytest.mark.parametrize("name,axis", [("wash", "A"), ("sheath", "B"), ("sample", "C")])
async def test_service_step_releases_only_selected_motor_after_completion(name, axis):
    sender, transport = await stack(StreamingValveMarlin())
    transport.driver_enabled.update({a: True for a in "ABCXY"})
    await sender.valves.jog(name, -.5, service=True)
    assert transport.target is None
    assert not transport.driver_enabled[axis]
    assert all(transport.driver_enabled[a] for a in "ABCXY" if a != axis)
    release = transport.commands.index(f"M18 {axis}")
    assert transport.commands[release - 1] == "M119"
    assert "M400" in transport.commands[:release]
    assert not any(c.startswith("G1") for c in transport.commands[release + 1:])
    assert [c for c in transport.commands if c.startswith("M18")] == [f"M18 {axis}"]
    assert not any(c.startswith(("M17", "M906", "M500")) for c in transport.commands)
    assert not any(sender._motion.homed.values())
    assert sender.valves.snapshot()["valves"]["ABC".index(axis)]["service_release"] == "ACKNOWLEDGED"
    assert transport.soft_endstops
    assert sender.valves.active_service is None


async def test_service_unexpected_contact_stops_then_requests_selected_release():
    sender, transport = await stack(StreamingValveMarlin())
    transport.inject_on_next_move = True
    with pytest.raises(ControllerError, match="Unexpected"):
        await sender.valves.jog("sample", -.5, service=True)
    contact = transport.triggered_while_moving[0][0]
    assert transport.commands[contact + 1] == "M410"
    assert transport.commands[-2:] == ["M410", "M18 C"]
    assert transport.commands.count("M18 C") == 1
    assert not transport.driver_enabled["C"]
    assert transport.target is None
    assert sender.valves.states["sample_valve"].state == "FAULT"
    assert sender.valves.states["sample_valve"].service_release == "REQUESTED_UNCONFIRMED"
    with pytest.raises(ControllerError, match="aborted"):
        await sender._q.send("G1 A1")


class InterruptedServiceMarlin(StreamingValveMarlin):
    """не отвечаем на G1 или теряем связь после старта мотора"""
    def __init__(self, fault=None):
        super().__init__()
        self.started = asyncio.Event()
        self.fault = fault

    async def _handle(self, command):
        await super()._handle(command)
        if command.startswith("G1 C"):
            self._inbox.get_nowait()  
            self.started.set()
            if self.fault == "disconnect":
                self.force_disconnect()
                raise TransportClosed("USB lost during service step")


@pytest.mark.parametrize("interrupt", ["stop", "cancel", "timeout", "disconnect"])
async def test_service_interruption_requests_release_without_resuming(interrupt):
    transport = InterruptedServiceMarlin(interrupt)
    sender, _ = await stack(transport)
    if interrupt == "timeout":
        sender._timeouts = sender._timeouts.model_copy(update={"move": .02})
    transport.driver_enabled.update({"A": True, "B": True, "X": True, "Y": True})
    task = asyncio.create_task(sender.valves.jog("sample", -.5, service=True))
    await asyncio.wait_for(transport.started.wait(), timeout=1)
    if interrupt == "stop":
        await sender.abort()
    elif interrupt == "cancel":
        task.cancel()
    with pytest.raises((ControllerError, TransportClosed, TransportTimeout, asyncio.CancelledError)):
        await asyncio.wait_for(task, timeout=1)
    state = sender.valves.states["sample_valve"]
    assert state.state == "FAULT"
    assert not any(sender._motion.homed.values())
    assert sender.valves.active_service is None
    assert all(transport.driver_enabled[a] for a in "ABXY")
    if interrupt == "disconnect":
        assert state.service_release == "UNKNOWN"
        assert transport.driver_enabled["C"]  # обрыв USB не доказывает, что мотор отключился
        assert "M18 C" not in transport.commands
        assert any("M18 C" in entry["line"] for entry in sender._q.trace)
    else:
        assert not transport.driver_enabled["C"]
        assert state.service_release == "REQUESTED_UNCONFIRMED"
        assert transport.commands.count("M18 C") == 1
        stop = transport.commands.index("M410")
        assert not any(c.startswith("G1") for c in transport.commands[stop + 1:])
    with pytest.raises(ControllerError, match="aborted"):
        await sender._q.send("G1 A1")


async def test_service_disable_ack_failure_is_not_reported_as_acknowledged():
    sender, transport = await stack()
    original = transport._handle

    async def missing_disable_ack(command):
        await original(command)
        if command == "M18 C":
            transport.force_disconnect()
            raise TransportClosed("USB lost before disable ACK")

    transport._handle = missing_disable_ack
    with pytest.raises(TransportClosed):
        await sender.valves.jog("sample", -.1, service=True)
    assert sender.valves.states["sample_valve"].service_release == "UNKNOWN"
    assert sender.valves.states["sample_valve"].state == "FAULT"


async def test_normal_commissioning_jog_keeps_reference_and_does_not_release():
    sender, transport = await stack()
    await sender.valves.calibrate("sample")
    assert not any(c.startswith("M18") for c in transport.commands)
    transport.commands.clear()
    await sender.valves.jog("sample", -.1)
    assert sender._motion.homed["C"]
    assert sender.valves.states["sample_valve"].commissioning
    assert not any(c.startswith("M18") for c in transport.commands)


async def test_ambiguous_extruder_motor_release_is_rejected_before_motion():
    sender, transport = await stack()
    sender._map = sender._map.model_copy(update={"valves": [
        sender._map.valves[2].model_copy(update={"axis": "E3"})]})
    with pytest.raises(ValueError, match="single-letter"):
        await sender.valves.jog("sample", -.1, service=True)
    assert transport.commands == []


async def test_diagnostic_home_pulls_off_then_releases_and_loses_reference():
    sender, transport = await stack(StreamingValveMarlin())
    sender._map = sender._map.model_copy(update={"valves": [
        v.model_copy(update={"home_timeout_s": 5}) for v in sender._map.valves]})
    transport.driver_enabled.update({"A": True, "B": True})
    result = await sender.valves.test_home("sample")
    assert result["shared_endstop"] == "OPEN"
    assert result["release"] == "ACKNOWLEDGED"
    assert not result["referenced"]
    assert transport.target is None
    assert not transport.driver_enabled["C"]
    assert all(transport.driver_enabled[a] for a in "AB")
    assert not sender._motion.homed["C"]
    assert "C" not in sender.reference_counts
    assert transport.commands.count("M18 C") == 1
    assert transport.soft_endstops
    assert sender._map.valves[2].calibrated
    with pytest.raises(StateError, match="Home required"):
        await sender.set_valve("sample", "A")


@pytest.mark.parametrize("interrupt", ["stop", "timeout", "disconnect"])
async def test_diagnostic_home_fault_requests_release_and_never_references(interrupt):
    sender, transport = await stack(InterruptedServiceMarlin(interrupt))
    if interrupt == "timeout":
        sender._timeouts = sender._timeouts.model_copy(update={"move": .02})
    task = asyncio.create_task(sender.valves.test_home("sample"))
    await asyncio.wait_for(transport.started.wait(), 1)
    if interrupt == "stop":
        await sender.abort()
    with pytest.raises((ControllerError, TransportTimeout, TransportClosed)):
        await asyncio.wait_for(task, 1)
    assert not any(sender._motion.homed.values())
    assert sender.valves.states["sample_valve"].state == "FAULT"
    assert sender.valves.states["sample_valve"].service_release == (
        "UNKNOWN" if interrupt == "disconnect" else "REQUESTED_UNCONFIRMED")
    if interrupt != "disconnect":
        assert not transport.driver_enabled["C"]
        assert transport.commands.count("M18 C") == 1


@pytest.mark.parametrize("interrupt", ["stop", "timeout", "disconnect"])
async def test_ordinary_home_fault_also_requests_selected_motor_release(interrupt):
    sender, transport = await stack(InterruptedServiceMarlin(interrupt))
    transport.driver_enabled.update({"A": True, "B": True})
    if interrupt == "timeout":
        sender._timeouts = sender._timeouts.model_copy(update={"move": .02})
    task = asyncio.create_task(sender.home_valve("sample"))
    await asyncio.wait_for(transport.started.wait(), 1)
    if interrupt == "stop":
        await sender.abort()
    with pytest.raises((ControllerError, TransportTimeout, TransportClosed)):
        await asyncio.wait_for(task, 1)
    assert not any(sender._motion.homed.values())
    assert sender.valves.states["sample_valve"].service_release == (
        "UNKNOWN" if interrupt == "disconnect" else "REQUESTED_UNCONFIRMED")
    assert all(transport.driver_enabled[a] for a in "AB")
    if interrupt != "disconnect":
        assert not transport.driver_enabled["C"]
        assert transport.commands.count("M18 C") == 1


def test_diagnostic_home_api_releases_selected_motor(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    from flowengine.config_store import save_yaml
    from flowengine.schemas import ConfigurationProfile
    save_yaml("profiles", "valve_bench", ConfigurationProfile(
        name="valve_bench", device_map=valve_map(), runtime=RuntimeParams()))
    with TestClient(create_app(AppSettings(mock=True, profile="valve_bench", log_level="WARNING"))) as client:
        response = client.post("/api/valves/sample", json={"action": "test_home"})
        assert response.status_code == 200, response.text
        status = wait_job(client)
        assert status["shared_valve_endstop"]["state"] == "OPEN"
        assert not status["valves"][2]["referenced"]
        assert status["valves"][2]["service_release"] == "ACKNOWLEDGED"
        assert not client.app.state.ctx.transport.driver_enabled["C"]


async def test_soft_endstop_state_is_preserved_and_stop_loses_reference():
    sender, transport = await stack()
    transport.soft_endstops = False
    await sender.home_valve("sample")
    assert not transport.soft_endstops
    await sender.abort()
    assert not sender._motion.homed["C"]
    assert sender.valves.snapshot()["valves"][2]["current"] == "UNKNOWN"
    assert sender._map.valves[2].calibrated


async def test_release_failure_never_references_or_runs_next_valve():
    sender, transport = await stack()
    original = transport._handle

    async def stuck_after_contact(command):
        if sender.valves.phase == "release":
            transport.valve_endstop_stuck = True
        await original(command)

    transport._handle = stuck_after_contact
    with pytest.raises(ControllerError, match="watchdog"):
        await sender.valves.home_all()
    assert not any(sender._motion.homed.values())
    assert not any(c.startswith(("G1 B", "G1 C")) for c in transport.commands)


def wait_job(client):
    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get("/api/valves").json()
        if status["job"] and status["job"]["state"] != "running":
            assert status["job"]["state"] == "completed", status
            return status
        time.sleep(.01)
    raise AssertionError("Valve job timeout")


def test_commissioning_persists_home_relative_positions_and_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    from flowengine.config_store import save_yaml
    from flowengine.schemas import ConfigurationProfile
    save_yaml("profiles", "valve_bench", ConfigurationProfile(
        name="valve_bench", device_map=valve_map(), runtime=RuntimeParams()))
    with TestClient(create_app(AppSettings(mock=True, profile="valve_bench", log_level="WARNING"))) as client:
        assert client.get("/valves").status_code == 200
        assert client.post("/api/valves/sample", json={"action": "calibrate"}).status_code == 200
        status = wait_job(client)
        assert not status["valves"][2]["calibrated"]
        assert status["valves"][2]["commissioning"]
        for position in ("a", "b"):
            assert client.post("/api/valves/sample", json={"action": "jog", "delta": -.1}).status_code == 200
            status = wait_job(client)
            coordinate = status["valves"][2]["coordinate"]
            saved = client.post("/api/valves/sample", json={"action": "save_" + position})
            assert saved.status_code == 200, saved.text
            assert saved.json()["coordinate"] == pytest.approx(coordinate)
            assert saved.json()["steps_from_home"] == round(coordinate * 80)
        assert saved.json()["calibrated"]
        before = client.get("/api/valves").json()["valves"][2]["config"]
        assert client.post("/api/reconnect").status_code == 200
        status = client.get("/api/valves").json()["valves"][2]
        assert status["config"] == before
        assert not status["referenced"]
        assert status["current"] == "UNKNOWN"
        assert client.post("/api/valves/sample", json={"action": "go_a"}).status_code == 409
    with TestClient(create_app(AppSettings(mock=True, profile="valve_bench", log_level="WARNING"))) as client:
        status = client.get("/api/valves").json()["valves"][2]
        assert status["config"] == before
        assert not status["referenced"]
        assert status["current"] == "UNKNOWN"


def test_api_requires_explicit_m119_channel_and_blocks_unknown_source(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    with TestClient(create_app(AppSettings(mock=True, log_level="WARNING"))) as client:
        from flowengine.api.router_axis_calibration import _apply_runtime_device_map
        ctx = client.app.state.ctx
        _apply_runtime_device_map(ctx, ctx.device_map.model_copy(update={
            "shared_valve_endstop": SharedValveEndstop(channel=None)}))
        assert client.post("/api/valves/home-all").status_code == 409
        inputs = client.get("/api/valves/inputs").json()
        assert "z_min" in inputs["z_candidates"]
        config = client.get("/api/config").json()["device_map"]
        response = client.put("/api/valves/config", json={"valves": config["valves"],
                              "shared_valve_endstop": {"channel": "z_min"}})
        assert response.status_code == 200, response.text
        client.app.state.ctx.transport.valve_mechanical["C"] = 0
        status = client.get("/api/valves").json()
        assert status["shared_valve_endstop"]["source"] == "UNKNOWN"
        assert client.post("/api/valves/home-all").status_code == 409
        assert client.post("/api/valves/sample", json={"action": "home"}).status_code == 409
        assert not any(c.startswith("G1") for c in client.app.state.ctx.transport.commands)


@pytest.mark.parametrize("action,delta,status", [
    ("service_jog", 1, 400), ("service_jog", -1, 400), ("service_jog", 0, 400),
    ("jog", -.1, 409),
])
def test_invalid_jog_is_rejected_before_job_without_fault(tmp_path, monkeypatch, action, delta, status):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    with TestClient(create_app(AppSettings(mock=True, log_level="WARNING"))) as client:
        response = client.post("/api/valves/sample", json={"action": action, "delta": delta})
        assert response.status_code == status, response.text
        assert client.get("/api/state").json()["state"] == "connected_idle"
        assert getattr(client.app.state.ctx.sender.valves, "job", None) is None
        assert not any(c.startswith(("G1", "M410")) for c in client.app.state.ctx.transport.commands)
