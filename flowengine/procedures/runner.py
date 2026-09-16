"""Sequential executor for the deliberately small procedure DSL."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import cast

from flowengine.errors import ProcedureError
from flowengine.events import EventBus
from flowengine.hardware.sender import GcodeSender
from flowengine.schemas import (
    CheckpointStep,
    DeviceMap,
    DwellStep,
    HomeStep,
    LogStep,
    MoveMultiStep,
    MoveStep,
    Procedure,
    SetParamStep,
    SetValveStep,
    Step,
    WaitPressureStep,
)
from flowengine.schemas.procedure import (
    MotorsStep,
    PumpMultiStep,
    PumpStep,
    ReadSensorsStep,
    ReferenceTestStep,
    SeekStep,
    ValveCalibrationStep,
)
from flowengine.state import State, StateMachine

log = logging.getLogger(__name__)


class ProcedureRunner:
    def __init__(
        self,
        sender: GcodeSender,
        device_map: DeviceMap,
        state: StateMachine,
        events: EventBus,
    ) -> None:
        self._sender = sender
        self._map = device_map
        self._state = state
        self._events = events
        self._task: asyncio.Task[None] | None = None
        self._single_step_lock = asyncio.Lock()
        self._name: str | None = None
        self._step = 0
        self._step_path: str | None = None
        self._error: str | None = None
        self._results: list[dict[str, object]] = []

    @property
    def status(self) -> dict[str, object]:
        return {
            "running": self._task is not None and not self._task.done(),
            "name": self._name,
            "step": self._step,
            "step_path": self._step_path,
            "error": self._error,
            "results": list(self._results),
        }

    def start(self, proc: Procedure, step_paths: list[str] | None = None) -> None:
        self._state.require(State.CONNECTED_IDLE)
        if proc.draft:
            raise ProcedureError(f"procedure {proc.name!r} is a draft")
        if self._single_step_lock.locked():
            raise ProcedureError("a single procedure step is already running")
        if self._task is not None and not self._task.done():
            raise ProcedureError(f"procedure {self._name!r} is already running")
        self._name, self._step, self._step_path, self._error = proc.name, 0, None, None
        self._results = []
        self._task = asyncio.create_task(self.run(proc, step_paths), name=f"procedure:{proc.name}")

    async def run_one(self, procedure_name: str, step_number: int, step: Step) -> None:
        """Run one explicitly selected, already resolved step and wait for it.

        This is intentionally separate from a full procedure run: developers
        can commission a draft on real hardware without marking it executable.
        """
        await self.run_selected(
            procedure_name,
            step_number,
            [step],
            [f"{procedure_name} step {step_number}"],
        )

    async def run_selected(
        self,
        procedure_name: str,
        step_number: int,
        steps: list[Step],
        step_paths: list[str],
    ) -> None:
        """Run one selected logical step after any nested calls are expanded."""
        if len(steps) != len(step_paths):
            raise ProcedureError("expanded steps and paths do not match")
        if self._task is not None and not self._task.done():
            raise ProcedureError(f"procedure {self._name!r} is already running")
        if self._single_step_lock.locked():
            raise ProcedureError("another single procedure step is already running")
        async with self._single_step_lock:
            self._state.require(State.CONNECTED_IDLE)
            self._name, self._step, self._error = procedure_name, step_number, None
            self._results = []
            self._task = asyncio.create_task(
                self._selected_steps(procedure_name, step_number, steps, step_paths)
            )
            try:
                await self._task
            except asyncio.CancelledError:
                self._error = "aborted; physical position is unverified"
                await self._sender.abort()
                await self._state.transition(State.ERRORED, detail=self._error)
                raise ProcedureError(self._error) from None
            except Exception as exc:
                self._sender.invalidate_positions()
                self._error = str(exc)
                if self._state.state != State.ERRORED:
                    await self._state.transition(
                        State.ERRORED,
                        detail=f"{procedure_name} step {step_number}: {exc}",
                    )
                raise
            finally:
                self._task = None

    async def _selected_steps(
        self, name: str, number: int, steps: list[Step], paths: list[str]
    ) -> None:
        for step, path in zip(steps, paths, strict=True):
            self._step_path = path
            await self._events.publish(
                {
                    "type": "procedure",
                    "name": name,
                    "step": number,
                    "step_path": path,
                    "op": step.op,
                    "single_step": True,
                }
            )
            await self._execute(step)

    async def abort(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    async def run(self, proc: Procedure, step_paths: list[str] | None = None) -> None:
        self._state.require(State.CONNECTED_IDLE)
        paths = step_paths or [
            f"{proc.name} step {index}" for index in range(1, len(proc.steps) + 1)
        ]
        if len(paths) != len(proc.steps):
            raise ProcedureError("expanded steps and paths do not match")
        try:
            for index, (step, path) in enumerate(zip(proc.steps, paths, strict=True), start=1):
                self._step = index
                self._step_path = path
                await self._events.publish(
                    {
                        "type": "procedure",
                        "name": proc.name,
                        "step": index,
                        "step_path": path,
                        "op": step.op,
                    }
                )
                await self._execute(step)
        except asyncio.CancelledError:
            self._error = "aborted; reconnect and re-home before further motion"
            await self._sender.abort()
            await self._state.transition(State.ERRORED, detail=self._error)
            raise
        except Exception as exc:
            self._sender.invalidate_positions()
            self._error = str(exc)
            if self._state.state != State.ERRORED:
                await self._state.transition(State.ERRORED, detail=f"{proc.name}: {exc}")
            log.exception("procedure %s failed", proc.name)
        else:
            await self._events.publish({"type": "procedure", "name": proc.name, "done": True})

    async def _execute(self, step: Step) -> None:
        if isinstance(step, HomeStep):
            await self._state.transition(
                State.HOMING, detail=f"procedure home {step.axes or 'all'}"
            )
            await self._sender.home(step.axes)
            await self._state.transition(State.CONNECTED_IDLE, detail="procedure home complete")
        elif isinstance(step, MoveStep):
            await self._state.transition(State.MOVING, detail=f"procedure move {step.axis}")
            value, speed = self._sender.convert_move(
                step.axis,
                step.to if step.to is not None else step.by,
                step.feedrate,
                step.units,
                step.speed_units,
            )
            async with self._sender.acceleration(step.acceleration, [step.axis]):
                if step.to is not None:
                    await self._sender.move_to(step.axis, value, speed)
                else:
                    await self._sender.jog(step.axis, value, speed)
                await self._sender.wait_idle()
            await self._state.transition(State.CONNECTED_IDLE, detail="procedure move complete")
        elif isinstance(step, SetValveStep):
            valve = next((item for item in self._map.valves if item.name == step.name), None)
            if valve is None:
                raise ProcedureError(f"unknown valve {step.name!r}")
            target = valve.position_a if step.position == "A" else valve.position_b
            await self._state.transition(
                State.MOVING, detail=f"valve {step.name} → {step.position}"
            )
            await self._sender.move_to(valve.axis, target)
            await self._sender.wait_idle()
            await self._state.transition(State.CONNECTED_IDLE, detail="valve move complete")
        elif isinstance(step, DwellStep):
            await asyncio.sleep(cast(float, step.seconds))
        elif isinstance(step, (LogStep, CheckpointStep)):
            message = step.message if isinstance(step, LogStep) else f"checkpoint: {step.name}"
            await self._events.publish({"type": "log", "level": "info", "message": message})
        elif isinstance(step, MoveMultiStep):
            await self._state.transition(State.MOVING, detail="coordinated movement")
            async with self._sender.acceleration(step.acceleration, list(step.axes)):
                result = await self._sender.move_multi(
                    step.axes, step.feedrate, step.relative, step.duration_s
                )
                await self._sender.wait_idle()
            self._results.append({"step_path": self._step_path, "op": step.op, "result": result})
            await self._events.publish({"type": "log", "level": "info", "message": str(result)})
            await self._state.transition(
                State.CONNECTED_IDLE, detail="coordinated movement complete"
            )
        elif isinstance(step, PumpStep):
            pump = next((p for p in self._map.pumps if p.name == step.name), None)
            if pump is None:
                raise ProcedureError("unknown pump")
            await self._state.transition(State.MOVING, detail=f"pump {step.name}")
            async with self._sender.acceleration(step.acceleration, [pump.axis]):
                await self._sender.pump(step.name, step.volume_ul, step.flow_ul_min, step.direction)
                await self._sender.wait_idle()
            await self._state.transition(State.CONNECTED_IDLE, detail="pump complete")
        elif isinstance(step, MotorsStep):
            await self._sender.motors(step.enabled, step.axes)
        elif isinstance(step, PumpMultiStep):
            axes = [p.axis for p in self._map.pumps if p.name in step.pumps]
            await self._state.transition(State.MOVING, detail="coordinated pumps")
            async with self._sender.acceleration(step.acceleration, axes):
                result = await self._sender.pump_multi(step.pumps)
                await self._sender.wait_idle()
            self._results.append({"step_path": self._step_path, "op": step.op, "result": result})
            await self._state.transition(State.CONNECTED_IDLE, detail="coordinated pumps complete")
        elif isinstance(step, ReadSensorsStep):
            result = await self._sender.sensors()
            self._results.append({"step_path": self._step_path, "op": step.op, "result": result})
            await self._events.publish({"type": "log", "level": "info", "message": str(result)})
        elif isinstance(step, SeekStep):
            await self._state.transition(State.HOMING, detail="verified probe search")
            result = await self._sender.seek(
                step.axis, step.channel, step.by, step.feedrate, step.zero
            )
            self._results.append({"step_path": self._step_path, "op": step.op, "result": result})
            await self._events.publish({"type": "log", "level": "info", "message": str(result)})
            await self._state.transition(State.CONNECTED_IDLE, detail="probe complete")
        elif isinstance(step, (ValveCalibrationStep, ReferenceTestStep)):
            await self._state.transition(State.HOMING, detail=step.op)
            kwargs = step.model_dump(exclude={"op"})
            if isinstance(step, ValveCalibrationStep):
                result = await self._sender.calibrate_valve(**kwargs)
            else:
                kwargs["delta"] = kwargs.pop("by")
                result = await self._sender.test_reference(**kwargs)
            self._results.append({"step_path": self._step_path, "op": step.op, "result": result})
            await self._events.publish({"type": "log", "level": "info", "message": str(result)})
            await self._state.transition(State.CONNECTED_IDLE, detail=f"{step.op} complete")
        elif isinstance(step, (SetParamStep, WaitPressureStep)):
            raise ProcedureError(f"operation {step.op!r} is not implemented safely yet")
        else:
            raise ProcedureError(f"unknown procedure operation: {step!r}")
