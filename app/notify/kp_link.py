"""Ссылка «Подготовить КП» в письме по сделке: без входа на стенд, подписана APP_SECRET_KEY.

Подпись привязана к записи notification (сделка + адресат). КП робот кладёт в карточку сделки на стенде.
"""

from __future__ import annotations

import hashlib
import hmac


def kp_token(secret: str, notification_id: int) -> str:
    return hmac.new(secret.encode(), f"kp-mail:{notification_id}".encode(), hashlib.sha256).hexdigest()[:32]


def kp_url(public_url: str, secret: str, notification_id: int) -> str:
    return f"{public_url}/mail/kp/{notification_id}/{kp_token(secret, notification_id)}"


def kp_token_ok(secret: str, notification_id: int, token: str) -> bool:
    return hmac.compare_digest(kp_token(secret, notification_id), token)
