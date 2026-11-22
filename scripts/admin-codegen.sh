#!/usr/bin/env bash
# Файлы админки, которые берутся из бэкенда (решения 0050, 0051):
# - admin/src/api/schema.d.ts — типы клиента из схемы API;
# - admin/src/styles/default-brand.css — токены бренда RemnaBay по умолчанию.
#
# Схема берётся командой `remnabay openapi` — без .env и базы. Генератор живёт в
# отдельном пакете admin/tools/openapi-typescript: ему нужен API компилятора
# TypeScript 5, а у TypeScript 7 админки такого API нет.
# Оба файла лежат в репозитории; CI проверяет, что они свежие.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
tool="$root/admin/tools/openapi-typescript"
schema="$(mktemp --suffix=.json)"
trap 'rm -f "$schema"' EXIT

uv run --project "$root" --quiet remnabay openapi > "$schema"
if [[ ! -x "$tool/node_modules/.bin/openapi-typescript" ]]; then
  npm ci --prefix "$tool" --no-audit --no-fund --silent
fi
"$tool/node_modules/.bin/openapi-typescript" "$schema" -o "$root/admin/src/api/schema.d.ts"

uv run --project "$root" --quiet remnabay default-brand-css > "$root/admin/src/styles/default-brand.css"
