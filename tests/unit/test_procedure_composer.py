"""Nested procedure composition."""

from __future__ import annotations

import pytest

from flowengine.errors import ProcedureError
from flowengine.procedures import expand_procedure
from flowengine.schemas import Procedure


def test_call_expands_child_and_maps_parent_parameters():
    procedures = {
        "wash_pump": Procedure.model_validate(
            {
                "name": "wash_pump",
                "parameters": {"distance": {"type": "number"}},
                "steps": [
                    {"op": "move", "axis": "X", "by": "${distance}"},
                    {"op": "log", "message": "washed"},
                ],
            }
        )
    }
    parent = Procedure.model_validate(
        {
            "name": "analysis",
            "parameters": {"wash_distance": {"type": "number", "default": 4.0}},
            "steps": [
                {
                    "op": "call",
                    "procedure": "wash_pump",
                    "parameters": {"distance": "${wash_distance}"},
                }
            ],
        }
    )

    expanded = expand_procedure(parent, procedures.__getitem__)

    assert [step.op for step in expanded.procedure.steps] == ["move", "log"]
    assert expanded.procedure.steps[0].by == 4.0  # type: ignore[union-attr]
    assert expanded.step_paths == [
        "analysis step 1 → wash_pump step 1",
        "analysis step 1 → wash_pump step 2",
    ]


def test_recursive_call_is_rejected():
    procedures = {
        "a": Procedure(name="a", steps=[{"op": "call", "procedure": "b"}]),
        "b": Procedure(name="b", steps=[{"op": "call", "procedure": "a"}]),
    }
    with pytest.raises(ProcedureError, match="a → b → a"):
        expand_procedure(procedures["a"], procedures.__getitem__)


def test_nested_draft_blocks_full_run_but_can_be_previewed():
    child = Procedure(name="child", draft=True, steps=[{"op": "log", "message": "x"}])
    parent = Procedure(name="parent", steps=[{"op": "call", "procedure": "child"}])
    with pytest.raises(ValueError, match=r"child.*draft"):
        expand_procedure(parent, lambda _name: child)
    assert len(expand_procedure(parent, lambda _name: child, allow_draft=True).procedure.steps) == 1
