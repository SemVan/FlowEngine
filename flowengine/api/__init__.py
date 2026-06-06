"""FastAPI routers."""

from flowengine.api.router_config import router as config_router
from flowengine.api.router_diagnostics import router as diagnostics_router
from flowengine.api.router_modes import router as modes_router
from flowengine.api.router_motion import router as motion_router
from flowengine.api.router_procedures import router as procedures_router
from flowengine.api.router_stepskip import router as stepskip_router
from flowengine.api.ws import router as ws_router

__all__ = [
    "config_router",
    "diagnostics_router",
    "modes_router",
    "motion_router",
    "procedures_router",
    "stepskip_router",
    "ws_router",
]
