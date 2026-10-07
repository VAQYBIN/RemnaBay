/** Даты и деньги в админке: в часовом поясе магазина (1.18), табличными цифрами. */

const LOCALE = 'ru-RU'

export function formatDateTime(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat(LOCALE, {
    timeZone,
    day: 'numeric',
    month: 'long',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso))
}

export function formatDate(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat(LOCALE, {
    timeZone,
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  }).format(new Date(iso))
}

export function formatMoney(amount: string, currency: string): string {
  return new Intl.NumberFormat(LOCALE, { style: 'currency', currency }).format(Number(amount))
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat(LOCALE).format(value)
}

/** «3 подписки», «5 подписок»: формы по правилам русского языка. */
export function plural(count: number, one: string, few: string, many: string): string {
  const rule = new Intl.PluralRules(LOCALE).select(count)
  return rule === 'one' ? one : rule === 'few' ? few : many
}
