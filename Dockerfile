# syntax=docker/dockerfile:1
# Версии: python 3.13.18 / alpine 3.24 (trivy 24.11: 0 CRITICAL), uv 0.13.10 (OSV чисто 26.11).
FROM ghcr.io/astral-sh/uv:0.13.10 AS uv

FROM python:3.13.18-alpine3.24 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/app/.venv PATH="/app/.venv/bin:$PATH"
RUN apk upgrade --no-cache \
 && addgroup -S -g 10001 app && adduser -S -D -H -u 10001 -G app -h /app app
WORKDIR /app

# ── зависимости рантайма ────────────────────────────────────────────────────
FROM base AS deps
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ── тесты и линтеры на том же интерпретаторе ───────────────────────────────
FROM deps AS test
RUN uv sync --frozen --no-install-project
COPY . .
CMD ["sh", "-c", "ruff check . && mypy && pytest"]

# ── рантайм: без uv и pip ──────────────────────────────────────────────────
FROM base AS runtime
RUN python -m pip uninstall -y pip setuptools wheel 2>/dev/null || true
COPY --from=deps /app/.venv /app/.venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app
COPY --chown=app:app config ./config
COPY --chown=app:app prompts ./prompts
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"
# Порт наружу не публикуется, запросы приходят только от Traefik — X-Forwarded-* доверяем
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]

# ── воркер: + LibreOffice Writer для КП в PDF (конвертирует только наши DOCX, документы площадок не открывает).
# Carlito — метрический аналог Calibri из шаблона КП, вёрстка не съезжает. Профиль LibreOffice — в /tmp (rootfs read-only)
FROM runtime AS worker
USER root
RUN apk add --no-cache libreoffice-writer font-carlito font-liberation font-dejavu
ENV HOME=/tmp
USER app
CMD ["python", "-m", "app.worker"]
