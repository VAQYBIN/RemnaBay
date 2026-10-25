import type { Schemas } from '@/api/client'

import { JOURNAL } from './clients'
import { HISTORY } from './payments'

type Row = Schemas['JournalRowOut']
type Json = Schemas['JsonValue']

/** Действия, которых нет в журнале клиента и платежа, — понятными словами (4.35). */
const OTHER_ACTIONS: Record<string, string> = {
  'payment.notification_rejected': 'Уведомление провайдера отклонено',
  'payments.provider_connected': 'Провайдер подключён',
  'payments.provider_disconnected': 'Провайдер отключён',
  'payments.provider_keys_lost': 'Ключи провайдера не расшифровываются',
  'promo.created': 'Промокод создан',
  'promo.limits_changed': 'Ограничения промокода изменены',
  'promo.disabled': 'Промокод отключён',
  'promo.enabled': 'Промокод включён',
  'promo.activated': 'Промокод применён',
  'promo.activation_cancelled': 'Активация промокода отменена',
  'promo.limit_exceeded': 'Лимит промокода исчерпан',
  'tariff.created': 'Тариф создан',
  'tariff.changed': 'Тариф изменён',
  'tariff.archived': 'Тариф перенесён в архив',
  'tariff.restored': 'Тариф возвращён в продажу',
  'tariff.deleted': 'Тариф удалён',
  'tariff.reordered': 'Порядок тарифов изменён',
  'broadcast.started': 'Рассылка запущена',
  'broadcast.stopped': 'Рассылка остановлена',
  'broadcast.finished': 'Рассылка завершена',
  'broadcast.test_sent': 'Рассылка отправлена себе',
  'migration.dry_run': 'Пробный прогон миграции',
  'migration.applied': 'Миграция применена',
  'migration.failed': 'Прогон миграции не удался',
  'panel.webhook_rejected': 'Вебхук панели отклонён',
  'panel.event_repeated': 'Повторное событие панели',
  'panel.unavailable': 'Панель недоступна',
  'panel.available_again': 'Панель снова доступна',
  'setting.changed': 'Настройка изменена',
  'setting.generated': 'Секрет создан магазином',
  'setting.locked': 'Префикс имён в панели закреплён',
  'text.changed': 'Текст бота изменён',
  'text.reset': 'Текст бота сброшен',
  'brand.asset_changed': 'Логотип изменён',
  'brand.asset_deleted': 'Логотип удалён',
  'brand.applied_to_telegram': 'Применено в Telegram',
  'guide.refreshed': 'Конфиг Subscription Page обновлён',
  'guide.webpage_allowed_changed': 'Изменился доступ браузера к странице подписки',
  'team.login': 'Вход в админку',
  'team.logout': 'Выход из админки',
  'team.login_rejected': 'Вход отклонён',
  'team.owner_added': 'Владелец добавлен',
  'team.owner_restored': 'Владелец восстановлен',
  'queue.attempt': 'Попытка операции',
  'queue.failed': 'Операция не выполнена',
  'queue.dropped': 'Операция прекращена',
  'queue.failed_hook': 'Сбой обработки проваленной операции',
  'queue.retried': 'Операция повторена',
  'queue.cancelled': 'Операция отменена',
  'queue.resolved': 'Операция решена вручную',
}

const ACTIONS: Record<string, string> = { ...HISTORY, ...JOURNAL, ...OTHER_ACTIONS }

export function actionName(action: string): string {
  return ACTIONS[action] ?? action
}

/** Группы действий для фильтра «что»: часть имени действия до точки. */
const GROUPS: { group: string; label: string }[] = [
  { group: 'payment', label: 'Платежи' },
  { group: 'subscription', label: 'Подписки' },
  { group: 'device', label: 'Устройства' },
  { group: 'trial', label: 'Триал' },
  { group: 'refund_request', label: 'Запросы на возврат' },
  { group: 'promo', label: 'Промокоды' },
  { group: 'reminder', label: 'Напоминания' },
  { group: 'message', label: 'Сообщения клиентам' },
  { group: 'client', label: 'Клиенты' },
  { group: 'referral', label: 'Приглашения' },
  { group: 'broadcast', label: 'Рассылки' },
  { group: 'migration', label: 'Миграция' },
  { group: 'panel', label: 'Панель' },
  { group: 'payments', label: 'Платёжные провайдеры' },
  { group: 'tariff', label: 'Тарифы' },
  { group: 'setting', label: 'Настройки' },
  { group: 'text', label: 'Тексты бота' },
  { group: 'brand', label: 'Бренд' },
  { group: 'guide', label: 'Инструкция' },
  { group: 'team', label: 'Команда и вход' },
  { group: 'queue', label: 'Операции' },
]

