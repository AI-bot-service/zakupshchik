"""Письма через Yandex Cloud Postbox по HTTPS (SES v2, подпись SigV4), формат Raw — чтобы были вложения.

SMTP не используем: у хостеров исходящие 25/465/587 часто закрыты. Повтор с паузой — на сеть, 429 и 5xx.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Protocol

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

HOST = "postbox.cloud.yandex.net"
PATH = "/v2/email/outbound-emails"
REGION = "ru-central1"
SERVICE = "ses"


@dataclass(frozen=True)
class Attachment:
    name: str
    data: bytes
    mime: str = "application/octet-stream"


class MailError(RuntimeError):
    pass


class Mailer(Protocol):
    def send(
        self, to: str, subject: str, text: str, attachments: list[Attachment], html: str | None = None
    ) -> str: ...


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign_v4(body: bytes, key_id: str, secret: str, now: datetime) -> dict[str, str]:
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    canonical = "\n".join(
        [
            "POST",
            PATH,
            "",
            f"host:{HOST}",
            f"x-amz-date:{amz_date}",
            "",
            "host;x-amz-date",
            hashlib.sha256(body).hexdigest(),
        ]
    )
    scope = f"{day}/{REGION}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret}".encode(), day), REGION), SERVICE), "aws4_request")
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Amz-Date": amz_date,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, "
            f"SignedHeaders=host;x-amz-date, Signature={signature}"
        ),
    }


def build_mime(
    sender: str, to: str, subject: str, text: str, attachments: list[Attachment], html: str | None = None
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    msg.set_content(text, charset="utf-8")
    if html is not None:  # почтовик без HTML покажет текстовую версию
        msg.add_alternative(html, subtype="html", charset="utf-8")
    for a in attachments:
        main, _, sub = a.mime.partition("/")
        msg.add_attachment(a.data, maintype=main, subtype=sub or "octet-stream", filename=a.name)
    return msg.as_bytes()


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 500, 502, 503, 504)


class PostboxMailer:
    def __init__(self, key_id: str, secret: str, sender: str, timeout_s: float = 30) -> None:
        if not (key_id and secret and sender):
            raise MailError("Postbox не настроен: POSTBOX_ACCESS_KEY_ID, POSTBOX_SECRET_KEY, POSTBOX_FROM")
        self._key_id, self._secret, self._sender = key_id, secret, sender
        self._client = httpx.Client(timeout=timeout_s)

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=20),
        reraise=True,
    )
    def _post(self, body: bytes) -> httpx.Response:
        headers = sign_v4(body, self._key_id, self._secret, datetime.now(UTC))
        headers["Content-Type"] = "application/json"
        resp = self._client.post(f"https://{HOST}{PATH}", content=body, headers=headers)
        resp.raise_for_status()
        return resp

    def send(
        self, to: str, subject: str, text: str, attachments: list[Attachment], html: str | None = None
    ) -> str:
        raw = build_mime(self._sender, to, subject, text, attachments, html)
        payload = {
            "FromEmailAddress": self._sender,
            "Destination": {"ToAddresses": [to]},
            "Content": {"Raw": {"Data": base64.b64encode(raw).decode()}},
        }
        try:
            resp = self._post(json.dumps(payload).encode())
        except httpx.HTTPStatusError as exc:
            raise MailError(f"Postbox ответил {exc.response.status_code}") from exc
        except httpx.TransportError as exc:
            raise MailError(f"Postbox недоступен ({type(exc).__name__})") from exc
        message_id: str = resp.json().get("MessageId", "")
        return message_id
