"""Runtime settings — CLI args, env vars, and on-disk paths.

CLI flags override env vars override defaults. YAML config files live under
`config/` (defaults shipped with the repo) and `$XDG_CONFIG_HOME/flowengine/`
(operator overlays).
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir

REPO_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@dataclass(slots=True)
class AppSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    profile: str = "default"
    serial_port: str | None = None
    serial_baud: int | None = None
    mock: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env_and_args(cls, argv: list[str] | None = None) -> AppSettings:
        env = os.environ
        defaults = cls(
            host=env.get("FLOWENGINE_HOST", "127.0.0.1"),
            port=int(env.get("FLOWENGINE_PORT", "8765")),
            profile=env.get("FLOWENGINE_PROFILE", "default"),
            serial_port=env.get("FLOWENGINE_SERIAL_PORT"),
            serial_baud=(
                int(env["FLOWENGINE_SERIAL_BAUD"]) if "FLOWENGINE_SERIAL_BAUD" in env else None
            ),
            mock=env.get("FLOWENGINE_MOCK", "0") not in {"0", "", "false", "False"},
            log_level=env.get("FLOWENGINE_LOG_LEVEL", "INFO"),
        )

        ap = argparse.ArgumentParser(
            prog="flowengine",
            description="FlowEngine — flow cytometer fluidics control",
        )
        ap.add_argument("--host", default=defaults.host)
        ap.add_argument("--port", type=int, default=defaults.port)
        ap.add_argument("--profile", default=defaults.profile)
        ap.add_argument("--serial-port", default=defaults.serial_port)
        ap.add_argument("--serial-baud", type=int, default=defaults.serial_baud)
        ap.add_argument(
            "--mock",
            action="store_true",
            default=defaults.mock,
            help="Use MockTransport (no hardware required).",
        )
        ap.add_argument(
            "--log-level", default=defaults.log_level, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
        )
        ns = ap.parse_args(argv)
        return cls(
            host=ns.host,
            port=ns.port,
            profile=ns.profile,
            serial_port=ns.serial_port,
            serial_baud=ns.serial_baud,
            mock=ns.mock,
            log_level=ns.log_level,
        )


def user_overlay_dir() -> Path:
    p = Path(user_config_dir("flowengine", appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_config_file(name: str) -> Path:
    """Return the user overlay path if present, else the repo default."""
    overlay = user_overlay_dir() / name
    if overlay.is_file():
        return overlay
    return REPO_CONFIG_DIR / name
