"""текстовый редактор проверенных YAML и журнал очереди, без прямого доступа к путям"""
from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Annotated, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from flowengine.api.deps import AppContext, get_ctx
from flowengine.config import REPO_CONFIG_DIR
from flowengine.config_store import find_saved, load_yaml, storage_dir, validate_name, without_commissioning
from flowengine.errors import ConfigError
from flowengine.parking_store import geometry
from flowengine.schemas import ConfigurationProfile, Procedure, RackConfig
from flowengine.state import State

router = APIRouter(prefix="/api", tags=["editor"])
Context = Annotated[AppContext, Depends(get_ctx)]
Kind = Literal["profiles", "procedures", "racks"]
MODELS = {"profiles": ConfigurationProfile, "procedures": Procedure, "racks": RackConfig}


class TextDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    revision: str


def source(kind, name):
    validate_name(name)
    try:
        return find_saved(kind, name)
    except ConfigError:
        path = REPO_CONFIG_DIR / kind / f"{name}.yaml"
        if kind == "profiles" or not path.is_file():
            raise
        return path


def editable_text(kind, path):
    text = path.read_text(encoding="utf-8")
    if kind == "profiles":
        # историю измерений оставляем в файле, но не грузим мегабайты TX/RX в редактор
        text = without_commissioning(text)
    return text.strip() + "\n"


def revision(text):
    return hashlib.sha256(text.encode()).hexdigest()


@router.get("/config/text/{kind}/{name}")
async def read_text(kind: Kind, name: str):
    try:
        text = editable_text(kind, source(kind, name))
        return {"text": text, "revision": revision(text), "kind": kind, "name": name}
    except (ConfigError, ValueError, yaml.YAMLError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put("/config/text/{kind}/{name}")
async def write_text(kind: Kind, name: str, body: TextDocument, ctx: Context):
    if ctx.runner.status["running"] or ctx.state.state in {State.MOVING, State.HOMING, State.ABORTING} or ctx.sender.manual_session:
        raise HTTPException(409, "Дождитесь завершения операции перед сохранением")
    if kind == "profiles" and name == "default":
        raise HTTPException(400, "Сначала сохраните личный профиль на странице профилей")
    try:
        path = source(kind, name)
        before = editable_text(kind, path)
        if revision(before) != body.revision:
            raise HTTPException(409, "Конфиг уже изменён в другом окне. Скопируйте свои правки и перечитайте файл")
        data = yaml.safe_load(body.text)
        if not isinstance(data, dict) or data.get("name") != name:
            raise ValueError("Корень YAML — объект; поле name должно совпадать с выбранным файлом")
        text = body.text.rstrip() + "\n"
        if kind == "profiles":
            if "commissioning" in data:
                raise ValueError("История commissioning сохраняется автоматически; уберите её из текста")
            previous = load_yaml(path, ConfigurationProfile)
            data["commissioning"] = [r.model_dump(mode="json") for r in previous.commissioning]
            if previous.commissioning:
                text += yaml.safe_dump({"commissioning": data["commissioning"]}, allow_unicode=True, sort_keys=False)
        MODELS[kind].model_validate(data)
        target = storage_dir(kind) / f"{validate_name(name)}.yaml"
        fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        saved = editable_text(kind, target)
        return {"saved": True, "revision": revision(saved), "text": saved,
                "active_session_changed": False}
    except (ConfigError, ValueError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/config/editor/apply/{name}")
async def apply_text_profile(name: str, ctx: Context):
    from flowengine.api.router_commissioning import apply_profile, idle
    from flowengine.parking import restore_parking
    idle(ctx)
    try:
        profile = load_yaml(find_saved("profiles", name), ConfigurationProfile)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    if (name != ctx.profile_name or geometry(profile.device_map) != geometry(ctx.device_map)
            or profile.runtime.transport != ctx.runtime.transport or profile.runtime.firmware != ctx.runtime.firmware):
        result = await apply_profile(name, ctx)
        result["parking"] = await restore_parking(ctx)
        return result
    if profile.runtime.motion.configure_firmware:
        raise HTTPException(409, "Для применения без перезапуска требуется configure_firmware: false")
    ctx.queue.configure_default_timeout(profile.runtime.timeouts.ok_default)
    ctx.device_map = ctx.motion._map = ctx.sender._map = profile.device_map
    ctx.runtime, ctx.modes = profile.runtime, profile.modes
    ctx.motion._feedrate_cap = profile.runtime.motion.feedrate_cap
    ctx.sender._timeouts = profile.runtime.timeouts
    ctx.sender._accel_cap = profile.runtime.motion.accel_cap
    ctx.sender._homing._map = profile.device_map
    ctx.sender._homing._timeout = profile.runtime.timeouts.homing
    ctx.sender.valve_commissioning._map = profile.device_map
    ctx.sender.valve_commissioning._timeouts = profile.runtime.timeouts
    ctx.runner._map = profile.device_map
    return {"ok": True, "references_preserved": True, "profile": name, "serial_commands": False}


@router.get("/diagnostics/trace")
async def trace(ctx: Context, after: int = 0, trace_id: str = ""):
    reset = trace_id != ctx.queue.trace_id
    rows = list(ctx.queue.trace)
    return {"trace_id": ctx.queue.trace_id, "reset": reset,
            "latest": ctx.queue.trace_sequence,
            "gap": bool(not reset and rows and after < rows[0]["sequence"] - 1),
            "entries": [r for r in rows if reset or r["sequence"] > after]}
