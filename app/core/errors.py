"""Один формат ошибки для всего конвейера, API и интерфейса: код, что случилось и что делать."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "auth_required"
    AUTH_FAILED = "auth_failed"
    DB_UNAVAILABLE = "db_unavailable"
    NOT_FOUND = "not_found"


class ErrorBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    code: ErrorCode
    message_ru: str
    action_ru: str


class AppError(Exception):
    def __init__(self, body: ErrorBody, status: int) -> None:
        super().__init__(body.message_ru)
        self.body = body
        self.status = status
