import json
import time
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from flowengine.desktop_session import DesktopSession
from flowengine.state import State


def test_save_validate_and_busy_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWENGINE_CONFIG_DIR", str(tmp_path))
    session = DesktopSession(tmp_path)
    session.server = SimpleNamespace(should_exit=False)
    app = session.make_app()
    with TestClient(app) as client:
        profile = client.get('/api/config/default-profile').json()
        profile['name'] = 'bench'
        profile['device_map']['axes'][0]['name'] = 'My pump'
        assert client.put('/api/config/profiles/bench', json=profile).status_code == 200
        assert client.get('/api/config/profiles/bench').json()['device_map']['axes'][0]['name'] == 'My pump'
        profile['name'] = 'default'
        assert client.put('/api/config/profiles/default', json=profile).status_code == 400
        token = {'X-Desktop-Token':session.token}
        assert client.post('/api/desktop/apply', json={'name':'bench'}).status_code == 403
        assert client.post('/api/desktop/apply', json={'name':'missing'}, headers=token).status_code == 400
        app.state.ctx.state._state = State.MOVING
        assert client.post('/api/desktop/apply', json={'name':'bench'}, headers=token).status_code == 409
        app.state.ctx.state._state = State.CONNECTED_IDLE
        app.state.ctx.runner._task = SimpleNamespace(done=lambda: False)
        assert client.post('/api/desktop/apply', json={'name':'bench'}, headers=token).status_code == 409
        app.state.ctx.runner._task = None
        assert client.post('/api/desktop/apply', json={'name':'bench'}, headers=token).status_code == 200
        assert session.pending == 'bench'
        assert session.server.should_exit
        assert client.post('/api/motion/home', json={}).status_code == 503


def wait_ready(session, generation):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if session.ready and session.generation >= generation:
            return
        assert not session.error, session.error
        time.sleep(.05)
    raise AssertionError('Engine did not start')


def test_full_restart_and_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWENGINE_CONFIG_DIR', str(tmp_path))
    session = DesktopSession(tmp_path)
    session.thread.start()
    try:
        wait_ready(session, 1)
        with httpx.Client(base_url=session.url, timeout=5, trust_env=False) as client:
            profile = client.get('/api/config/default-profile').json()
            profile['name'] = 'my_bench'
            profile['device_map']['axes'][0]['name'] = 'Saved pump'
            assert client.put('/api/config/profiles/my_bench', json=profile).status_code == 200
            assert client.post('/api/desktop/apply', json={'name':'my_bench'}, headers={'X-Desktop-Token':session.token}).status_code == 200
            wait_ready(session, 2)
            assert client.get('/api/state').json()['profile'] == 'my_bench'
            assert client.get('/api/config').json()['device_map']['axes'][0]['name'] == 'Saved pump'
            assert json.loads(session.prefs.read_text())['profile'] == 'my_bench'
            assert client.post('/api/stop').status_code == 200
            assert client.get('/api/state').json()['state'] == 'errored'
            assert client.post('/api/desktop/reconnect').status_code == 403
            assert client.post('/api/desktop/reconnect', headers={'X-Desktop-Token':session.token}).status_code == 200
            wait_ready(session, 3)
            state = client.get('/api/state').json()
            assert state['state'] == 'connected_idle'
            assert state['profile'] == 'my_bench'
            assert not any(state['homed'].values())
            assert client.get('/api/diagnostics/firmware').status_code == 200
    finally:
        session.close()
        session.thread.join(8)
    assert not session.thread.is_alive()
    reopened = DesktopSession(tmp_path)
    assert reopened.profile == 'my_bench'
    reopened.thread.start()
    try:
        wait_ready(reopened, 1)
        with httpx.Client(base_url=reopened.url, trust_env=False) as client:
            assert client.get('/api/state').json()['profile'] == 'my_bench'
            assert client.get('/api/config').json()['device_map']['axes'][0]['name'] == 'Saved pump'
    finally:
        reopened.close()
        reopened.thread.join(8)


def test_missing_last_profile_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWENGINE_CONFIG_DIR', str(tmp_path))
    (tmp_path / 'desktop.json').write_text('{"profile":"missing"}')
    assert DesktopSession(tmp_path).profile == 'default'
