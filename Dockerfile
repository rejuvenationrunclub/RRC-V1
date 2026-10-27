# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first for layer caching (no dev tools in the runtime image).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY rrc_bot ./rrc_bot
COPY migrations ./migrations
COPY alembic.ini README.md ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN uv sync --frozen --no-dev && chmod +x /entrypoint.sh

RUN useradd --system --uid 10001 --home /app rrc && chown -R rrc /app
USER rrc

ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "rrc_bot"]
