"""при смене профиля перезапускаем локальный сервер"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import threading
from pathlib import Path

import uvicorn
from fastapi import HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from flowengine.app import create_app
from flowengine.config import AppSettings
from flowengine.config_store import find_saved, load_yaml
from flowengine.errors import ConfigError
from flowengine.schemas import ConfigurationProfile


class Selection(BaseModel):
    name: str


class DesktopSession:
    def __init__(self, data_dir: Path, *, mock: bool = True):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.prefs = data_dir / "desktop.json"
        self.mock = mock
        self.profile = "default"
        try:
            self.profile = json.loads(self.prefs.read_text(encoding="utf-8"))["profile"]
            self.validate(self.profile)
        except (OSError, ValueError, KeyError, TypeError, ConfigError):
            self.profile = "default"
        self.token = secrets.token_urlsafe(32)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = None
        self.loop = None
        self.app = None
        self.ready = False
        self.error = ""
        self.generation = 0
        self.stopping = False
        self.pending = None
        self.in_flight = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    @staticmethod
    def validate(name):
        if name != "default":
            load_yaml(find_saved("profiles", name), ConfigurationProfile)

    def check_idle(self):
        ctx = self.app.state.ctx
        if (ctx.state.state.value not in {"connected_idle", "errored"}
                or ctx.runner.status["running"] or ctx.queue.depth or self.in_flight):
            raise HTTPException(409, "Оборудование занято. Дождитесь завершения операции и повторите.")

    def make_app(self):
        app = create_app(AppSettings(mock=self.mock, profile=self.profile, port=self.port))
        self.app = app
        self.in_flight = 0
        app.state.desktop_restarting = False

        @app.middleware("http")
        async def guard(request: Request, call_next):
            if app.state.desktop_restarting:
                return JSONResponse({"detail": "Применяем профиль…"}, status_code=503)
            tracked = request.url.path.startswith("/api/") and not request.url.path.startswith("/api/desktop/")
            if tracked:
                self.in_flight += 1
            try:
                return await call_next(request)
            finally:
                if tracked:
                    self.in_flight -= 1

        @app.get("/api/desktop/session")
        async def session():
            return {"profile": self.profile, "mock": self.mock, "token": self.token}

        @app.post("/api/desktop/apply")
        async def apply(selection: Selection, request: Request):
            if request.headers.get("X-Desktop-Token") != self.token:
                raise HTTPException(403, "Invalid desktop session")
            try:
                self.validate(selection.name)
            except (ValueError, ConfigError) as exc:
                raise HTTPException(400, str(exc)) from exc
            self.check_idle()
            # сначала блокируем новые запросы, потом дожидаемся текущих
            app.state.desktop_restarting = True
            self.pending = selection.name
            self.ready = False
            self.server.should_exit = True
            return {"ok": True}

        @app.post("/api/desktop/reconnect")
        async def reconnect(request: Request):
            if request.headers.get("X-Desktop-Token") != self.token:
                raise HTTPException(403, "Invalid desktop session")
            self.check_idle()
            try:
                self.validate(self.profile)
            except (ValueError, ConfigError) as exc:
                raise HTTPException(400, str(exc)) from exc
            app.state.desktop_restarting = True
            self.pending = self.profile
            self.ready = False
            self.server.should_exit = True
            return {"ok": True, "profile": self.profile, "references_lost": True}

        return app

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        self.server = uvicorn.Server(uvicorn.Config(
            self.make_app(), host="127.0.0.1", port=self.port,
            log_level="warning", timeout_graceful_shutdown=5,
        ))
        task = asyncio.create_task(self.server.serve())
        while not self.server.started and not task.done():
            await asyncio.sleep(.05)
        if self.server.started:
            temp = self.prefs.with_suffix(".tmp")
            temp.write_text(json.dumps({"profile": self.profile}), encoding="utf-8")
            os.replace(temp, self.prefs)
            self.generation += 1
            self.ready = True
        else:
            self.error = "Не удалось запустить FlowEngine. Подробности в desktop.log."
        await task
        self.ready = False

    def run(self):
        while not self.stopping:
            try:
                asyncio.run(self.serve())
            except BaseException as exc:
                self.error = f"Ошибка запуска: {exc}. Подробности в desktop.log."
                self.ready = False
            if self.pending is None:
                break
            self.profile, self.pending = self.pending, None

    async def stop_idle(self):
        self.check_idle()
        self.app.state.desktop_restarting = True
        self.stopping = True
        self.server.should_exit = True

    def close(self):
        if self.ready and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.stop_idle(), self.loop).result(timeout=3)
        else:
            self.stopping = True
            if self.server:
                self.server.should_exit = True
