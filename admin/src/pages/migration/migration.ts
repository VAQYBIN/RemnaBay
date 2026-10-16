import type { Schemas } from '@/api/client'
import { formatCount, plural } from '@/lib/format'

export type Run = Schemas['RunOut']
export type Report = Schemas['MigrationReport']
export type Group = Schemas['GroupReport']
export type Problem = Schemas['Problem']
export type MappingRule = Schemas['MappingRule']

// Пока прогон идёт, ход обновляется сам
export const PROGRESS_REFRESH_MS = 2000

export function running(run: Run): boolean {
  return run.finished_at === null
}

/** Название прогона: вид и пробный или настоящий (А9). */
export function runTitle(run: Run): string {
  const kind = run.kind === 'adoption' ? 'усыновления' : 'импорта'
  return run.dry_run ? `Пробный прогон ${kind}` : `Применение ${kind}`
}

export const STATES: Record<Run['state'], string> = {
  reading_panel: 'Читает панель',
  planning: 'Считает отчёт',
  applying: 'Применяет',
  done: 'Готово',
  failed: 'Не удался',
}

/** Что сейчас делает прогон и сколько сделано. */
export function progressText(run: Run): string {
  const total = run.total ?? 0
  if (run.state === 'reading_panel') {
    return total > 0
      ? `Прочитано пользователей панели: ${formatCount(run.processed)} из ${formatCount(total)}`
      : 'Читаем пользователей панели…'
  }
  if (run.state === 'applying') {
    return `Применено клиентов: ${formatCount(run.processed)} из ${formatCount(total)}`
  }
  return 'Считаем отчёт…'
}

export function progressShare(run: Run): number {
  const total = run.total ?? 0
  return total > 0 ? Math.min(100, (run.processed / total) * 100) : 0
}

const CONFLICTS: ReadonlySet<Problem['reason']> = new Set([
  'bound_elsewhere',
  'telegram_id_mismatch',
  'claimed_twice',
  'code_taken',
])

export function isConflict(problem: Problem): boolean {
  return CONFLICTS.has(problem.reason)
}

/** Причины конфликтов (9.12) и нераспознанного (9.11) понятными словами. */
export const REASONS: Record<Problem['reason'], string> = {
  bound_elsewhere: 'Пользователь панели уже привязан к другому клиенту',
  telegram_id_mismatch: 'В панели у пользователя другой Telegram ID',
  claimed_twice: 'Пользователь панели указан у двух клиентов файла',
  code_taken: 'Реферальный код уже ведёт на другого клиента',
  duplicate_telegram_id: 'Telegram ID повторяется в файле',
  bad_telegram_id: 'Неверный Telegram ID',
  panel_user_not_found: 'Пользователь панели не найден',
  inviter_not_found: 'Пригласившего нет ни в файле, ни в магазине',
  self_invite: 'Клиент указан пригласившим сам себя',
  bad_code: 'Неверный реферальный код',
}

/** Короткий итог прогона для истории. */
export function summaryText(run: Run): string {
  if (run.state === 'failed') return run.error ?? 'Прогон прервался'
  if (running(run)) return progressText(run)
  const report = run.report
  if (!report) return ''
  const parts = [
    `${run.dry_run ? 'усыновить' : 'усыновлено'} ${formatCount(report.adopt)} ${plural(report.adopt, 'пользователя', 'пользователей', 'пользователей')} панели`,
  ]
  if (report.file) parts.push(`в файле ${formatCount(report.file.file_clients)}`)
  if (report.conflicts > 0) parts.push(`конфликтов ${formatCount(report.conflicts)}`)
  if (report.unrecognized > 0) parts.push(`не распознано ${formatCount(report.unrecognized)}`)
  return parts.join(' · ')
}

export function devicesText(limit: number | null): string {
  if (limit === null) return 'лимит устройств не задан'
  return `${formatCount(limit)} ${plural(limit, 'устройство', 'устройства', 'устройств')}`
}

/** Ключ группы: набор сквадов и лимит устройств (9.15). */
export function groupKey(squads: readonly string[], deviceLimit: number | null): string {
  return `${[...squads].sort().join(',')}|${deviceLimit ?? ''}`
}
