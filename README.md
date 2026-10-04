# RemnaBay

Простой и надёжный магазин VPN для Remnawave: Telegram-бот и веб-админка, на которые не страшно переехать и которые не страшно обновлять.

Remnawave — волна. Bay — бухта, куда волна спокойно приходит к берегу. RemnaBay — тихая гавань для небольшого VPN-бизнеса: переезд без потери клиентов, настройка в админке, а не в конфигах, и гарантия для клиента: деньги пришли — услуга будет.

> RemnaBay — неофициальный проект сообщества. Он не связан с командой Remnawave.

> **Статус: реализация MVP.** Спецификация — в [`docs/`](docs/README.md), порядок работы — [план разработки](docs/plan.md).

## Разработка
Нужны [uv](https://docs.astral.sh/uv/), Docker с Compose и Node.js 24 LTS (только для админки).

```bash
cp .env.example .env                         # заполнить параметры
docker compose -f compose.dev.yaml up --build  # PostgreSQL, миграции, веб и воркер
curl http://localhost:8000/health            # {"status":"ok"}
```

Проверки, которые должны проходить перед слиянием (их же запускает CI):

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest           # нужна база: docker compose -f compose.dev.yaml up -d db
cd admin && npm ci && npm run typecheck && npm run build
```

Команда `remnabay` запускает роли одного образа: `web`, `worker`, `migrate` и `healthcheck web|worker`.

Лицензия — [MIT](LICENSE).
