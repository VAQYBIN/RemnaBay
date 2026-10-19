import type { Schemas } from '@/api/client'

import { historyName } from './payments'

/** Состояние подписки — из панели (01-domain, «Подписка»). */
export const SUBSCRIPTION_STATUSES: Record<NonNullable<Schemas['SubscriptionOut']['status']>, string> = {
  active: 'Активна',
  limited: 'Трафик исчерпан',
  expired: 'Истекла',
  disabled: 'Отключена в панели',
}

export const REQUEST_STATES: Record<Schemas['RefundRequestState'], string> = {
  open: 'Открыт',
  done: 'Закрыт возвратом',
  rejected: 'Отказано',
}

/** Записи журнала по клиенту — понятными словами (10.2, 10.7). */
const JOURNAL: Record<string, string> = {
  'subscription.created': 'Подписка создана',
  'subscription.purchased': 'Подписка куплена поверх триала',
  'subscription.renewed': 'Подписка продлена',
  'subscription.changed_in_panel': 'Изменена в панели',
  'subscription.deleted_in_panel': 'Пользователь удалён в панели',
  'subscription.days_granted': 'Выданы дни',
  'subscription.days_added': 'Дни добавлены в панели',
  'subscription.days_grant_cancelled': 'Выдача дней отменена',
  'subscription.sync_requested': 'Синхронизация с панелью',
  'subscription.term_cut_by_refund': 'Срок сокращён возвратом',
  'device.deleted': 'Удалено устройство',
  'device.deleted_all': 'Удалены все устройства',
  'device.deleted_in_panel': 'Устройства удалены в панели',
  'device.registered': 'Подключено устройство',
  'reminder.sent': 'Отправлено напоминание',
  'message.not_delivered': 'Сообщение не доставлено',
  'trial.requested': 'Запрошен триал',
  'trial.issued': 'Выдан триал',
  'trial.skipped': 'Триал не выдан: клиент уже купил подписку',
  'referral.linked': 'Пришёл по приглашению',
  'client.adopted': 'Усыновлён при миграции',
  'client.imported': 'Перенесён из файла миграции',
  'subscription.adopted': 'Подписка усыновлена из панели',
  'promo.input_limited': 'Ввод промокодов приостановлен',
  'refund_request.filed': 'Зафиксирован запрос на возврат',
  'refund_request.declined': 'Отказано в возврате',
  'refund_request.done': 'Запрос закрыт возвратом',
}

export function journalName(action: string): string {
  return JOURNAL[action] ?? historyName(action)
}

/** Подробности записи, которые полезно видеть сразу: дни и комментарий команды. */
export function journalDetails(entry: Schemas['JournalOut']): string | null {
  const { details } = entry
  const parts: string[] = []
  if (typeof details.days === 'number') parts.push(`${details.days} дн.`)
  if (typeof details.comment === 'string') parts.push(`«${details.comment}»`)
  return parts.length > 0 ? parts.join(' · ') : null
}
