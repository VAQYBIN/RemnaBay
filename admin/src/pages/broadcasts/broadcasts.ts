import type { Schemas } from '@/api/client'
import { parseMarkup, type MarkupNode } from '@/lib/telegramMarkup'

export type Broadcast = Schemas['BroadcastOut']
export type SegmentKind = Schemas['SegmentKind']

/** Сегменты MVP (11.2). */
export const SEGMENTS: Record<SegmentKind, string> = {
  all: 'Все клиенты',
  active: 'С активной подпиской',
  expired: 'С истёкшей подпиской',
}

export const STATES: Record<Broadcast['state'], string> = {
  draft: 'Черновик',
  sending: 'Идёт',
  stopped: 'Остановлена',
  finished: 'Завершена',
}

function plainText(nodes: MarkupNode[]): string {
  return nodes.map((node) => (node.kind === 'text' ? node.text : plainText(node.children))).join('')
}

/** Первая строка текста без разметки — название рассылки в истории. */
export function headline(broadcast: Broadcast): string {
  const text = plainText(parseMarkup(broadcast.content.text).nodes).trim()
  const line = text.split('\n', 1)[0]
  if (line) return line
  return broadcast.has_image ? 'Изображение без текста' : 'Пустой черновик'
}

export function imageUrl(broadcastId: number, version: number | string): string {
  return `/api/admin/broadcasts/${broadcastId}/image?v=${version}`
}
