# Установка RemnaBay

RemnaBay — Telegram-бот и веб-админка магазина VPN для панели [Remnawave](https://docs.rw). Здесь — как поставить магазин на сервер, обновлять его и делать резервные копии.

Магазин работает в Docker: база PostgreSQL, шаг миграций, «веб» (админка, вебхуки, страница-переходник) и «воркер» (фоновые задачи). Обратный прокси с HTTPS в магазин не входит: ниже — готовые настройки для Nginx и Caddy.

## Что понадобится

- **Сервер** на Linux, amd64 или arm64, с Docker Engine и Compose 2.24 или новее. Docker ставится одной командой: `curl -fsSL https://get.docker.com | sh`.
- **Домен для магазина**, например `shop.example.com`: A-запись указывает на сервер магазина.
- **Панель Remnawave 3.4.4 или новее в пределах 3.4.** С другой версией пункт «Подключение к панели» в чек-листе админки не выполнится и покажет требуемую версию.
- **Бот в Telegram** — его создаёт @BotFather (шаг 1).

## Выберите вариант установки

| Где магазин | Прокси | Файлы |
|---|---|---|
| На сервере с панелью | Nginx панели | `compose.panel-network.yaml`, `proxy/panel-nginx.conf` |
| На сервере с панелью | Caddy панели | `compose.panel-network.yaml`, `proxy/panel-caddy.Caddyfile` |
| На отдельном сервере | Caddy в контейнере | `compose.caddy.yaml`, `proxy/Caddyfile` |
| На отдельном сервере | Nginx и certbot на сервере | `compose.nginx.yaml`, `proxy/nginx.conf` |

На сервере с панелью магазин подключается к её Docker-сети `remnawave-network`: прокси панели ходит к магазину по внутреннему адресу, а панель шлёт вебхук мимо интернета. Если у вас уже работает свой прокси, возьмите ближайший вариант за образец.

## Шаг 1. Бот в @BotFather

1. В @BotFather создайте бота командой `/newbot` и сохраните токен — это `BOT_TOKEN`.
2. Вход в админку идёт через Telegram. В @BotFather откройте мини-приложение → ваш бот → **Login Widget** → **OpenID Connect** (переключение необратимо) и укажите:
   - Redirect URI: `https://shop.example.com/api/admin/auth/telegram/callback`
   - Trusted Origin: `https://shop.example.com`
   - Signing Algorithm: `RS256`

   Скопируйте **Client Secret** — это `TELEGRAM_LOGIN_CLIENT_SECRET`. Client ID указывать не нужно: это ID бота.
3. Узнайте свой Telegram ID, например у @userinfobot, — это `OWNER_TELEGRAM_ID`, первый владелец магазина.

## Шаг 2. Файлы магазина

Скачайте файлы нужной версии в `/opt/remnabay`. Номер версии — в [CHANGELOG.md](../CHANGELOG.md).

```bash
VERSION=0.1.0
mkdir -p /opt/remnabay/proxy && cd /opt/remnabay
base="https://raw.githubusercontent.com/VAQYBIN/RemnaBay/v$VERSION/deploy"
for file in compose.yaml compose.panel-network.yaml compose.caddy.yaml compose.nginx.yaml \
    compose.external-db.yaml .env.example \
    proxy/panel-nginx.conf proxy/panel-caddy.Caddyfile proxy/Caddyfile proxy/nginx.conf; do
  curl -fsSL -o "$file" "$base/$file"
done
cp .env.example .env
```

## Шаг 3. Параметры в `.env`

Откройте `.env` (`nano .env`) и заполните его — у каждого параметра там есть пояснение:

- **`COMPOSE_FILE`** — уберите `#` у строки своего варианта установки;
- **`BOT_TOKEN`, `TELEGRAM_LOGIN_CLIENT_SECRET`, `OWNER_TELEGRAM_ID`** — из шага 1;
- **`PANEL_URL`** — на сервере с панелью `http://remnawave:3000`, на отдельном сервере — публичный адрес панели;
- **`PANEL_TOKEN`** — в панели: **API Tokens** → создать токен;
- **`PUBLIC_URL`** — `https://shop.example.com`;
- **`SHOP_DOMAIN`** — только для Caddy в контейнере: `shop.example.com`, без `https://`.

Пароль встроенной базы и ключ шифрования создайте командами — они сами запишут значения в `.env`:

```bash
cd /opt/remnabay
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 64)/" .env
sed -i "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=$(docker compose run --rm --no-deps -T migrate generate-key)|" .env
```

Сохраните копию `.env` в надёжном месте. Без `ENCRYPTION_KEY` ключи платёжных провайдеров, введённые в админке, не расшифровать — их придётся ввести заново.

> Пароль базы PostgreSQL берёт только при первом запуске. Если потом поменять `POSTGRES_PASSWORD` в `.env`, пароль базы не изменится, и магазин не сможет к ней подключиться. Как сменить пароль — в разделе [«Смена пароля базы»](#смена-пароля-базы).

Если какой-то обязательный параметр не задан, магазин не запустится и назовёт его: без `POSTGRES_PASSWORD` об этом скажет сама команда `docker compose`, остальные параметры назовёт магазин в логе (`docker compose logs web`).

## Шаг 4. Прокси

Выполните раздел своего варианта. Везде замените `shop.example.com` на домен магазина.

### Сервер с панелью, Nginx панели

Здесь предполагается Nginx из документации Remnawave: контейнер `remnawave-nginx`, файлы в `/opt/remnawave/nginx`, сертификаты от acme.sh.

1. Выпустите сертификат для домена магазина так же, как для панели:

   ```bash
   acme.sh --issue --standalone -d 'shop.example.com' \
     --key-file /opt/remnawave/nginx/remnabay_privkey.key \
     --fullchain-file /opt/remnawave/nginx/remnabay_fullchain.pem \
     --alpn --tlsport 8443 --reloadcmd "docker exec remnawave-nginx nginx -s reload"
   ```

2. В `/opt/remnawave/nginx/docker-compose.yml` добавьте файлы сертификата в `volumes` контейнера `remnawave-nginx`, а в `ports` — порт 80 для перенаправления с HTTP на HTTPS:

   ```yaml
       volumes:
           # ...что уже было
           - ./remnabay_fullchain.pem:/etc/nginx/ssl/remnabay_fullchain.pem:ro
           - ./remnabay_privkey.key:/etc/nginx/ssl/remnabay_privkey.key:ro
       ports:
           - '0.0.0.0:443:443'
           - '0.0.0.0:80:80'
   ```

3. Допишите блоки магазина в конец `nginx.conf` панели и перезапустите Nginx:

   ```bash
   sed 's/shop.example.com/ВАШ_ДОМЕН/g' /opt/remnabay/proxy/panel-nginx.conf >> /opt/remnawave/nginx/nginx.conf
   cd /opt/remnawave/nginx && docker compose up -d --force-recreate
   docker exec remnawave-nginx nginx -t
   ```

   Nginx ищет адрес магазина в момент запроса: пока магазин не запущен, панель работает как обычно, а магазин отвечает 502.

### Сервер с панелью, Caddy панели

Здесь предполагается Caddy из документации Remnawave: контейнер `caddy`, файлы в `/opt/remnawave/caddy`. Сертификат и перенаправление с HTTP на HTTPS Caddy делает сам.

```bash
sed 's/shop.example.com/ВАШ_ДОМЕН/g' /opt/remnabay/proxy/panel-caddy.Caddyfile >> /opt/remnawave/caddy/Caddyfile
cd /opt/remnawave/caddy && docker compose restart
```

### Отдельный сервер, Caddy в контейнере

Ничего настраивать не нужно: Caddy запустится вместе с магазином (`compose.caddy.yaml`) и сам получит сертификат для `SHOP_DOMAIN`. Порты 80 и 443 сервера должны быть свободны и открыты.

### Отдельный сервер, Nginx и certbot

```bash
apt install -y nginx certbot
# Сертификат: проверку certbot отдаёт Nginx из /var/www/html
certbot certonly --webroot -w /var/www/html -d shop.example.com \
  --deploy-hook "systemctl reload nginx"
sed 's/shop.example.com/ВАШ_ДОМЕН/g' /opt/remnabay/proxy/nginx.conf > /etc/nginx/conf.d/remnabay.conf
nginx -t && systemctl reload nginx
certbot renew --dry-run   # продление будет работать
```

Порты 80 и 443 сервера должны быть открыты. Магазин открывает веб только на `127.0.0.1:8000` (`compose.nginx.yaml`) — снаружи к нему не попасть, только через Nginx.

## Шаг 5. Запуск

```bash
cd /opt/remnabay
docker compose up -d
docker compose ps
```

Через минуту `migrate` — `Exited (0)`, `db`, `web` и `worker` — `Up … (healthy)`. Проверка снаружи:

```bash
curl https://shop.example.com/health    # {"status":"ok"}
```

Откройте `https://shop.example.com/admin/` и войдите через Telegram аккаунтом `OWNER_TELEGRAM_ID`. Дальше ведёт чек-лист на главной админки.

## Шаг 6. Вебхук панели

Панель сообщает магазину об изменениях пользователей вебхуком. Без него магазин узнаёт о них сверкой — раз в 15 минут.

1. В админке откройте **Настройки → Панель** (или пункт «Вебхук панели» в чек-листе) и скопируйте адрес и секрет вебхука. На сервере с панелью вместо адреса из админки можно указать внутренний — ниже.
2. В `/opt/remnawave/.env` панели задайте:

   ```bash
   WEBHOOK_ENABLED=true
   # на сервере с панелью — по внутренней сети:
   WEBHOOK_URL=http://remnabay:8000/webhooks/panel
   # на отдельном сервере — адрес из админки:
   # WEBHOOK_URL=https://shop.example.com/webhooks/panel
   WEBHOOK_SECRET_HEADER=секрет из админки
   ```

   Если панель ещё шлёт события прежнему боту, адреса можно перечислить через запятую. Секрет у них общий: в админке можно указать секрет, который уже задан в панели.
3. Пересоздайте контейнеры панели — простого перезапуска мало, панель читает `.env` только при создании контейнера:

   ```bash
   cd /opt/remnawave && docker compose down && docker compose up -d
   ```

Когда первое событие от панели дойдёт, пункт «Вебхук панели» в чек-листе отметится сам.

## Subscription Page

Если у вас стоит Subscription Page, учтите два её свойства:

- **Переменная `SUBPAGE_CONFIG_UUID`** в Subscription Page показывает всем клиентам на веб-странице подписки один и тот же конфиг. Бот её не видит и берёт конфиг по внешнему скваду клиента — инструкции в боте и на странице разойдутся.
- **Subscription Page читает конфиги только при запуске.** После правки конфига в панели бот обновится сам (раз в час или кнопкой «Обновить сейчас» в админке), а веб-страница — только после перезапуска Subscription Page.

## Резервная копия

Резервные копии — ваша ответственность. Делайте копию регулярно и обязательно перед каждым обновлением.

```bash
cd /opt/remnabay
docker compose exec -T db pg_dump -U remnabay -d remnabay --format=custom > remnabay-$(date +%F).dump
```

Храните копии не только на этом сервере. Вместе с ними держите копию `.env`: без `ENCRYPTION_KEY` из него сохранённые ключи провайдеров не расшифровать.

Восстановление из копии:

```bash
cd /opt/remnabay
docker compose stop web worker
docker compose exec -T db pg_restore -U remnabay -d remnabay --clean --if-exists < remnabay-ДАТА.dump
docker compose up -d
```

## Обновление

1. Прочитайте в [CHANGELOG.md](../CHANGELOG.md), что изменилось.
2. Сделайте [резервную копию](#резервная-копия).
3. Скачайте файлы новой версии командой из [шага 2](#шаг-2-файлы-магазина) с новым `VERSION`, **без последней строки `cp .env.example .env`** — ваш `.env` остаётся. Сравните `.env` с новым `.env.example`: если появились новые параметры, допишите их.
4. Скачайте образ и перезапустите магазин:

   ```bash
   cd /opt/remnabay
   docker compose pull
   docker compose up -d
   ```

Миграции базы применятся сами перед запуском веба и воркера. Во время перезапуска магазин недоступен несколько секунд — ничего не теряется: Telegram и платёжные провайдеры повторят уведомления, а пропущенное от панели найдёт сверка.

### Предварительная версия

Предварительные версии (`0.1.0-rc.1` и т. п.) — для проверки на тестовом сервере, не для рабочего магазина. Чтобы поставить такую версию, скачайте файлы с `VERSION=0.1.0-rc.1` и задайте в `.env` `REMNABAY_VERSION=0.1.0-rc.1`. Чтобы вернуться на обычные версии, уберите `REMNABAY_VERSION` из `.env`.

## Смена пароля базы

PostgreSQL берёт `POSTGRES_PASSWORD` только при первом запуске базы, поэтому пароль меняется в самой базе, а потом в `.env`:

```bash
cd /opt/remnabay
NEW_PASSWORD=$(openssl rand -hex 64)
docker compose exec -T db psql -U remnabay -d remnabay -c "ALTER USER remnabay WITH PASSWORD '$NEW_PASSWORD'"
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$NEW_PASSWORD/" .env
docker compose up -d
```

## База на другом сервере

Для магазина нужна отдельная база PostgreSQL 18 — не база панели. В `.env`:

- допишите `:compose.external-db.yaml` в конец строки `COMPOSE_FILE`;
- задайте `DATABASE_URL=postgresql://пользователь:пароль@хост:5432/база`;
- `POSTGRES_PASSWORD` не нужен.

Встроенная база тогда не запускается. Резервные копии внешней базы делайте средствами её сервера.

## Если что-то не так

- **Состояние:** `docker compose ps`. Веб, воркер и база перезапускаются сами после падения и после перезагрузки сервера, но не после того, как вы остановили их сами (`docker compose stop`).
- **Логи:** `docker compose logs -f web worker`.
- **Веб или воркер то и дело перезапускаются.** Процесс, который потерял работоспособность (например, 3 минуты не может связаться с базой), сам завершается, и Docker поднимает его заново. Причина — в логе перед завершением, строка с `CRITICAL`.
- **`unhealthy` в `docker compose ps`** — процесс завис и не может завершиться сам. Перезапустите его: `docker compose restart web` (или `worker`) — и посмотрите лог.
