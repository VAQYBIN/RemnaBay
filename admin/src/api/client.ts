import createFetchClient from 'openapi-fetch'
import createClient from 'openapi-react-query'

import type { components, paths } from './schema'

/** Запросы к API админки того же сервера; cookie сессии уходит сама (решение 0051). */
export const fetchClient = createFetchClient<paths>({
  baseUrl: '',
  credentials: 'same-origin',
})

export const $api = createClient(fetchClient)

export type Schemas = components['schemas']

/** Текст ошибки FastAPI для показа оператору. */
export function errorMessage(error: unknown, fallback: string): string {
  if (typeof error === 'object' && error !== null && 'detail' in error) {
    const detail = (error as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
  }
  return fallback
}
