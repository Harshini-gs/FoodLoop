"""Structured logging. Never logs passwords, tokens, or handover codes.

`REDACT_KEYS` is applied to any dict passed as `extra={"context": {...}}`.
"""

import json
import logging
import sys
from typing import Any

from app.core.settings import get_settings

REDACT_KEYS = frozenset(
    {
        "password",
        "plain",
        "token",
        "access_token",
        "jwt_secret",
        "authorization",
        "api_key",
        "llm_api_key",
        "handover_code",
        "code",
        "secret",
    }
)


def redact(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in REDACT_KEYS:
            out[key] = "***"
        elif isinstance(value, dict):
            out[key] = redact(value)
        else:
            out[key] = value
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload["context"] = redact(context)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<7} {record.name:<28} {record.getMessage()}"
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            pairs = " ".join(f"{k}={v}" for k, v in redact(context).items())
            base = f"{base}  [{pairs}]"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging() -> None:
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if settings.log_format == "json" else ConsoleFormatter()
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # SQLAlchemy is chatty at INFO when echo is on; keep it at WARNING otherwise.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
