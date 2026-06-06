"""Tiny async pub/sub for telemetry fanout (state, log, position) to WS subscribers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class EventBus:
    """Fanout to all subscribers. Drops the oldest item if a subscriber lags."""

    def __init__(self, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[Any]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, message: Any) -> None:
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Any]]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(q)
        try:
            yield q
        finally:
            async with self._lock:
                self._subscribers.discard(q)
