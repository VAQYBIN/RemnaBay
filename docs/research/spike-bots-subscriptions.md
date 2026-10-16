# Эксперимент: несколько подписок с одним Telegram ID в чужих ботах

**Статус:** проведён 2026-10-05 (этап 1 из [plan.md](../plan.md), последний пункт «Проверить на реальных данных»); предложение по спеке принято владельцем. Дополнен 2026-10-16 к этапу 10: платежи, триал, рефералы — раздел «Этап 10: данные для адаптера»

## Зачем
Адаптер remnawave-tg-shop (9.9) и усыновление (9.4: пользователи панели с одним Telegram ID становятся одним клиентом с несколькими подписками) опираются на то, как бот-источник хранит подписки и связывает их с пользователями панели. Ответ нужен до реализации блока 9 (этап 10).

## Вопросы
1. Может ли у одного Telegram-аккаунта быть несколько подписок, и как бот это хранит?
2. Как запись бота связана с пользователем панели: UUID, `shortUuid`, `username` (по какому шаблону)? Что из этого переживает переход панели на v3, где у пользователя нет UUID?
3. Пишет ли бот Telegram ID в пользователя панели (на этом держится усыновление, 9.4)?
4. Особые случаи: одна подписка на несколько аккаунтов, удалённые и пересозданные пользователи панели, пользователи без Telegram ID.

## Источники
| Бот | Репозиторий | Версия |
|---|---|---|
| remnawave-tg-shop, оригинал | github.com/kavore/remnawave-tg-shop | тег `v2.7.0`; после него база не менялась, с мая 2026 репозиторий в архиве |
| remnawave-tg-shop, форк владельца | github.com/VAQYBIN/remnawave-tg-shop | тег `v4.0.2` — на нём работает магазин владельца |
| Bedolaga (v1) | github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot | тег `v5.0.0` |

## Как проводить
- Разбор исходников: модели базы, код создания и продления подписки, вызовы панели. Код — вне репозитория, в `~/code/spike-bots`; в проект не переносится.
- Проверка на данных форка владельца: SQL-запрос только на чтение; владелец запускает его сам и присылает только итоговые числа — без Telegram ID и имён.

## Выводы

### Коротко

| | Оригинал v2.7.0 | Форк владельца v4.0.2 | Bedolaga v5.0.0 |
|---|---|---|---|
| Несколько подписок на один Telegram ID | нет: один пользователь панели на человека, покупка продлевает ту же запись | нет: так же; «права на тарифы» (основной и дополнения) сводятся в одного пользователя панели | только в режиме мультитарифа (`MULTI_TARIFF_ENABLED`, по умолчанию выключен): тогда каждая подписка — отдельный пользователь панели |
| Имя пользователя в панели | `tg_<Telegram ID>` | `tg_<Telegram ID>`; веб-клиенты — `web_…` | `user_<Telegram ID>` (шаблон настраивается); в мультитарифе — с суффиксом `_<код подписки>` |
| Связь с панелью в базе бота | UUID пользователя панели v2 | числовой ID панели v3 (`panel_user_id`); у старых записей — ещё UUID v2 | числовой ID панели v3 и `shortUuid`; у старых записей — UUID v2 |
| Telegram ID в пользователе панели | пишет | пишет (веб-клиентам — нет) | пишет (клиентам без Telegram — почту) |
| Клиенты без Telegram ID | нет | да — веб-клиенты, отрицательный `users.user_id` | да — вход по почте и OAuth, `telegram_id` пустой |
| Версия панели | 2.x | 3.x | 3.x |

**Ответ на вопрос.** Правило 9.4 («пользователи панели с одним Telegram ID становятся одним клиентом с несколькими подписками») подходит для всех трёх ботов: каждый пишет Telegram ID в пользователя панели. Несколько подписок на один Telegram ID в панели появляются только у Bedolaga в режиме мультитарифа. Несколько строк в таблице подписок бота — история и следы пересоздания пользователя панели, а не параллельные подписки: адаптеру нельзя считать каждую строку отдельной подпиской.

### Подробно

