"""Настройки из окружения. Секреты — только SecretStr, в repr и журнал не попадают."""

from __future__ import annotations

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # hide_input_in_errors: при ошибке валидации значение секрета не попадает в журнал
    model_config = SettingsConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)

    domain: str = "localhost"
    debug: bool = False
    app_secret_key: SecretStr = Field(min_length=32)

    # Рабочая роль без прав владельца: только DML. Миграции идут отдельным контейнером под DATABASE_OWNER_URL.
    database_url: PostgresDsn

    llm_base_url: str = "http://omniroute:20128/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model_main: str = "zakupshchik-main"
    llm_model_cheap: str = "zakupshchik-cheap"

    proxy_url: str | None = None

    jury_login: str = "jury"
    # Пустой пароль = вход закрыт, а не открыт.
    jury_password: SecretStr = SecretStr("")

    @property
    def https_only(self) -> bool:
        return not self.debug
