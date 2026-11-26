"""Структурный журнал JSON. Значения ключей с секретами вырезаются до вывода."""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog

_SECRET_PARTS = ("password", "token", "secret", "key", "authorization", "cookie")


def _drop_secrets(_: object, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for name in list(event):
        if any(part in name.lower() for part in _SECRET_PARTS):
            event[name] = "***"
    return event


def configure_logging(debug: bool) -> None:
    logging.basicConfig(format="%(message)s", level=logging.DEBUG if debug else logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _drop_secrets,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG if debug else logging.INFO),
        cache_logger_on_first_use=True,
    )
