# syntax=docker/dockerfile:1
# Один образ, две роли: «веб» и «воркер»; роль задаётся командой запуска (0026).
# Версии базовых образов закреплены точными тегами (0027).

# Сборка админки: Node нужен только здесь, в итоговом образе его нет (0025)
FROM node:24.21.0-trixie-slim AS admin
WORKDIR /admin
COPY admin/package.json admin/package-lock.json ./
RUN npm ci
COPY admin/ ./
RUN npm run build

FROM python:3.14.8-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_DEV=1

RUN groupadd --system --gid 999 remnabay \
 && useradd --system --gid 999 --uid 999 --create-home remnabay

WORKDIR /app

# Сначала зависимости строго по uv.lock — этот слой кэшируется, пока lock не изменился
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

COPY --from=admin /admin/dist ./admin/dist

ENV PATH="/app/.venv/bin:$PATH"
USER remnabay
EXPOSE 8000

ENTRYPOINT ["remnabay"]
CMD ["web"]
