"""WebSocket telemetry endpoint.

The browser opens `/ws`, then receives a stream of typed JSON envelopes
matching `flowengine.schemas.telemetry`. The connection is one-way (server →
client); commands go via REST.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from flowengine.api.deps import get_ctx

log = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws")
async def telemetry_ws(ws: WebSocket):
    await ws.accept()
    ctx = ws.app.state.ctx
    # Initial snapshot so the UI can paint before the next event arrives.
    await ws.send_text(json.dumps({
        "type": "state",
        "state": ctx.state.state.value,
        "detail": ctx.state.detail,
    }))
    await ws.send_text(json.dumps({
        "type": "position",
        "positions": ctx.motion.positions,
        "settled": False,
    }))
    async with ctx.events.subscribe() as queue:
        try:
            while True:
                msg = await queue.get()
                await ws.send_text(json.dumps(msg))
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001
            log.exception("ws telemetry loop crashed")
            return
