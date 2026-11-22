#!/usr/bin/env bash
# Достаёт openapi.json панели Remnawave из образа remnawave/backend:<версия> для
# контрактного теста клиента панели (решения 0027, 0042).
#
# Файл не хранится в репозитории: лицензия бэкенда панели — AGPL-3.0.
#
#   scripts/fetch-panel-openapi.sh            # все поддерживаемые версии
#   scripts/fetch-panel-openapi.sh 3.4.4      # конкретная версия
#
# Куда кладётся файл: $PANEL_OPENAPI_DIR (по умолчанию .cache/panel-openapi).
set -euo pipefail

cd "$(dirname "$0")/.."
out_dir="${PANEL_OPENAPI_DIR:-.cache/panel-openapi}"

versions=("$@")
if [ ${#versions[@]} -eq 0 ]; then
    read -ra versions <<< "$(uv run python -c \
        'from remnabay.panel import SUPPORTED_PANEL_VERSIONS as v; print(*v)')"
fi

mkdir -p "$out_dir"
for version in "${versions[@]}"; do
    target="$out_dir/$version.json"
    if [ -s "$target" ]; then
        echo "Уже есть: $target"
        continue
    fi
    image="remnawave/backend:$version"
    docker pull --quiet "$image" > /dev/null
    container="$(docker create "$image")"
    docker cp "$container:/opt/app/openapi.json" "$target"
    docker rm "$container" > /dev/null
    echo "Получен: $target"
done
