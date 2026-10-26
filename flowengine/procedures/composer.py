"""Resolve parameters and expand nested procedure calls into executable steps."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from flowengine.errors import ProcedureError
from flowengine.schemas import CallStep, Procedure, RepeatStep, Step

ProcedureLoader = Callable[[str], Procedure]


@dataclass(frozen=True, slots=True)
class ExpandedProcedure:
    procedure: Procedure
    step_paths: list[str]


def expand_procedure(
    source: Procedure,
    loader: ProcedureLoader,
    parameters: dict[str, float | int | str | bool] | None = None,
    *,
    allow_draft: bool = False,
    max_depth: int = 8,
    max_steps: int = 10_000,
) -> ExpandedProcedure:
    """Resolve and flatten calls/repeats while retaining a readable source path."""
    steps: list[Step] = []
    paths: list[str] = []

    def visit(
        current: Procedure,
        supplied: dict[str, float | int | str | bool] | None,
        stack: tuple[str, ...],
        prefix: tuple[str, ...],
    ) -> None:
        if current.name in stack:
            cycle = " → ".join((*stack, current.name))
            raise ProcedureError(f"recursive procedure call: {cycle}")
        if len(stack) >= max_depth:
            chain = " → ".join((*stack, current.name))
            raise ProcedureError(f"procedure nesting exceeds {max_depth}: {chain}")

        resolved = current.resolve(supplied, allow_draft=allow_draft)
        next_stack = (*stack, current.name)
        for index, step in enumerate(resolved.steps, start=1):
            location = (*prefix, f"{current.name} step {index}")
            if isinstance(step, CallStep):
                child = loader(step.procedure)
                visit(child, step.parameters, next_stack, location)
            elif isinstance(step, RepeatStep):
                child = loader(step.procedure)
                for iteration in range(1, step.times + 1):
                    visit(
                        child,
                        step.parameters,
                        next_stack,
                        (*location, f"repeat {iteration}/{step.times}"),
                    )
            else:
                steps.append(step)
                paths.append(" → ".join(location))
                if len(steps) > max_steps:
                    raise ProcedureError(f"expanded procedure exceeds {max_steps} steps")

    visit(source, parameters, (), ())
    flattened = Procedure(
        name=source.name,
        title=source.title,
        description=source.description,
        version=source.version,
        draft=False,
        parameters={},
        steps=steps,
    )
    return ExpandedProcedure(flattened, paths)
