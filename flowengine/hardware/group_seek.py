"""совместный поиск X/Y с раздельными входами концевиков"""
from __future__ import annotations

import asyncio
import math
import re

from flowengine.errors import ControllerError, StateError


async def seek_group(sender, axes, directions, speeds, pressed, timeouts, *, cancelled, progress=None):
    if not axes or not set(axes) <= {"X", "Y"}:
        raise ValueError("совместный поиск поддерживает только X/Y")
    pending = set(axes)
    started = asyncio.get_running_loop().time()
    found = {}
    queue = sender._q
    diagnostics = sender._timeouts.diagnostics
    move_timeout = sender._timeouts.move

    async def switches():
        values = await sender.read_endstops()
        if any(f"{a.lower()}_min" not in values for a in axes):
            raise ControllerError("В M119 отсутствует отдельный вход X/Y")
        return values

    async def stop():
        await queue.send("M410", timeout=max(2, diagnostics))
        await queue.send("M400", timeout=move_timeout)
        report = await queue.send("M114 R", timeout=diagnostics)
        counts = {a: int(v) for line in report.raw if "Count " in line
                  for a, v in re.findall(r"([A-Z]):(-?\d+)", line.split("Count ", 1)[1])}
        if not set(axes) <= counts.keys():
            report = await queue.send("M114", timeout=diagnostics)
            counts.update({a: int(v) for line in report.raw if "Count " in line
                           for a, v in re.findall(r"([A-Z]):(-?\d+)", line.split("Count ", 1)[1])})
        if not set(axes) <= counts.keys():
            raise ControllerError("Для совместного поиска нужны реальные Count X/Y из M114 R")
        sender._counts.update(counts)
        coordinates = {a: counts[a] / sender._map.axis(a).steps_per_unit for a in axes}
        await queue.send("G92 " + " ".join(f"{a}{v:.6f}" for a, v in coordinates.items()), timeout=diagnostics)
        return coordinates, counts

    initial = await switches()
    if any(initial[f"{a.lower()}_min"] == pressed for a in axes):
        raise ControllerError("Неверное начальное состояние концевика X/Y")
    moving = False
    renew_at = 0.0
    try:
        while pending:
            elapsed = asyncio.get_running_loop().time() - started
            if cancelled():
                raise StateError("Совместный поиск X/Y остановлен оператором")
            expired = [a for a in pending if elapsed >= timeouts[a]]
            if expired:
                raise ControllerError(f"{', '.join(sorted(expired))}: концевик не найден за отведённое время")

            if not moving or asyncio.get_running_loop().time() >= renew_at:
                # в симуляторе ход мгновенный; на плате опрашиваем входы во время длинного хода
                window = .04 if queue.transport_name == "mock" else 30.0
                deltas = {a: directions[a] * speeds[a] * window / 60 for a in sorted(pending)}
                feedrate = math.sqrt(sum(speeds[a] ** 2 for a in pending))
                await queue.send("G91", timeout=diagnostics)
                await queue.send("G1 " + " ".join(f"{a}{v:.6f}" for a, v in deltas.items()) + f" F{feedrate:.3f}", timeout=move_timeout)
                moving = True
                renew_at = asyncio.get_running_loop().time() + window * .8
                if queue.transport_name == "mock":
                    await queue.send("M400", timeout=move_timeout)

            values = await switches()
            reached = {a for a in pending if values[f"{a.lower()}_min"] == pressed}
            if progress:
                await progress(f"X/Y: {elapsed:.1f} с · " + " · ".join(f"{a}: {'нажат' if values[f'{a.lower()}_min'] else 'open'}" for a in axes))
            if reached:
                coordinates, counts = await stop()
                moving = False
                values = await switches()
                reached = {a for a in pending if values[f"{a.lower()}_min"] == pressed}
                for a in reached:
                    found[a] = {"position": coordinates[a], "step_count": counts[a]}
                pending -= reached
            elif queue.transport_name == "mock":
                moving = False
            else:
                await asyncio.sleep(.04)
        return found
    finally:
        if moving:
            await stop()
        await queue.send("G90", timeout=diagnostics)


async def move_group_away(sender, axes, directions, distances, speeds, *, cancelled):
    if cancelled():
        raise StateError("Совместный поиск X/Y остановлен оператором")
    positions = await sender.wait_idle()
    duration = max(60 * distances[a] / speeds[a] for a in axes)
    targets = {a: positions[a] + directions[a] * distances[a] for a in axes}
    feedrate = 60 * math.sqrt(sum(distances[a] ** 2 for a in axes)) / duration
    await sender._q.send("G90", timeout=sender._timeouts.diagnostics)
    await sender._q.send("G1 " + " ".join(f"{a}{v:.6f}" for a, v in targets.items()) + f" F{feedrate:.3f}", timeout=sender._timeouts.move)
    await sender.wait_idle()
    if cancelled():
        raise StateError("Совместный поиск X/Y остановлен оператором")
    values = await sender.read_endstops()
    if any(values.get(f"{a.lower()}_min") is not False for a in axes):
        raise ControllerError("После отхода концевики X/Y должны быть OPEN")


async def find_group_edges(sender, axes, directions, seek, latch, backoff, timeouts, *, cancelled, progress=None):
    async def phase(text):
        if progress:
            await progress(text)

    await phase("X/Y одновременно: быстрый поиск концевиков")
    await seek_group(sender, axes, directions, seek, True, timeouts, cancelled=cancelled, progress=progress)
    away = {a: -directions[a] for a in axes}
    release_timeout = {a: max(5, 60 * (backoff[a] + 2) / latch[a]) for a in axes}
    await phase("X/Y одновременно: освобождение концевиков")
    await seek_group(sender, axes, away, latch, False, release_timeout, cancelled=cancelled, progress=progress)
    await move_group_away(sender, axes, away, backoff, seek, cancelled=cancelled)
    await phase("X/Y одновременно: точное касание")
    precise_timeout = {a: max(10, 60 * (backoff[a] + 3) / latch[a]) for a in axes}
    return await seek_group(sender, axes, directions, latch, True, precise_timeout, cancelled=cancelled, progress=progress)