#### remnawave-tg-shop, оригинал (v2.7.0)
- `users.user_id` — Telegram ID (первичный ключ); `users.panel_user_uuid` — UUID пользователя панели, уникален: один пользователь панели на человека.
- `subscriptions` — много строк на человека (ограничений нет), но покупка, продление, бонус и триал перед записью деактивируют остальные активные строки этого пользователя панели и обновляют строку по `panel_subscription_uuid` (`subscriptionUuid` или `shortUuid` пользователя панели). Новая строка появляется, когда у пользователя панели сменился `shortUuid` или его пересоздали.
- Бот работает с панелью 2.x и адресует пользователя по UUID. В панели 3.x UUID пользователя нет, поэтому хранимые UUID непригодны как ключ; для сопоставления остаются Telegram ID (в панели), имя `tg_<Telegram ID>` и `shortUuid` (может устареть после отзыва ссылки).
- Репозиторий с мая 2026 в архиве; после тега v2.7.0 база не менялась.

#### remnawave-tg-shop, форк владельца (v4.0.2)
- Схема v2.7.0 сохранена без переименований; добавлены `users.panel_user_id` и `subscriptions.panel_user_id` (числовой ID панели v3), тарифы и права на тарифы (`pricing_plans`, `user_plan_entitlements`), веб-аккаунты (`accounts`), активации триала (`trial_activations`).
- Подписки — как в оригинале: одна активная строка на пользователя панели, повторная покупка обновляет её.
- Веб-клиенты без Telegram — строки `users` с отрицательным `user_id` и пользователем панели `web_…` без Telegram ID. **Адаптер не должен выдавать отрицательный `user_id` как Telegram ID.**
- Старые записи эпохи панели v2 хранят UUID v2 в `panel_user_uuid` и `subscriptionUuid` в `panel_subscription_uuid`; `subscriptions.panel_user_id` у них пустой.

#### Bedolaga (v5.0.0)
- `users.id` — свой ключ; `telegram_id` уникален, но может быть пустым (почта, OAuth, «фантомы» гостевой покупки).
- Связь «один пользователь — одна подписка» снята миграцией 0050; по умолчанию бот держит одну подписку, в мультитарифе — по одной на тариф, каждая со своим пользователем панели (`user_<ID>_<код>`), Telegram ID записывается во всех.
- Работает только с панелью 3.x; хранит числовой ID и `shortUuid`, старые UUID v2 остаются в отдельных колонках.

#### Данные магазина владельца (форк v4.0.2)
Запрос только на чтение, на выходе — только счётчики; абсолютные числа в репозиторий не выносятся.
- Ни у одного клиента нет больше одной активной подписки; несколько строк в таблице подписок — у единиц клиентов (история и пересоздание пользователя панели).
- У всех активных подписок есть числовой ID панели; среди активных нет записей со старыми UUID v2. Общих пользователей панели у разных клиентов нет.
- Есть несколько веб-клиентов без Telegram ID; активные подписки — у единиц из них.

### Что учесть адаптеру remnawave-tg-shop (9.9)
- Клиент — строка `users` с положительным `user_id`; строки с отрицательным `user_id` (веб-клиенты форка) в файл не попадают.
- Пользователь панели — `users.panel_user_id` (форк) — числовой ID для поля «Пользователи панели» ([решение 0043](../decisions/0043-panel-checks-refinements.md)). Для оригинала хранимые UUID v2 не годятся: связь с панелью находит усыновление по Telegram ID (9.10), поле можно не заполнять.
- Строки `subscriptions` — история; в файл не переносятся (формат и не предусматривает подписок).
- Версию схемы адаптер узнаёт по наличию `users.panel_user_id`.
- Оригинал работает только с панелью 2.x, а магазин — с 3.4.4 и новее. Значит, оператор оригинала переезжает уже после обновления панели, когда старый бот с ней не работает; адаптер читает только его базу, поэтому это не мешает.

## Предложения по спеке

> **Принято владельцем 2026-10-05, внесено в [06-scope.md](../06-scope.md):** обоснование «Привязка по ссылке подписки — v1» было «У клиентов автора Telegram ID есть», но в магазине автора есть веб-клиенты без Telegram ID, у единиц из них — активные подписки. Обоснование уточнено: Telegram ID есть почти у всех клиентов автора, единичных клиентов без Telegram владелец переносит вручную. Срок функции (v1) не меняется.

