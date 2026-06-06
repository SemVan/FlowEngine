"""Procedure loader + lint."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from flowengine.errors import ConfigError, ProcedureError
from flowengine.procedures import lint, load_and_lint
from flowengine.schemas import Procedure


@pytest.fixture
def proc_file(tmp_path: Path) -> Path:
    body = {
        "name": "t",
        "description": "",
        "version": 1,
        "steps": [
            {"op": "home", "axes": ["X"]},
            {"op": "move", "axis": "X", "to": 5.0},
            {"op": "set_valve", "name": "sample_valve", "position": "A"},
            {"op": "log", "message": "done"},
        ],
    }
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def test_load_and_lint_clean(proc_file, device_map):
    proc = load_and_lint(proc_file, device_map)
    assert isinstance(proc, Procedure)


def test_lint_catches_unknown_axis(proc_file, device_map):
    body = yaml.safe_load(proc_file.read_text())
    body["steps"][1]["axis"] = "Q"
    proc_file.write_text(yaml.safe_dump(body))
    with pytest.raises(ProcedureError, match="unknown axis"):
        load_and_lint(proc_file, device_map)


def test_lint_catches_unknown_valve(proc_file, device_map):
    body = yaml.safe_load(proc_file.read_text())
    body["steps"][2]["name"] = "nonexistent_valve"
    proc_file.write_text(yaml.safe_dump(body))
    with pytest.raises(ProcedureError, match="unknown valve"):
        load_and_lint(proc_file, device_map)


def test_missing_file_raises(tmp_path, device_map):
    with pytest.raises(ProcedureError):
        load_and_lint(tmp_path / "nope.yaml", device_map)


def test_bad_yaml_raises(tmp_path, device_map):
    p = tmp_path / "bad.yaml"
    p.write_text("steps:\n  - op: move\n  axis: X\n    to: 5\n")
    with pytest.raises(ProcedureError):
        load_and_lint(p, device_map)
