"""Config endpoints: read current device map + runtime params; future writeback."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flowengine.api.deps import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
async def get_config(ctx: AppContext = Depends(get_ctx)):
    return {
        "device_map": ctx.device_map.model_dump(),
        "runtime": ctx.runtime.model_dump(),
        "modes": ctx.modes.model_dump() if ctx.modes else None,
    }


@router.get("/schemas")
async def get_schemas():
    """JSON schemas for clients that want to render forms generically."""
    from flowengine.schemas import DeviceMap, ModesConfig, RuntimeParams

    return {
        "DeviceMap": DeviceMap.model_json_schema(),
        "RuntimeParams": RuntimeParams.model_json_schema(),
        "ModesConfig": ModesConfig.model_json_schema(),
    }