## Этап 10: данные для адаптера

### Зачем
Адаптер remnawave-tg-shop (9.9) заполняет поля промежуточного формата: язык, дату появления, «Триал использован», «Платил в старом боте» ([решение 0070](../decisions/0070-migrated-paid-clients.md)), реферальные коды и пригласившего. Нужно было понять, где старый бот это хранит и можно ли по его базе надёжно понять, кто платил.

### Как проводили
Разбор исходников оригинала (тег `v2.7.0`) и форка владельца (тег `v4.0.2`) в `~/code/spike-bots`, вне репозитория. Код ботов в проект не переносится. Проверка на копии данных форка — запросы ниже, владелец запускает их сам и присылает только счётчики.

### База
- Только PostgreSQL: оба бота строят адрес `postgresql+asyncpg://…` и пользуются возможностями PostgreSQL. Адаптеру хватает драйвера `psycopg`, который уже есть в магазине.
- Схема ведётся Alembic, миграции применяются при запуске бота. Многие столбцы созданы без значений по умолчанию на стороне базы: `NULL` в `language_code` значит `ru`.
- В форке столбцы появляются по миграциям: `trial_activations` — 0008, `trial_reset_grants` — 0009, новые столбцы `payments` и таблицы тарифов — 0010, `users.panel_user_id` и `subscriptions.panel_user_id` — 0021. Адаптер определяет, что есть в базе, по `information_schema`, а не по номеру версии.

### Клиент
| Поле формата | Оригинал v2.7.0 | Форк v4.0.2 |
|---|---|---|
| Telegram ID | `users.user_id` | `users.user_id > 0`; отрицательные — веб-клиенты, в файл не попадают |
| username, имя | `users.username`, `first_name`, `last_name` | так же |
| Язык | `users.language_code`: `ru` или `en` | так же; вход через Telegram на сайте пишет код языка Telegram как есть (`uk`, `pt-br`) |
| Дата появления | `users.registration_date` — момент первого /start | так же |
| Пользователи панели | имя `tg_<Telegram ID>` (UUID панели v2 в `panel_user_uuid` для панели v3 непригоден) | `users.panel_user_id` / `subscriptions.panel_user_id`; у старых записей — только имя `tg_<Telegram ID>` |

### Платежи
- Таблица `payments`: `user_id`, `provider`, `amount` (итог после скидки, `Float`), `currency`, `status`, `created_at`. В форке ещё `activation_status` и связь с правами на тариф `entitlement_payments` (`purpose`: `purchase`, `renewal`, `trial`).
- Провайдеры: ЮКасса (`RUB`), Звёзды Telegram (`telegram_stars`, валюта `XTR`, сумма — число звёзд), CryptoPay (валюта — настроенный актив), FreeKassa, Platega, SeverPay; в форке ещё LavaPay.
- **Успешный платёж — только `status = 'succeeded'`.** Так считает и сам бот: число оплат, выручка, «приглашённый купил». Остальные состояния:
  - ожидание — `pending_<провайдер>`, `pending`, `waiting_for_capture`;
  - промежуточное — `processing` (может застрять, если бот упал во время активации);
  - неуспех — `canceled`, `failed`, `failed_creation`, `failed_user_not_found`, `failed_metadata_error`; в форке ещё `expired`.
- **Без платежа:** триал, бонусные дни по промокоду (`promo_code_activations` без `payment_id`), реферальные бонусные дни, дни, добавленные админом в боте или веб-админке форка (только запись в `message_logs`). Подарков другому человеку и внутреннего баланса нет ни в одной версии. Админ не может создать платёж руками.
- **Возвраты в базе не видны.** Ни одна версия возвраты не записывает: возврат через личный кабинет ЮКассы оставляет платёж `succeeded`.
- **Поздние уведомления могут затереть `succeeded`:** chargeback в Platega → `canceled`, отказ SeverPay → `failed`, в форке — уведомления LavaPay. Такой клиент выглядит не платившим.
- **В форке веб-платежи остаются у веб-клиента.** Когда веб-аккаунт привязывают к Telegram, подписка переходит к Telegram-клиенту, а платежи остаются на отрицательном `site_user_id` аккаунта (`accounts.telegram_user_id`, `accounts.site_user_id`).
- **Удалённые админом клиенты** удаляются вместе с платежами.

