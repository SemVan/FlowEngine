"""открываем страницу, когда сервер уже отвечает"""
from __future__ import annotations

import argparse
import json
import time
import webbrowser
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen


def open_when_ready(url: str, timeout_s: float = 60) -> bool:
    target = urlsplit(url)
    status_url = f"{target.scheme}://{target.netloc}/api/state"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urlopen(status_url, timeout=2) as response:
                state = json.load(response)
            if isinstance(state, dict) and "state" in state:
                return webbrowser.open(url)
        except (URLError, OSError, ValueError):
            pass
        time.sleep(.25)
    print(f"Сервер ещё не отвечает. После запуска откройте {url}")
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/")
    args = parser.parse_args()
    open_when_ready(args.url)


if __name__ == "__main__":
    main()
