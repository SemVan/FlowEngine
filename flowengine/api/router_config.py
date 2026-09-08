"""Read active config and persist validated profiles and rack definitions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from flowengine.api.deps import AppContext, get_ctx
from flowengine.config import REPO_CONFIG_DIR
from flowengine.config_store import find_saved, list_saved, load_yaml, save_yaml
from flowengine.errors import ConfigError
from flowengine.schemas import ConfigurationProfile, RackConfig

router = APIRouter(prefix="/api", tags=["config"])
Context = Annotated[AppContext, Depends(get_ctx)]


@router.get("/config")
async def get_config(ctx: Context):
    return {
        "device_map": ctx.device_map.model_dump(),
        "runtime": ctx.runtime.model_dump(),
        "modes": ctx.modes.model_dump() if ctx.modes else None,
    }


@router.get("/schemas")
async def get_schemas():
    """JSON schemas for clients that want to render forms generically."""
    from flowengine.schemas import DeviceMap, ModesConfig, Procedure, RuntimeParams

    return {
        "DeviceMap": DeviceMap.model_json_schema(),
        "RuntimeParams": RuntimeParams.model_json_schema(),
        "ModesConfig": ModesConfig.model_json_schema(),
        "ConfigurationProfile": ConfigurationProfile.model_json_schema(),
        "RackConfig": RackConfig.model_json_schema(),
        "Procedure": Procedure.model_json_schema(),
    }


@router.get("/config/profiles")
async def profiles():
    return [_summary(path, ConfigurationProfile) for path in list_saved("profiles")]


@router.get("/config/profiles/{name}")
async def get_profile(name: str):
    try:
        return load_yaml(find_saved("profiles", name), ConfigurationProfile).model_dump()
    except ConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/config/profiles/{name}")
async def put_profile(name: str, profile: ConfigurationProfile):
    if profile.name != name:
        raise HTTPException(status_code=400, detail="URL name must match profile.name")
    try:
        path = save_yaml("profiles", name, profile)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "name": name,
        "file": path.name,
        "restart_required": True,
        "restart_command": f"python -m flowengine --profile {name}",
    }


@router.get("/config/racks")
async def racks():
    paths = list_saved("racks")
    seen = {path.name for path in paths}
    paths.extend(
        path for path in sorted((REPO_CONFIG_DIR / "racks").glob("*.yaml")) if path.name not in seen
    )
    return [_summary(path, RackConfig) for path in paths]


@router.get("/config/racks/{name}")
async def get_rack(name: str):
    try:
        try:
            path = find_saved("racks", name)
        except ConfigError:
            path = REPO_CONFIG_DIR / "racks" / f"{name}.yaml"
            if not path.is_file():
                raise ConfigError(f"rack {name!r} not found") from None
        return load_yaml(path, RackConfig).model_dump()
    except ConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/config/racks/{name}")
async def put_rack(name: str, rack: RackConfig):
    if rack.name != name:
        raise HTTPException(status_code=400, detail="URL name must match rack.name")
    try:
        path = save_yaml("racks", name, rack)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "name": name, "file": path.name}


@router.post("/config/racks/{name}/plan")
async def plan_rack(name: str, cells: list[str]):
    rack = await get_rack(name)
    model = RackConfig.model_validate(rack)
    try:
        return {
            "rack": name,
            "axes": {"x": model.x_axis, "y": model.y_axis, "z": model.z_axis},
            "positions": [model.coordinates(cell) for cell in cells],
            "executable": bool(
                model.x_axis and model.y_axis and model.z_axis and model.safe_z is not None
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _summary(path, model):
    try:
        item = load_yaml(path, model)
        return {"name": item.name, "description": item.description, "ok": True}
    except ConfigError as exc:
        return {"name": path.stem, "ok": False, "error": str(exc)}
