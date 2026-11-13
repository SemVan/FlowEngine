"""`python -m flowengine` entry point."""

from __future__ import annotations

import json
import os
import sys

import uvicorn

from flowengine.app import create_app
from flowengine.config import AppSettings, user_overlay_dir
from flowengine.config_store import find_saved, load_yaml
from flowengine.errors import ConfigError
from flowengine.schemas import ConfigurationProfile


def _restore_remembered_profile(settings: AppSettings) -> None:
    explicit = "FLOWENGINE_PROFILE" in os.environ or any(
        arg == "--profile" or arg.startswith("--profile=") for arg in sys.argv[1:]
    )
    if explicit or settings.profile != "default":
        return
    try:
        name = json.loads((user_overlay_dir() / "desktop.json").read_text(encoding="utf-8"))["profile"]
        if name != "default":
            load_yaml(find_saved("profiles", name), ConfigurationProfile)
        settings.profile = name
    except (OSError, ValueError, KeyError, TypeError, ConfigError):
        settings.profile = "default"


def main() -> None:
    settings = AppSettings.from_env_and_args()
    _restore_remembered_profile(settings)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
