"""Запросы к площадкам: российский выход (PROXY_URL), лимит частоты на площадку, повтор с паузой."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from socksio import SOCKSError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.adapters.config import PlatformConfig

# Честный User-Agent (§19): площадка видит, что это робот, и где о нём прочитать
USER_AGENT = "Mozilla/5.0 (compatible; Zakupshchik/0.1; +https://zakupki-robot.systemtool.ru/status)"


class FileTooLargeError(ValueError):
    pass


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


class FlakyStatusError(httpx.HTTPStatusError):
    """Код ответа, который у этой площадки бывает разовым (retry_status в platforms.yaml): повторяем."""


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError | FlakyStatusError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 500, 502, 503, 504)


@contextmanager
def _socks_as_transport() -> Iterator[None]:
    """Битый ответ SOCKS-прокси (площадка недоступна с выхода) httpx пропускает как socksio-ошибку.
    Делаем её сетевой, иначе одна площадка роняет весь цикл сбора."""
    try:
        yield
    except SOCKSError as exc:
        raise httpx.ProxyError(f"SOCKS: {exc}") from exc


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

    def get(self, cfg: PlatformConfig, query: str) -> str:
        """Страница поиска площадки по слову."""
        return self.get_url(cfg, cfg.url, build_params(cfg, query))

    def get_url(self, cfg: PlatformConfig, url: str, params: dict[str, str | list[str]] | None = None) -> str:
        return self._get(cfg, url, params).text

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=3, max=20),
        reraise=True,
    )
    def get_bytes(self, cfg: PlatformConfig, url: str, max_bytes: int) -> bytes:
        """Вложение закупки. Больше max_bytes — отказ, файл целиком в память не берём."""
        if not url.startswith("https://"):
            raise ValueError("площадки опрашиваем только по https")
        self._wait_turn(cfg)
        with _socks_as_transport(), self._client.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise FileTooLargeError(f"файл больше {max_bytes // 1_000_000} МБ")
                chunks.append(chunk)
        return b"".join(chunks)

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=3, max=20),
        reraise=True,
    )
    def _get(
        self, cfg: PlatformConfig, url: str, params: dict[str, str | list[str]] | None
    ) -> httpx.Response:
        if not url.startswith("https://"):
            raise ValueError("площадки опрашиваем только по https")
        self._wait_turn(cfg)
        with _socks_as_transport():
            resp = self._client.get(url, params=params)
        if resp.status_code in cfg.retry_status:
            raise FlakyStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
        resp.raise_for_status()
        return resp