### Можно ли понять, кто платил (решение 0070)
**Да.** Правило «у клиента есть хотя бы один платёж `succeeded` с суммой больше нуля» отделяет платные дни от подаренных: подаренные дни платежей не создают. Уточнения:
- **Провайдер любой**, включая Звёзды и криптовалюту: клиент платил деньгами в любой форме.
- **Форк:** учитываются и платежи привязанного веб-аккаунта (`accounts.site_user_id`). Подтверждением оплаты считается и активация: `activation_status = 'succeeded'` или связь `entitlement_payments` с `purpose` `purchase` или `renewal`. Это спасает оплату, статус которой позже затёрло уведомление провайдера. Сумма больше нуля обязательна: у пробного тарифа форка цена 0.
- **Не видно и не исправить по базе:**
  - вернувший деньги клиент остаётся «платившим»;
  - клиент с оспоренным платежом (chargeback) — «не платившим», если других оплат нет;
  - `processing` и `expired` считаются неоплатой, хотя деньги могли прийти.
- **Клиенты, которых бот подтянул из панели синхронизацией** (`/sync` в админке бота), создаются без истории платежей: `username`, `first_name`, `last_name` пустые, язык `ru`, `registration_date` — момент синхронизации. Если такой клиент платил раньше — в другом боте или до синхронизации, — правило запишет ему «не платил». Сколько таких в данных владельца, покажет запрос 4. Если найдутся, поле им не передаётся: усыновлённый клиент без поля считается платившим (0070). Признак определяется по итогам запроса.

### Триал
- **Оригинал:** явной отметки нет. Бот отказывает в триале, если у клиента есть **любая** строка `subscriptions` (`has_any_subscription_for_user`). Строку триала покупка перезаписывает: одна строка на пользователя панели.
- **Форк:** отказывает, если есть действующая (`reset_at IS NULL`) строка `trial_activations` клиента или его веб-аккаунта, **или** есть любая строка `subscriptions`. Строку `subscriptions` форк не проверяет, если админ выдал неиспользованное разрешение на повторный триал (`trial_reset_grants.used_at IS NULL`). Миграция 0008 не заполнила `trial_activations` по прошлым триалам, поэтому без проверки подписок старые триалы потерялись бы.
- **Правило адаптера** (утверждено владельцем): «Триал использован», если старый бот сейчас отказал бы в триале по этим же условиям.

### Рефералы
- Код клиента — `users.referral_code`: 9 знаков из `A–Z` и `0–9`. Ссылка бота — `?start=ref_u<код>`.
- Бот принимает параметр `start` по шаблону `^ref_((?:[uU][A-Za-z0-9]{9})|(?:[A-Za-z0-9]{9})|\d+)$`:
  - `ref_u<код>` и `ref_U<код>` — основной вид;
  - `ref_<код>` — код без буквы `u`;
  - `ref_<Telegram ID>` — старый вид ссылки, если включено `LEGACY_REFS` (по умолчанию включено).
  Код бот ищет без учёта регистра.
- `promo_<код>` — промокод старого бота; любой другой параметр — метка рекламной кампании. Промокоды и метки не переносятся.
- Пригласивший — `users.referred_by_id` (Telegram ID). Бот записывает его при первом /start и позже, если поле пусто и у клиента нет активной подписки. Ссылка на сайт форка (`/register?ref=<код>`) к боту не относится.
> **Предложение (не утверждено):** адаптер выдаёт каждому клиенту коды `ref_u<код>`, `ref_U<код>`, `ref_<код>` и `ref_<Telegram ID>` — значения параметра `start` целиком, как их получит новый бот ([решение 0071](../decisions/0071-stage-10-preparation.md)).

