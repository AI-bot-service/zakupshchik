"""LLM за интерфейсом: шлюз OmniRoute (OpenAI-совместимый) в работе, фейк в тестах.

Повтор с паузой только на сеть, 429 и 5xx; фолбэк между провайдерами делает шлюз (комбо моделей).
Стоимость — из usage.cost ответа шлюза (Polza считает в ₽); нет поля → 0 и пометка в журнале.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Protocol, cast

import openai
from openai import OpenAI
from pydantic import BaseModel, ConfigDict
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

Message = dict[str, Any]


class ToolCall(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: str
    name: str
    arguments: str


class LLMReply(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    content: str | None
    tool_calls: list[ToolCall]
    model: str
    tokens_in: int
    tokens_out: int
    cost_rub: Decimal
    cost_known: bool
    ms: int

    def as_message(self) -> Message:
        msg: Message = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in self.tool_calls
            ]
        return msg


class LLM(Protocol):
    def chat(
        self,
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4000,
    ) -> LLMReply: ...


_RETRYABLE = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


class GatewayLLM:
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 120) -> None:
        if not api_key:
            raise ValueError("LLM_API_KEY не задан — агент не запускается")
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0)

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
    def chat(
        self,
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4000,
    ) -> LLMReply:
        started = time.monotonic()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        if tools:
            kwargs["tools"] = tools
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        usage = resp.usage
        raw_cost = getattr(usage, "cost", None) if usage is not None else None
        calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
            for c in cast(list[Any], choice.message.tool_calls or [])
            if getattr(c, "function", None) is not None
        ]
        return LLMReply(
            content=choice.message.content,
            tool_calls=calls,
            model=resp.model or model,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            cost_rub=Decimal(str(raw_cost)) if raw_cost is not None else Decimal(0),
            cost_known=raw_cost is not None,
            ms=int((time.monotonic() - started) * 1000),
        )
