"""Procedure YAML loader (lint-only in Phase 1).

The runner is Phase 2; here we expose pure loading + a lint pass that checks
referenced axes/valves/pumps exist in the device map. The Procedures page in
the UI calls this to surface bad procedures *before* anyone tries to run them.
"""

from __future__ import annotations

from pathlib import Path

from flowengine.errors import ConfigError, ProcedureError
from flowengine.loaders import load_procedure
from flowengine.schemas import (
    DeviceMap,
    HomeStep,
    MoveMultiStep,
    MoveStep,
    Procedure,
    SetValveStep,
)


def lint(proc: Procedure, dm: DeviceMap) -> list[str]:
    """Return a list of lint warnings/errors. Empty list = clean."""
    issues: list[str] = []
    known_axes = {a.marlin_axis for a in dm.axes}
    known_valves = {v.name for v in dm.valves}
    for i, step in enumerate(proc.steps):
        if isinstance(step, MoveStep):
            if step.axis not in known_axes:
                issues.append(f"step {i}: unknown axis {step.axis!r}")
        elif isinstance(step, MoveMultiStep):
            for axis in step.axes:
                if axis not in known_axes:
                    issues.append(f"step {i}: unknown axis {axis!r}")
        elif isinstance(step, HomeStep):
            if step.axes is not None:
                for axis in step.axes:
                    if axis not in known_axes:
                        issues.append(f"step {i}: unknown axis {axis!r}")
        elif isinstance(step, SetValveStep):
            if step.name not in known_valves:
                issues.append(f"step {i}: unknown valve {step.name!r}")
    return issues


def load_and_lint(path: Path, dm: DeviceMap) -> Procedure:
    try:
        proc = load_procedure(path)
    except ConfigError as e:
        raise ProcedureError(str(e)) from e
    issues = lint(proc, dm)
    if issues:
        raise ProcedureError(f"procedure {proc.name!r} has issues:\n  - " + "\n  - ".join(issues))
    return proc
