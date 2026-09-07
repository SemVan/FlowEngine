"""YAML → pydantic loaders. Strict — never silently default wiring-derived fields."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from flowengine.config import REPO_CONFIG_DIR, resolve_config_file
from flowengine.errors import ConfigError
from flowengine.schemas import DeviceMap, ModesConfig, Procedure, RuntimeParams

T = TypeVar("T", bound=BaseModel)


def _load(path: Path, model: type[T]) -> T:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"bad YAML in {path}: {e}") from e
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"invalid {model.__name__} in {path}:\n{e}") from e


def load_device_map() -> DeviceMap:
    return _load(resolve_config_file("device_map.yaml"), DeviceMap)


def load_runtime() -> RuntimeParams:
    return _load(resolve_config_file("runtime.yaml"), RuntimeParams)


def load_modes() -> ModesConfig:
    return _load(resolve_config_file("modes.yaml"), ModesConfig)


def list_procedures() -> list[Path]:
    """Return every .yaml under repo config/procedures plus user overlay procedures."""
    out: list[Path] = []
    for base in (
        REPO_CONFIG_DIR / "procedures",
        resolve_config_file("procedures").parent / "procedures",
    ):
        if base.is_dir():
            for p in sorted(base.glob("*.yaml")):
                if p not in out:
                    out.append(p)
    return out


def load_procedure(path: Path) -> Procedure:
    return _load(path, Procedure)
