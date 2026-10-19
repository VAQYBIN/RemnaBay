#!/usr/bin/env bash
# Проверяет файлы развёртывания из deploy/ (решения 0026, 0073): синтаксис вариантов
# прокси (nginx -t, caddy validate) и compose для операторов во всех сочетаниях.
# Нужен только Docker. Запускается в CI и локально:
#
#   scripts/check-deploy.sh
set -euo pipefail

cd "$(dirname "$0")/.."
deploy="$PWD/deploy"
# Версии — те же, что в compose.caddy.yaml и у Nginx панели по документации Remnawave
nginx_image="nginx:1.30.6"
caddy_image="caddy:2.11.7"
domain="shop.example.com"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

step() { printf '\n== %s\n' "$1"; }

# Самоподписанный сертификат: nginx -t читает файлы сертификатов
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=$domain" \
    -keyout "$work/key.pem" -out "$work/cert.pem" 2> /dev/null

step "Nginx панели: proxy/panel-nginx.conf"
docker run --rm \
    -v "$deploy/proxy/panel-nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "$work/cert.pem:/etc/nginx/ssl/remnabay_fullchain.pem:ro" \
    -v "$work/key.pem:/etc/nginx/ssl/remnabay_privkey.key:ro" \
    --entrypoint nginx "$nginx_image" -t

step "Nginx чистого сервера: proxy/nginx.conf"
docker run --rm \
    -v "$deploy/proxy/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "$work/cert.pem:/etc/letsencrypt/live/$domain/fullchain.pem:ro" \
    -v "$work/key.pem:/etc/letsencrypt/live/$domain/privkey.pem:ro" \
    --entrypoint nginx "$nginx_image" -t

step "Caddy панели: proxy/panel-caddy.Caddyfile"
docker run --rm \
    -v "$deploy/proxy/panel-caddy.Caddyfile:/etc/caddy/Caddyfile:ro" \
    "$caddy_image" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

step "Caddy чистого сервера: proxy/Caddyfile"
docker run --rm -e SHOP_DOMAIN="$domain" \
    -v "$deploy/proxy/Caddyfile:/etc/caddy/Caddyfile:ro" \
    "$caddy_image" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

# compose проверяется в копии папки: .env у каждого сочетания свой
compose_check() {
    local name="$1" env="$2"
    step "compose: $name"
    rm -rf "$work/deploy" && cp -r "$deploy" "$work/deploy"
    printf '%s\n' "$env" > "$work/deploy/.env"
    (cd "$work/deploy" && docker compose config --quiet)
}

compose_check "встроенная база" "POSTGRES_PASSWORD=check"
compose_check "сеть панели" \
    "POSTGRES_PASSWORD=check
COMPOSE_FILE=compose.yaml:compose.panel-network.yaml"
compose_check "Caddy в контейнере" \
    "POSTGRES_PASSWORD=check
SHOP_DOMAIN=$domain
COMPOSE_FILE=compose.yaml:compose.caddy.yaml"
compose_check "Nginx сервера" \
    "POSTGRES_PASSWORD=check
COMPOSE_FILE=compose.yaml:compose.nginx.yaml"
compose_check "внешняя база" \
    "DATABASE_URL=postgresql://remnabay:check@db.example.com:5432/remnabay
COMPOSE_FILE=compose.yaml:compose.external-db.yaml"

step "compose: внешняя база не запускает встроенную"
services="$(cd "$work/deploy" && docker compose config --services)"
if grep -qx db <<< "$services"; then
    echo "Встроенная база запускается, хотя задан compose.external-db.yaml" >&2
    exit 1
fi

step "1.1: внешняя база без DATABASE_URL — compose не запускается и называет параметр"
rm -rf "$work/deploy" && cp -r "$deploy" "$work/deploy"
printf '%s\n' "POSTGRES_PASSWORD=check" "COMPOSE_FILE=compose.yaml:compose.external-db.yaml" \
    > "$work/deploy/.env"
if output="$(cd "$work/deploy" && docker compose config --quiet 2>&1)"; then
    echo "compose с внешней базой принял .env без DATABASE_URL" >&2
    exit 1
fi
grep -q "DATABASE_URL" <<< "$output" || { echo "$output" >&2; exit 1; }
echo "$output"

step "1.1: без пароля встроенной базы compose не запускается и называет параметр"
rm -rf "$work/deploy" && cp -r "$deploy" "$work/deploy"
: > "$work/deploy/.env"
if output="$(cd "$work/deploy" && docker compose config --quiet 2>&1)"; then
    echo "compose принял .env без POSTGRES_PASSWORD" >&2
    exit 1
fi
grep -q "POSTGRES_PASSWORD" <<< "$output" || { echo "$output" >&2; exit 1; }
echo "$output"

printf '\nФайлы развёртывания в порядке\n'
