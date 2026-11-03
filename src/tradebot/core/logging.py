"""Structured JSON logging with rotation plus a helper to redact secrets."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

_JSON = True
_SECRET_KEYS = re.compile(r"(secret|key|token|password|passwd|authorization|api_key)", re.I)
_TELEGRAM_TOKEN = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")


class RedactFilter(logging.Filter):
    """Belt and braces: secrets must never reach a log file, even by accident."""

    def filter(self, record: logging.LogRecord) -> bool:
        for attr, value in list(record.__dict__.items()):
            if isinstance(value, str):
                record.__dict__[attr] = redact_text(value)
        return True


def redact_text(text: str) -> str:
    text = _TELEGRAM_TOKEN.sub("<telegram-token>", text)
    return text


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if _SECRET_KEYS.search(str(key)):
            out[key] = "***" if value else ""
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        else:
            out[key] = value
    return out


def setup_logging(level: str = "INFO", json_logs: bool = True, log_file: str | None = None) -> None:
    global _JSON
    _JSON = json_logs
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}'
    if json_logs:
        formatter = logging.Formatter(fmt)
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(RedactFilter())
    root.addHandler(stream)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        rotating.addFilter(RedactFilter())
        root.addHandler(rotating)

    for noisy in ("ccxt", "urllib3", "websockets", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class JsonMessage:
    """Lazy JSON payload for log messages: `logger.info(JsonMessage({...}))`."""

    __slots__ = ("payload",)

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __str__(self) -> str:
        import json

        try:
            return json.dumps(self.payload, default=str, separators=(",", ":"))
        except (TypeError, ValueError):  # pragma: no cover
            return str(self.payload)
