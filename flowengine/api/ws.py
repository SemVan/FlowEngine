"""WebSocket telemetry endpoint.

The browser opens `/ws`, then receives a stream of typed JSON envelopes
matching `flowengine.schemas.telemetry`. The connection is one-way (server →
client); commands go via REST.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from flowengine.api.deps import get_ctx

log = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws")
async def telemetry_ws(ws: WebSocket):
    await ws.accept()
    ctx = ws.app.state.ctx
    # Initial snapshot so the UI can paint before the next event arrives.
    await ws.send_text(
        json.dumps(
            {
                "type": "state",
                "state": ctx.state.state.value,
                "detail": ctx.state.detail,
                "homed": ctx.motion.homed,
                "reference_counts": ctx.sender.reference_counts,
                "reference_sides": ctx.sender.reference_sides,
            }
        )
    )
    await ws.send_text(
        json.dumps(
            {
                "type": "position",
                "positions": ctx.motion.positions,
                "settled": False,
            }
        )
    )
    async with ctx.events.subscribe() as queue:
        receive_task = asyncio.create_task(ws.receive())
        event_task = asyncio.create_task(queue.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {receive_task, event_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if receive_task in done:
                    if receive_task.result()["type"] == "websocket.disconnect":
                        return
                    receive_task = asyncio.create_task(ws.receive())
                if event_task in done:
                    await ws.send_text(json.dumps(event_task.result()))
                    event_task = asyncio.create_task(queue.get())
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except Exception:  # noqa: BLE001
            log.exception("ws telemetry loop crashed")
            return
        finally:
            receive_task.cancel()
            event_task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(receive_task, event_task, return_exceptions=True)
