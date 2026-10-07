import type { Schemas } from '@/api/client'

type State = Schemas['PaymentRow']['state']

/** Состояния платежа (01-domain, «Платёж»). */
export const PAYMENT_STATES: Record<State, string> = {
  pending: 'Ожидает оплаты',
  paid: 'Оплачен',
  applied: 'Применён',
  declined: 'Отклонён',
  expired: 'Истёк',
  cancelled: 'Отменён',
  paid_not_applied: 'Оплачен — не применён',
  resolved_manually: 'Решён вручную',
  refunded: 'Возвращён',
  partially_refunded: 'Частично возвращён',
}

export const PURPOSES: Record<NonNullable<Schemas['PaymentRow']['purpose']>, string> = {
  purchase: 'Покупка',
  renewal: 'Продление',
  tariff_change: 'Смена тарифа',
}

/** Операции очереди, которые видит команда; неизвестные показываются по имени. */
const OPERATIONS: Record<string, string> = {
  'payments.apply': 'Применение платежа',
}

export function operationName(name: string): string {
  return OPERATIONS[name] ?? name
}

const ACTORS: Record<Schemas['HistoryOut']['actor_type'], string> = {
  team_member: 'Команда',
  client: 'Клиент',
  system: 'Система',
  panel: 'Панель',
  provider: 'Провайдер',
}

export function actorName(actor: Schemas['HistoryOut']['actor_type']): string {
  return ACTORS[actor]
}

/** Записи журнала о платеже — понятными словами (4.25). */
const HISTORY: Record<string, string> = {
  'payment.created': 'Счёт создан',
  'payment.replaced': 'Счёт заменён новым',
  'payment.cancelled': 'Клиент отменил счёт',
  'payment.expired': 'Счёт истёк',
  'payment.declined': 'Оплата отклонена',
  'payment.paid': 'Оплата подтверждена',
  'payment.amount_mismatch': 'Сумма не совпала со счётом',
  'payment.unknown_received': 'Пришла неизвестная оплата',
  'payment.confirmation_repeated': 'Повторное подтверждение',
  'payment.applied': 'Применён',
  'payment.not_applied': 'Попытки исчерпаны',
  'payment.retried': 'Применить повторно',
  'payment.applying_as_new': 'Применить созданием новой подписки',
  'payment.resolved_manually': 'Отмечен решённым вручную',
  'payment.bound': 'Привязан к клиенту',
  'payment.polling_stopped': 'Опрос статуса остановлен',
}

export function historyName(action: string): string {
  return HISTORY[action] ?? action
}

export function clientLabel(client: Schemas['ClientRef'] | null | undefined): string {
  if (!client) return 'Клиент не известен'
  const name = client.name ?? (client.username ? `@${client.username}` : `Клиент ${client.id}`)
  return client.telegram_id ? `${name} · ID ${client.telegram_id}` : name
}
