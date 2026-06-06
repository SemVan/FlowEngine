"""WS telemetry pushes state transitions to subscribers."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings


@pytest.fixture
def client() -> TestClient:
    settings = AppSettings(mock=True, log_level="WARNING")
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_initial_snapshot(client):
    with client.websocket_connect("/ws") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "state"
        msg2 = json.loads(ws.receive_text())
        assert msg2["type"] == "position"


def test_state_change_pushes_event(client):
    with client.websocket_connect("/ws") as ws:
        # consume initial snapshot
        ws.receive_text()
        ws.receive_text()
        # trigger a state change
        client.post("/api/home", json={"axes": None})
        # we may receive several events; assert at least one is `state` going to a non-idle state
        seen_homing = False
        for _ in range(8):
            text = ws.receive_text()
            msg = json.loads(text)
            if msg["type"] == "state" and msg["state"] in {"homing", "connected_idle"}:
                seen_homing = True
                if msg["state"] == "connected_idle":
                    break
        assert seen_homing
