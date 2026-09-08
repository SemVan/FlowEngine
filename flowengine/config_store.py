"""Validated, atomic persistence for user-editable FlowEngine YAML files."""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from flowengine.config import user_overlay_dir
from flowengine.errors import ConfigError

T = TypeVar("T", bound=BaseModel)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_name(name: str) -> str:
    if not _SAFE_NAME.fullmatch(name):
        raise ConfigError(
            "name must start with a letter or digit and contain only letters, "
            "digits, underscore, or hyphen (maximum 64 characters)"
        )
    return name


def storage_dir(kind: str) -> Path:
    if kind not in {"procedures", "profiles", "racks"}:
        raise ConfigError(f"unsupported config kind {kind!r}")
    path = user_overlay_dir() / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_yaml(kind: str, name: str, model: BaseModel) -> Path:
    """Atomically replace one validated model without accepting arbitrary paths."""
    path = storage_dir(kind) / f"{validate_name(name)}.yaml"
    payload = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=True),
        allow_unicode=True,
        sort_keys=False,
    )
    fd, temporary = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return path


def load_yaml(path: Path, model: type[T]) -> T:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return model.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"invalid {model.__name__} in {path.name}: {exc}") from exc


def list_saved(kind: str) -> list[Path]:
    return sorted(storage_dir(kind).glob("*.yaml"))


def find_saved(kind: str, name: str) -> Path:
    path = storage_dir(kind) / f"{validate_name(name)}.yaml"
    if not path.is_file():
        raise ConfigError(f"saved {kind[:-1]} {name!r} not found")
    return path
