from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .storage import ensure_private_dir


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(state_dir: Path, verbose: bool = False) -> None:
    ensure_private_dir(state_dir)
    handler = RotatingFileHandler(state_dir / "service.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("agentic_web_team")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
