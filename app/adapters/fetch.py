"""Запросы к площадкам: российский выход (PROXY_URL), лимит частоты на площадку, повтор с паузой."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.adapters.config import PlatformConfig

# Честный User-Agent (§19): площадка видит, что это робот, и где о нём прочитать
USER_AGENT = "Mozilla/5.0 (compatible; Zakupshchik/0.1; +https://zakupki-robot.systemtool.ru/status)"


def _fill(value: Any, query: str) -> Any:
    if isinstance(value, str):
        return value.replace("{query}", query)
    if isinstance(value, list):
        return [_fill(v, query) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, query) for k, v in value.items()}
    return value


def build_params(cfg: PlatformConfig, query: str) -> dict[str, str | list[str]]:
    params: dict[str, str | list[str]] = _fill(dict(cfg.params), query)
    for name, value in cfg.json_params.items():
        params[name] = json.dumps(_fill(value, query), ensure_ascii=False, separators=(",", ":"))
    return params


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 500, 502, 503, 504)


class Fetcher:
    """Один клиент на цикл сбора. Пауза между запросами к одной площадке — не меньше min_interval_s."""

    def __init__(
        self,
        proxy_url: str | None,
        timeout_s: float = 45,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            proxy=proxy_url or None,
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
        )
        self._last: dict[str, float] = {}
        self._clock = clock
        self._sleep = sleep

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def _wait_turn(self, cfg: PlatformConfig) -> None:
        last = self._last.get(cfg.code)
        if last is not None:
            gap = cfg.min_interval_s - (self._clock() - last)
            if gap > 0:
                self._sleep(gap)
        self._last[cfg.code] = self._clock()

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=3, max=20),
        reraise=True,
    )
    def get(self, cfg: PlatformConfig, query: str) -> str:
        self._wait_turn(cfg)
        resp = self._client.get(cfg.url, params=build_params(cfg, query))
        resp.raise_for_status()
        return resp.text