### Что адаптер берёт из каждой версии
| | Оригинал | Форк |
|---|---|---|
| Как узнать | нет `users.panel_user_id` | есть `users.panel_user_id` |
| Клиенты | все `users` | `users.user_id > 0` |
| Платил | `payments` клиента: `succeeded`, сумма > 0 | то же для клиента и его веб-аккаунта, плюс `activation_status` и `entitlement_payments` |
| Триал | любая строка `subscriptions` | `trial_activations` или любая строка `subscriptions` без неиспользованного разрешения |
| Пользователи панели | имя `tg_<ID>`, если у клиента есть строки `subscriptions` | `panel_user_id` из `users` и `subscriptions`; имя `tg_<ID>`, если числового ID нет |
| Коды | `ref_u…`, `ref_U…`, `ref_…`, `ref_<ID>` | так же |
| Пригласивший | `referred_by_id > 0` | так же |

### Запросы для копии данных форка
Только чтение. Каждый запрос возвращает только счётчики — без Telegram ID и имён. Запускать на копии базы форка, например через `psql`.

**1. Клиенты и веб-клиенты**
```sql
SELECT count(*) FILTER (WHERE user_id > 0) AS telegram_clients,
       count(*) FILTER (WHERE user_id < 0) AS web_clients
FROM users;
```

**2. Состояния платежей**
```sql
SELECT provider, status, count(*) AS payments, count(DISTINCT user_id) AS clients
FROM payments GROUP BY provider, status ORDER BY provider, status;
```

**3. Кто платил по правилу адаптера**
```sql
WITH ids AS (
  SELECT user_id AS tg_id, user_id AS pay_id FROM users WHERE user_id > 0
  UNION
  SELECT telegram_user_id, site_user_id FROM accounts
  WHERE telegram_user_id > 0 AND site_user_id IS NOT NULL
),
evidence AS (
  SELECT ids.tg_id, p.provider,
         p.status = 'succeeded' AS by_status,
         p.activation_status = 'succeeded'
           OR EXISTS (SELECT 1 FROM entitlement_payments ep
                      WHERE ep.payment_id = p.payment_id
                        AND ep.purpose IN ('purchase', 'renewal')) AS by_activation
  FROM ids JOIN payments p ON p.user_id = ids.pay_id
  WHERE p.amount > 0
)
SELECT
  (SELECT count(DISTINCT tg_id) FROM evidence WHERE by_status OR by_activation) AS paid,
  (SELECT count(DISTINCT tg_id) FROM evidence WHERE by_activation AND NOT by_status
     AND tg_id NOT IN (SELECT tg_id FROM evidence WHERE by_status)) AS paid_only_by_activation,
  (SELECT count(DISTINCT tg_id) FROM evidence e WHERE (by_status OR by_activation)
     AND NOT EXISTS (SELECT 1 FROM evidence e2 WHERE e2.tg_id = e.tg_id
                     AND (e2.by_status OR e2.by_activation)
                     AND e2.provider <> 'telegram_stars')) AS paid_only_with_stars,
  (SELECT count(DISTINCT ids.tg_id) FROM ids JOIN payments p ON p.user_id = ids.pay_id
     WHERE ids.pay_id < 0 AND p.status = 'succeeded' AND p.amount > 0) AS paid_via_web_account;
```

**4. Похожие на подтянутых синхронизацией**
Клиент ни разу не открывал бот (нет имени: у любого аккаунта Telegram оно есть), но у него есть подписка.
```sql
WITH paid AS (
  SELECT DISTINCT user_id FROM payments WHERE status = 'succeeded' AND amount > 0
)
SELECT
  count(*) FILTER (WHERE first_name IS NULL AND username IS NULL) AS without_name,
  count(*) FILTER (WHERE first_name IS NULL AND username IS NULL
                   AND EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.user_id))
    AS without_name_with_subscription,
  count(*) FILTER (WHERE first_name IS NULL AND username IS NULL
                   AND EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.user_id)
                   AND u.user_id NOT IN (SELECT user_id FROM paid))
    AS without_name_with_subscription_unpaid,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.user_id)
                   AND u.user_id NOT IN (SELECT user_id FROM paid)
                   AND NOT EXISTS (SELECT 1 FROM message_logs m WHERE m.user_id = u.user_id
                                   AND m.event_type = 'command:/start'))
    AS subscription_unpaid_no_start_in_logs
FROM users u WHERE u.user_id > 0;
```

