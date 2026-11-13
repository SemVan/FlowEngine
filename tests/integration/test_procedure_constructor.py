from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flowengine.app import create_app
from flowengine.config import AppSettings


@pytest.fixture
def constructor_client(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path / "config"))
    app = create_app(AppSettings(mock=True, log_level="WARNING"))
    with TestClient(app) as client:
        yield client, app.state.ctx.transport


def test_startup_preview_keeps_engineering_requirements_without_commands(constructor_client):
    client, transport = constructor_client
    before = list(transport.commands)
    source = client.get("/api/procedures/startup_draft").json()
    assert source["title"] == "Запуск"
    assert source["draft"] is True
    response = client.post("/api/procedures/startup_draft/preview", json={})
    assert response.status_code == 200, response.text
    report = response.json()
    assert [s["name"] for s in report["steps"] if s["op"] == "home_valve"] == [
        "wash_valve", "sheath_valve", "sample_valve",
    ]
    assert "КС-ПРОКАЧКА" in report["steps"][5]["title"]
    assert any("параллельный" in issue for issue in report["execution_issues"])
    assert any("датчика U" in issue for issue in report["execution_issues"])
    assert transport.commands == before


@pytest.mark.parametrize("mode", ["full", "selected", "nested"])
def test_unfinished_plan_cannot_run_even_with_draft_cleared(constructor_client, mode):
    client, transport = constructor_client
    child = {
        "name": "unfinished",
        "title": "Не настроено",
        "draft": False,
        "steps": [
            {"op": "home", "axes": ["X"]},
            {"op": "require_setup", "title": "Проверка промывки", "message": "Не выбран датчик"},
        ],
    }
    assert client.put("/api/procedures/unfinished", json=child).status_code == 200
    if mode == "nested":
        parent = {"name": "parent", "steps": [{"op": "call", "procedure": "unfinished"}]}
        assert client.put("/api/procedures/parent", json=parent).status_code == 200
        url = "/api/procedures/parent/steps/1/run"
    elif mode == "selected":
        url = "/api/procedures/unfinished/steps/2/run"
    else:
        url = "/api/procedures/unfinished/run"
    before = list(transport.commands)
    response = client.post(url, json={})
    assert response.status_code == 409, response.text
    assert "Не выбран датчик" in response.json()["detail"]
    assert transport.commands == before


def test_step_title_survives_save_preview_and_nested_call(constructor_client):
    client, transport = constructor_client
    child = {
        "name": "named_child",
        "title": "Сообщение",
        "parameters": {"text": {"type": "string", "default": "готово"}},
        "steps": [{"op": "log", "title": "Этап завершён", "message": "${text}"}],
    }
    parent = {"name": "named_parent", "steps": [{"op": "call", "procedure": "named_child"}]}
    assert client.put("/api/procedures/named_child", json=child).status_code == 200
    assert client.put("/api/procedures/named_parent", json=parent).status_code == 200
    before = list(transport.commands)
    preview = client.post("/api/procedures/named_parent/preview", json={})
    assert preview.status_code == 200, preview.text
    assert preview.json()["steps"] == [
        {"op": "log", "title": "Этап завершён", "message": "готово"},
    ]
    entries = client.get("/api/procedures").json()
    assert next(p for p in entries if p["name"] == "named_child")["title"] == "Сообщение"
    assert transport.commands == before
