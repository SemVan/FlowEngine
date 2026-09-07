"""structlog configuration + JSONL audit sink.

Audit sink writes to `$XDG_STATE_HOME/flowengine/audit.jsonl` (or
`~/.local/state/flowengine/audit.jsonl` if XDG isn't set). Every operator action
and every G-code line sent lands there. Cheap, invaluable post-mortem.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from platformdirs import user_state_dir


def audit_log_path() -> Path:
    p = Path(user_state_dir("flowengine", appauthor=False)) / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class _JsonlAuditHandler(logging.Handler):
    """Writes every record with the `audit=True` extra to the audit JSONL."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path

    def emit(self, record: logging.LogRecord) -> None:
        if not getattr(record, "audit", False):
            return
        try:
            payload: dict[str, Any] = {
                "t": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
            }
            for k, v in record.__dict__.items():
                if k in (
                    "args",
                    "msg",
                    "levelname",
                    "levelno",
                    "name",
                    "pathname",
                    "filename",
                    "module",
                    "exc_info",
                    "exc_text",
                    "stack_info",
                    "lineno",
                    "funcName",
                    "created",
                    "msecs",
                    "relativeCreated",
                    "thread",
                    "threadName",
                    "processName",
                    "process",
                    "audit",
                    "message",
                    "asctime",
                ):
                    continue
                payload[k] = v
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, default=str) + "\n")
        except Exception:  # noqa: BLE001
            # Never let the audit sink kill the main process.
            pass


def configure_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.setLevel(log_level)
    if not any(isinstance(h, _JsonlAuditHandler) for h in root.handlers):
        root.addHandler(_JsonlAuditHandler(audit_log_path()))


def audit(event: str, **fields: Any) -> None:
    """Emit one audit record. Cheap; safe to call frequently."""
    logging.getLogger("flowengine.audit").info(event, extra={"audit": True, **fields})