**5. Моменты появления: сколько клиентов появилось в одну минуту** (всплеск — след синхронизации)
```sql
SELECT date_trunc('minute', registration_date) AS minute, count(*) AS clients
FROM users WHERE user_id > 0
GROUP BY 1 HAVING count(*) >= 20 ORDER BY 2 DESC LIMIT 10;
```

**6. Триал по правилу адаптера**
Столбец веб-аккаунта назван `web_id`: у `trial_activations` есть свой `site_user_id`, и без явного имени подзапрос сравнивал бы строку саму с собой (так было в первой версии запроса).
```sql
WITH ids AS (
  SELECT u.user_id AS tg_id, a.id AS account_id, a.site_user_id AS web_id
  FROM users u LEFT JOIN accounts a ON a.telegram_user_id = u.user_id
  WHERE u.user_id > 0
),
flags AS (
  SELECT i.tg_id,
    EXISTS (SELECT 1 FROM trial_activations t WHERE t.reset_at IS NULL
            AND (t.user_id IN (i.tg_id, i.web_id) OR t.telegram_user_id = i.tg_id
                 OR t.site_user_id = i.web_id OR t.account_id = i.account_id)) AS activation,
    EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id IN (i.tg_id, i.web_id)) AS any_subscription,
    EXISTS (SELECT 1 FROM trial_reset_grants g WHERE g.used_at IS NULL
            AND (g.user_id IN (i.tg_id, i.web_id) OR g.telegram_user_id = i.tg_id
                 OR g.site_user_id = i.web_id OR g.account_id = i.account_id)) AS reset_grant
  FROM ids i
)
SELECT count(*) FILTER (WHERE activation OR (any_subscription AND NOT reset_grant)) AS trial_used,
       count(*) FILTER (WHERE activation) AS by_activation,
       count(*) FILTER (WHERE any_subscription AND NOT activation AND NOT reset_grant) AS only_by_subscription,
       count(*) FILTER (WHERE reset_grant) AS unused_reset_grant
FROM flags;
```

**7. Рефералы**
```sql
SELECT
  count(*) FILTER (WHERE referral_code IS NOT NULL) AS with_code,
  count(*) FILTER (WHERE referral_code ~ '^[0-9]+$') AS digits_only_code,
  count(*) FILTER (WHERE referred_by_id IS NOT NULL) AS with_inviter,
  count(*) FILTER (WHERE referred_by_id < 0) AS inviter_is_web_client,
  count(*) FILTER (WHERE referred_by_id IS NOT NULL
                   AND referred_by_id NOT IN (SELECT user_id FROM users)) AS inviter_missing
FROM users WHERE user_id > 0;
```

**8. Языки**
```sql
SELECT coalesce(language_code, '(пусто)') AS language, count(*) AS clients
FROM users WHERE user_id > 0 GROUP BY 1 ORDER BY 2 DESC;
```

**9. Связь с панелью**
```sql
SELECT
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.user_id)) AS with_subscription_rows,
  count(*) FILTER (WHERE u.panel_user_id IS NOT NULL
                   OR EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.user_id
                              AND s.panel_user_id IS NOT NULL)) AS with_panel_id,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.user_id)
                   AND u.panel_user_id IS NULL
                   AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.user_id
                                   AND s.panel_user_id IS NOT NULL)) AS only_by_name
FROM users u WHERE u.user_id > 0;
```

**10. Успешные платежи: клиенты Telegram и веб-клиенты**
```sql
SELECT
  count(DISTINCT user_id) FILTER (WHERE status = 'succeeded') AS succeeded_clients,
  count(DISTINCT user_id) FILTER (WHERE status = 'succeeded' AND user_id > 0) AS succeeded_telegram,
  count(DISTINCT user_id) FILTER (WHERE status = 'succeeded' AND user_id < 0) AS succeeded_web,
  count(DISTINCT user_id) FILTER (WHERE status = 'succeeded' AND user_id < 0
    AND user_id IN (SELECT site_user_id FROM accounts WHERE telegram_user_id > 0)) AS succeeded_web_linked,
  count(*) FILTER (WHERE status = 'succeeded' AND amount <= 0) AS succeeded_zero_amount,
  count(DISTINCT user_id) FILTER (WHERE status = 'succeeded' AND user_id > 0
    AND user_id IN (SELECT user_id FROM payments p2 WHERE p2.status = 'succeeded'
                    GROUP BY user_id HAVING count(DISTINCT provider) > 1)) AS paid_with_several_providers
FROM payments;
```

