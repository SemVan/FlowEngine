"""рабочая сессия начинается из сохранённой парковки, полный промер сюда не входит"""
from __future__ import annotations

from flowengine.errors import StateError
from flowengine.parking_store import read_receipt, receipt_path, save_receipt
from flowengine.state import State


def parking_status(ctx):
    receipt = read_receipt(ctx)
    return {"configured": bool(ctx.device_map.parking.positions),
            "positions": ctx.device_map.parking.positions,
            "feedrates": ctx.device_map.parking.feedrates,
            "saved_parking": receipt,
            "reference_source": getattr(ctx.sender, "reference_source", "unknown"),
            "note": "Старт из парковки предполагает, что выключенные узлы не сдвигали. Это расчётная привязка, без поиска концевиков."}


async def restore_parking(ctx):
    targets = ctx.device_map.parking.positions
    receipt = read_receipt(ctx)
    if not targets or not receipt:
        return {"restored": False, "reason": "Нет завершённой парковки для текущего профиля"}
    ctx.state.require(State.CONNECTED_IDLE)
    if ctx.runner.status["running"] or ctx.queue.depth or ctx.sender.valves.lock.locked() or ctx.sender.manual_session:
        raise StateError("Сначала завершите текущую операцию")
    if all(ctx.motion.homed.get(a) for a in targets):
        return {"restored": False, "reason": "Привязка уже установлена"}
    if receipt.get("positions") != targets:
        return {"restored": False, "reason": "Парковочные координаты изменились"}
    await ctx.state.transition(State.HOMING, detail="Восстанавливаем координаты из парковки · без движения")
    try:
        # G92 задаёт систему координат, но не двигает моторы
        receipt_path(ctx.transport.name).unlink(missing_ok=True)
        await ctx.sender.hold_references()
        await ctx.queue.send("G92 " + " ".join(f"{a}{v:.6f}" for a, v in targets.items()), timeout=ctx.runtime.timeouts.diagnostics)
        await ctx.sender.wait_idle()
        for axis, position in targets.items():
            ctx.motion.mark_referenced(axis, position)
        for valve in ctx.device_map.valves:
            st = ctx.sender.valves.states[valve.name]
            st.state, st.current, st.error = "REFERENCED", "PARK", None
        ctx.sender.reference_source = "saved_parking"
        await ctx.state.transition(State.CONNECTED_IDLE, detail="Готов из парковки · без HOME")
        return {"restored": True, "positions": targets, "reference_source": "saved_parking"}
    except BaseException:
        await ctx.sender.abort()
        if ctx.state.state == State.HOMING:
            await ctx.state.transition(State.ERRORED, detail="Не удалось восстановить парковку")
        raise


async def park_and_finish(ctx):
    ctx.state.require(State.CONNECTED_IDLE)
    if ctx.runner.status["running"] or ctx.queue.depth or ctx.sender.valves.lock.locked() or ctx.sender.manual_session:
        raise StateError("Сначала завершите текущую операцию")
    targets = ctx.device_map.parking.positions
    if not targets:
        raise ValueError("Задайте device_map.parking.positions в конфиге или запомните текущие положения")
    if ctx.transport.name != "mock" and not ctx.runtime.motion.enabled:
        raise StateError("Движение выключено в активном профиле")
    missing = [a for a in targets if not ctx.motion.homed.get(a)]
    if missing:
        raise StateError("Нет привязки для парковки: " + ", ".join(missing))
    rates = {a: ctx.device_map.parking.feedrates.get(a, ctx.device_map.axis(a).feedrate_default) for a in targets}
    # проверяем весь план до первого движения
    for axis, target in targets.items():
        ctx.motion.plan_absolute(axis, target, rates[axis])
    if any(not v.calibrated for v in ctx.device_map.valves):
        raise StateError("Сначала сохраните рабочие положения всех кранов")
    epoch = ctx.sender.reference_epoch
    await ctx.state.transition(State.MOVING, detail="Парковка перед завершением сессии")
    receipt_path(ctx.transport.name).unlink(missing_ok=True)
    try:
        for valve in ctx.device_map.valves:
            await ctx.sender.valves.park(valve.name, targets[valve.axis], rates[valve.axis])
            if epoch != ctx.sender.reference_epoch:
                raise StateError("Парковка прервана")
        pumps = {a: v for a, v in targets.items() if not ctx.sender.valves.for_axis(a)}
        if pumps:
            await ctx.sender.move_independent(pumps, {a: rates[a] for a in pumps})
        positions = await ctx.sender.wait_idle()
        if epoch != ctx.sender.reference_epoch or any(abs(positions[a] - v) > .02 for a, v in targets.items()):
            raise StateError("Парковочные координаты не достигнуты")
        await ctx.sender.motors(False)
        # отметка появляется только после завершения всего хода и подтверждённого M18
        receipt = save_receipt(ctx, dict(targets))
        ctx.sender.reference_source = "parked"
        await ctx.state.transition(State.CONNECTED_IDLE, detail="Узлы припаркованы · моторы отпущены")
        return {"parked": True, "released": True, "receipt": receipt}
    except BaseException:
        receipt_path(ctx.transport.name).unlink(missing_ok=True)
        await ctx.sender.abort()
        if ctx.state.state == State.MOVING:
            await ctx.state.transition(State.ERRORED, detail="Парковка не завершена; отметка не сохранена")
        raise