/** Значение фильтра «что»: '' — все, `группа.*` — вся группа, иначе одно действие. */
export const ALL_ACTIONS = ''
const GROUP_SUFFIX = '.*'

export function actionQuery(value: string): { action?: string; action_group?: string } {
  if (value === ALL_ACTIONS) return {}
  if (value.endsWith(GROUP_SUFFIX)) return { action_group: value.slice(0, -GROUP_SUFFIX.length) }
  return { action: value }
}

/** Варианты фильтра «что»: сначала группа целиком, за ней её действия. */
export function actionOptions(): { options: string[]; labels: Record<string, string> } {
  const options = [ALL_ACTIONS]
  const labels: Record<string, string> = { [ALL_ACTIONS]: 'Все действия' }
  for (const { group, label } of GROUPS) {
    const value = `${group}${GROUP_SUFFIX}`
    options.push(value)
    labels[value] = `${label} — все`
    const actions = Object.keys(ACTIONS)
      .filter((name) => name.split('.')[0] === group)
      .sort((a, b) => ACTIONS[a].localeCompare(ACTIONS[b], 'ru'))
    for (const action of actions) {
      options.push(action)
      labels[action] = `${label}: ${ACTIONS[action]}`
    }
  }
  return { options, labels }
}

/** Виды объектов для фильтра «над чем»; `none` — записи без объекта. */
export const SUBJECT_TYPES: Record<string, string> = {
  client: 'Клиент',
  subscription: 'Подписка',
  payment: 'Платёж',
  promo_code: 'Промокод',
  tariff: 'Тариф',
  broadcast: 'Рассылка',
  migration_run: 'Прогон миграции',
  payment_provider: 'Платёжный провайдер',
  setting: 'Настройка',
  bot_text: 'Текст бота',
  brand_asset: 'Логотип',
  telegram_bot: 'Бот в Telegram',
  team_member: 'Участник команды',
  queue_task: 'Операция',
  telegram_chat: 'Чат Telegram',
  none: 'Без объекта',
}

// Объекты с числовым id: для них есть поле «id»
export const NUMERIC_SUBJECTS = new Set([
  'client',
  'subscription',
  'payment',
  'promo_code',
  'tariff',
  'broadcast',
  'migration_run',
  'team_member',
  'queue_task',
])

// Путь изменения (4.9); `env` — владелец из параметров запуска (OWNER_TELEGRAM_ID)
const SOURCES: Record<string, string> = {
  webhook: 'через вебхук',
  sync: 'при сверке',
  env: 'из параметров запуска',
}

const REASONS: Record<string, string> = {
  signature: 'неверная подпись',
  too_large: 'слишком большой запрос',
  unreadable: 'запрос не читается',
  secret_unreadable: 'секрет не расшифровывается',
  not_in_team: 'не в команде',
}

function text(value: Json | undefined): string | null {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return null
}

/** Главное из подробностей одной строкой: путь изменения (4.9), причина, дни,
 *  комментарий команды. Остальное — в «Подробностях». */
export function detailsSummary(entry: Row): string | null {
  const { details } = entry
  const parts: string[] = []
  const source = text(details.source)
  if (source) parts.push(SOURCES[source] ?? source)
  const reason = text(details.reason)
  if (reason) parts.push(REASONS[reason] ?? reason)
  if (typeof details.days === 'number') parts.push(`${details.days} дн.`)
  const comment = text(details.comment)
  if (comment) parts.push(`«${comment}»`)
  return parts.length > 0 ? parts.join(' · ') : null
}

/** Все подробности записи «ключ: значение» — для раскрытия. */
export function detailsLines(entry: Row): { key: string; value: string }[] {
  return Object.entries(entry.details).map(([key, value]) => ({
    key,
    value: text(value) ?? JSON.stringify(value),
  }))
}