**11. Клиенты без имени с подпиской и без оплат: откуда они**
Имя может быть пустым не только после синхронизации: при /start форк стирает имя и username, в которых есть ссылки или «запрещённые» слова (`telegram`, `t.me` и похожие).
```sql
WITH nameless AS (
  SELECT u.user_id FROM users u
  WHERE u.user_id > 0 AND u.first_name IS NULL AND u.username IS NULL
    AND EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.user_id)
    AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.user_id = u.user_id
                    AND p.status = 'succeeded' AND p.amount > 0)
)
SELECT
  count(*) AS nameless,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.telegram_user_id = n.user_id))
    AS with_web_account,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM trial_activations t
                                 WHERE t.telegram_user_id = n.user_id OR t.user_id = n.user_id))
    AS with_trial_activation,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = n.user_id
                                 AND s.status_from_panel = 'TRIAL')) AS with_trial_subscription,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM payments p WHERE p.user_id = n.user_id))
    AS with_any_payment_attempt,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM message_logs m WHERE m.user_id = n.user_id))
    AS with_any_log,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = n.user_id
                                 AND s.end_date > now())) AS with_active_subscription
FROM nameless n;
```

**12. Ведётся ли журнал сообщений бота**
```sql
SELECT
  (SELECT count(*) FROM users WHERE user_id > 0) AS telegram_clients,
  count(DISTINCT user_id) FILTER (WHERE event_type = 'command:/start') AS clients_with_start_in_logs,
  min(timestamp) AS first_log_at
FROM message_logs;
```

### Итоги на копии данных
Только доли и выводы, без абсолютных чисел. Запросы 1–9 — 2026-10-17; уточняющие 6, 10–12 — следом.
- **Платили** — около пятой части клиентов Telegram. Успешных оплат Звёздами нет, оплат через привязанный веб-аккаунт нет; подтверждение активации ни одного клиента не добавило. Единицы клиентов платили через нескольких провайдеров.
- **Платили только через сайт** — единицы веб-клиентов без Telegram. В файл они не попадают; их владелец переносит вручную, как решено на этапе 1.
- **CryptoPay хранит свои состояния счёта как есть:** `active` (счёт ещё не оплачен) и `expired`. Это не оплата, правило адаптера их не считает.
- **Языки** — только `ru` и `en`.
- **Связь с панелью:** у всех клиентов со строками подписок есть числовой ID панели; по имени `tg_<ID>` искать не придётся.
- **Синхронизация из панели — не найдено ни одного клиента.** Всплеска появлений в одну минуту нет. Клиенты без имени, с подпиской и без оплат (около 2 %) пришли не из синхронизации: у большинства есть веб-аккаунт или активация триала — синхронизация не создаёт ни того, ни другого. Пустое имя у них — от входа на сайт через Telegram или от того, что форк стёр имя при /start. Журнал сообщений ведётся: /start в нём есть почти у всех клиентов. Поэтому адаптер передаёт «Платил в старом боте» всем клиентам, отдельный признак синхронизации не нужен (решение 0071).
- **Ограничение для других операторов:** если у оператора remnawave-tg-shop клиенты подтянуты синхронизацией, адаптер запишет им «не платил» — надёжного признака таких клиентов в базе нет. Строки `users` они получают без имени, языка из Telegram и записи /start в журнале, но то же бывает и у обычных клиентов.
- **Код из одних цифр** встретился у единиц клиентов. Ссылку `ref_<только цифры>` старый бот читает как Telegram ID, поэтому адаптер такой код без буквы `u` не выдаёт.
- **Триал использован** — около трёх четвертей клиентов: все, у кого есть строки подписок. Активация триала записана лишь у малой их части, остальных находит проверка подписок — без неё большинство триалов потерялось бы. Неиспользованных разрешений на повторный триал нет. Первая версия запроса 6 из-за ошибки в запросе находила активацию у всех клиентов; адаптер считает на Python и этой ошибки не повторяет.
