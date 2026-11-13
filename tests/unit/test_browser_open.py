import io
from urllib.error import URLError

from flowengine import browser_open


def test_browser_waits_until_api_answers(monkeypatch):
    attempts = []
    opened = []

    def request(url, timeout):
        attempts.append(url)
        if len(attempts) < 3:
            raise URLError("not ready")
        return io.BytesIO(b'{"state":"connected_idle"}')

    monkeypatch.setattr(browser_open, "urlopen", request)
    monkeypatch.setattr(browser_open.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(browser_open.webbrowser, "open", lambda url: opened.append(url) or True)
    url = "http://127.0.0.1:8765/procedures?procedure=demo_sample_to_cuvette"
    assert browser_open.open_when_ready(url)
    assert len(attempts) == 3
    assert all(url == "http://127.0.0.1:8765/api/state" for url in attempts)
    assert opened == [url]


def test_browser_does_not_open_dead_server(monkeypatch):
    opened = []
    monkeypatch.setattr(browser_open.webbrowser, "open", lambda url: opened.append(url))
    assert not browser_open.open_when_ready("http://127.0.0.1:8765/", timeout_s=0)
    assert not opened
