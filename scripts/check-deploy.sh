#!/usr/bin/env bash
# Проверяет файлы развёртывания из deploy/ (решения 0026, 0073): синтаксис вариантов
# прокси (nginx -t, caddy validate), compose каждого варианта установки и то, что
# общая часть трёх compose-файлов не разъехалась. Нужны Docker и python3.
# Запускается в CI и локально:
#
#   scripts/check-deploy.sh
set -euo pipefail

cd "$(dirname "$0")/.."
deploy="$PWD/deploy"
variants=(panel caddy nginx)
# Версии — те же, что в caddy/compose.yaml и у Nginx панели по документации Remnawave
nginx_image="nginx:1.30.6"
caddy_image="caddy:2.11.7"
domain="shop.example.com"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

step() { printf '\n== %s\n' "$1"; }

# Самоподписанный сертификат: nginx -t читает файлы сертификатов
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=$domain" \
    -keyout "$work/key.pem" -out "$work/cert.pem" 2> /dev/null

step "Nginx панели: panel/panel-nginx.conf"
docker run --rm \
    -v "$deploy/panel/panel-nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "$work/cert.pem:/etc/nginx/ssl/remnabay_fullchain.pem:ro" \
    -v "$work/key.pem:/etc/nginx/ssl/remnabay_privkey.key:ro" \
    --entrypoint nginx "$nginx_image" -t

step "Nginx отдельного сервера: nginx/remnabay.conf"
docker run --rm \
    -v "$deploy/nginx/remnabay.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "$work/cert.pem:/etc/letsencrypt/live/$domain/fullchain.pem:ro" \
    -v "$work/key.pem:/etc/letsencrypt/live/$domain/privkey.pem:ro" \
    --entrypoint nginx "$nginx_image" -t

step "Caddy панели: panel/panel-caddy.Caddyfile"
docker run --rm \
    -v "$deploy/panel/panel-caddy.Caddyfile:/etc/caddy/Caddyfile:ro" \
    "$caddy_image" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

step "Caddy отдельного сервера: caddy/Caddyfile"
docker run --rm -e SHOP_DOMAIN="$domain" \
    -v "$deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
    "$caddy_image" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

# Папка варианта, как у оператора: compose.yaml, файлы прокси и свой .env
prepare() {
    local variant="$1" env="$2"
    rm -rf "${work:?}/$variant" && cp -r "$deploy/$variant" "$work/$variant"
    printf '%s\n' "$env" > "$work/$variant/.env"
}

for variant in "${variants[@]}"; do
    step "compose: $variant/compose.yaml"
    prepare "$variant" "POSTGRES_PASSWORD=check
SHOP_DOMAIN=$domain"
    (cd "$work/$variant" && docker compose config --format json) > "$work/$variant.json"

    step "1.1: $variant — без пароля встроенной базы compose не запускается и называет параметр"
    prepare "$variant" "SHOP_DOMAIN=$domain"
    if output="$(cd "$work/$variant" && docker compose config --quiet 2>&1)"; then
        echo "compose принял .env без POSTGRES_PASSWORD" >&2
        exit 1
    fi
    grep -q "POSTGRES_PASSWORD" <<< "$output" || { echo "$output" >&2; exit 1; }
    echo "$output" | head -1

    step "$variant — база на другом сервере: пароль встроенной не нужен"
    prepare "$variant" "DATABASE_URL=postgresql://remnabay:check@db.example.com:5432/remnabay
SHOP_DOMAIN=$domain"
    (cd "$work/$variant" && docker compose config --quiet)
done

step "Общая часть трёх compose-файлов одинакова"
python3 - "$work" <<'EOF'
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])


def common(variant: str) -> dict:
    """Конфиг варианта без того, чем варианты различаются по замыслу."""
    config = json.loads((work / f"{variant}.json").read_text())
    services = config["services"]
    services.pop("caddy", None)
    for name in ("web", "worker"):
        services[name].pop("networks", None)
        services[name].pop("ports", None)
    for key in ("networks", "volumes"):
        config.pop(key, None)
    return config


reference = common("panel")
for variant in ("caddy", "nginx"):
    if common(variant) != reference:
        sys.exit(f"{variant}/compose.yaml расходится с panel/compose.yaml в общей части")
print("Совпадает")
EOF

printf '\nФайлы развёртывания в порядке\n'
