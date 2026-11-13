"""ищем концевик непрерывным ходом, проверяя M119 во время движения"""
from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from flowengine.errors import ControllerError
from flowengine.hardware.queue import CommandQueue

StopReason = Callable[[], str | None]
PollCallback = Callable[[float, bool], Awaitable[None]]


@dataclass(frozen=True)
class SeekOutcome:
    reached: bool
    stop_reason: str | None
    elapsed_s: float
    polls: int


async def _switch_state(
    queue: CommandQueue,
    channel: str,
    timeout: float,
) -> bool:
    result = await queue.send("M119", timeout=timeout)
    if channel not in (result.endstops or {}):
        raise ControllerError(f"endstop input {channel} missing from M119")
    return bool(result.endstops[channel])


async def _normal_quickstop(
    queue: CommandQueue,
    *,
    axis: str,
    steps_per_unit: float | None,
    diagnostics_timeout: float,
    move_timeout: float,
) -> None:
    """обычное касание: M410 через очередь без аварийной блокировки"""
    # ответ на M410 бывает медленнее, чем на M119
    stop_timeout = max(2.0, diagnostics_timeout)
    await queue.send("M410", timeout=stop_timeout)
    await queue.send("M400", timeout=max(stop_timeout, move_timeout))

    # после стопа берём Count: цель M114 могла устареть
    if steps_per_unit and steps_per_unit > 0:
        report = await queue.send("M114 R", timeout=diagnostics_timeout)

        def read_count(result):
            for line in result.raw:
                if "Count " not in line:
                    continue
                found = re.search(rf"\b{re.escape(axis)}:(-?\d+)\b", line.split("Count ", 1)[1])
                if found:
                    return int(found.group(1))
            return None

        count = read_count(report)
        if count is None:
            # некоторые прошивки отдают Count только на обычный M114
            fallback = await queue.send("M114", timeout=diagnostics_timeout)
            count = read_count(fallback)
        if count is not None:
            actual = count / steps_per_unit
        elif axis in (report.position or {}):
            actual = report.position[axis]
        else:
            raise ControllerError(
                f"{axis}: cannot resynchronize position after M410; "
                "M114 R/Count unavailable"
            )
        await queue.send(f"G92 {axis}{actual:.6f}", timeout=diagnostics_timeout)


async def seek_switch(
    queue: CommandQueue,
    *,
    axis: str,
    channel: str,
    direction: int,
    feedrate: float,
    target_pressed: bool,
    timeout_s: float | None,
    steps_per_unit: float | None = None,
    diagnostics_timeout: float,
    move_timeout: float,
    poll_interval_s: float = 0.04,
    stop_reason: StopReason | None = None,
    on_poll: PollCallback | None = None,
) -> SeekOutcome:
    if direction not in {-1, 1}:
        raise ValueError("direction must be -1 or +1")
    if not math.isfinite(feedrate) or feedrate <= 0 or (timeout_s is not None and (not math.isfinite(timeout_s) or timeout_s <= 0)):
        raise ValueError("feedrate and timeout must be positive")

    loop = asyncio.get_running_loop()
    started = loop.time()
    polls = 0

    async def report(pressed: bool) -> None:
        if on_poll is not None:
            await on_poll(loop.time() - started, pressed)

    # mock выполняет ход мгновенно, поэтому используем короткие шаги
    if queue.transport_name == "mock":
        step = max(0.25, min(2.0, feedrate * max(poll_interval_s, 0.04) / 60.0 * 2.0))
        await queue.send("G90", timeout=diagnostics_timeout)
        while True:
            reason = stop_reason() if stop_reason else None
            if reason:
                return SeekOutcome(False, reason, loop.time() - started, polls)
            if timeout_s is not None and loop.time() - started >= timeout_s:
                return SeekOutcome(False, "watchdog_timeout", loop.time() - started, polls)
            current = await queue.send("M114", timeout=diagnostics_timeout)
            if axis not in (current.position or {}):
                raise ControllerError(f"{axis}: controller did not report position during seek")
            target = current.position[axis] + direction * step
            await queue.send(f"G1 {axis}{target:.4f} F{feedrate:.2f}", timeout=move_timeout)
            await queue.send("M400", timeout=move_timeout)
            pressed = await _switch_state(queue, channel, diagnostics_timeout)
            polls += 1
            await report(pressed)
            if pressed == target_pressed:
                return SeekOutcome(True, None, loop.time() - started, polls)

    # без таймера продлеваем ход заранее; M119 и STOP продолжают работать
    watchdog_distance = feedrate if timeout_s is None else feedrate * timeout_s / 60.0 * 1.05
    next_segment_at = started + 50.0
    await queue.send("G91", timeout=diagnostics_timeout)
    try:
        await queue.send(
            f"G1 {axis}{direction * watchdog_distance:.4f} F{feedrate:.2f}",
            timeout=move_timeout,
        )
        while True:
            reason = stop_reason() if stop_reason else None
            if reason:
                await _normal_quickstop(
                    queue,
                    axis=axis,
                    steps_per_unit=steps_per_unit,
                    diagnostics_timeout=diagnostics_timeout,
                    move_timeout=move_timeout,
                )
                return SeekOutcome(False, reason, loop.time() - started, polls)

            elapsed = loop.time() - started
            if timeout_s is not None and elapsed >= timeout_s:
                await _normal_quickstop(
                    queue,
                    axis=axis,
                    steps_per_unit=steps_per_unit,
                    diagnostics_timeout=diagnostics_timeout,
                    move_timeout=move_timeout,
                )
                return SeekOutcome(False, "watchdog_timeout", elapsed, polls)

            if timeout_s is None and loop.time() >= next_segment_at:
                await queue.send(f"G1 {axis}{direction * watchdog_distance:.4f} F{feedrate:.2f}", timeout=move_timeout)
                next_segment_at += 60.0

            pressed = await _switch_state(queue, channel, diagnostics_timeout)
            polls += 1
            await report(pressed)
            if pressed == target_pressed:
                await _normal_quickstop(
                    queue,
                    axis=axis,
                    steps_per_unit=steps_per_unit,
                    diagnostics_timeout=diagnostics_timeout,
                    move_timeout=move_timeout,
                )
                return SeekOutcome(True, None, loop.time() - started, polls)
            await asyncio.sleep(poll_interval_s)
    finally:
        # если связь уже сломалась, сохраняем первую ошибку
        try:
            await queue.send("G90", timeout=diagnostics_timeout)
        except Exception:
            pass
