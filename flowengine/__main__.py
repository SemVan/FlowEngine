"""`python -m flowengine` entry point."""

from __future__ import annotations

import uvicorn

from flowengine.app import create_app
from flowengine.config import AppSettings


def main() -> None:
    settings = AppSettings.from_env_and_args()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
