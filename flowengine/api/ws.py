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
        async def receive_disconnect():
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return

        receiver = asyncio.create_task(receive_disconnect())
        event = None
        try:
            while True:
                event = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {receiver, event}, return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    return
                await ws.send_text(json.dumps(event.result()))
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except Exception:
            log.exception("ws telemetry loop crashed")
            return
        finally:
            tasks = [receiver] + ([event] if event is not None else [])
            for task in tasks:
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
