"""сохраняем только успешно завершённую парковку, отдельно от калибровок"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime

from flowengine.config import user_overlay_dir


def receipt_path(transport):
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", transport)
    return user_overlay_dir() / "sessions" / f"parking-{name}.json"


def fingerprint(ctx):
    data = {"profile": ctx.profile_name, "geometry": geometry(ctx.device_map),
            "parking": ctx.device_map.parking.positions,
            "firmware": ctx.runtime.firmware.model_dump(mode="json"),
            "port": getattr(ctx.transport, "port", None)}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def geometry(device):
    return {"axes": [{k: a.model_dump(mode="json")[k] for k in (
        "marlin_axis", "kind", "steps_per_unit", "units", "dir_invert", "home_direction", "axis_calibration",
        "endstop_channels_shared", "endstop_inverted")} for a in device.axes],
        "valves": [(v.name, v.axis, v.direction_toward_home) for v in device.valves],
        "shared": device.shared_valve_endstop.model_dump(),
        "inputs": [g.model_dump() for g in device.endstop_inputs]}


def read_receipt(ctx):
    try:
        data = json.loads(receipt_path(ctx.transport.name).read_text(encoding="utf-8"))
        return data if data.get("fingerprint") == fingerprint(ctx) else None
    except (OSError, ValueError):
        return None


def discard_on_command(transport, command):
    # диагностика не трогает сохранённую парковку; запись и любое движение сбрасывают её заранее
    if command.strip().upper() in {"M105", "M114", "M114 R", "M115", "M119", "M122", "M503", "M906", "M211", "M400", "G90", "G91"}:
        return
    receipt_path(transport).unlink(missing_ok=True)


def save_receipt(ctx, positions):
    path = receipt_path(ctx.transport.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fingerprint": fingerprint(ctx), "profile": ctx.profile_name,
               "timestamp": datetime.now(UTC).isoformat(), "positions": positions,
               "transport": ctx.transport.name, "port": getattr(ctx.transport, "port", None)}
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload
