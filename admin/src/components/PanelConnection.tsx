import type { ReactNode } from 'react'

import type { Schemas } from '@/api/client'
import { formatDateTime } from '@/lib/format'

type Overview = Schemas['OverviewOut']

function ConnectionRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      {children}
    </div>
  )
}

/** Связь с панелью: на связи ли, последнее событие, последняя сверка (А1). На странице
 *  «Настройки → Панель» — ещё и версия панели (А11). */
export function PanelConnection({
  overview,
  timeZone,
  version,
}: {
  overview: Overview
  timeZone: string
  version?: ReactNode
}) {
  return (
    <dl className="grid gap-2 text-sm">
      <ConnectionRow label="Панель">
        <dd className={overview.panel_available ? 'text-success-text' : 'text-danger-text'}>
          {overview.panel_available ? 'на связи' : 'недоступна'}
        </dd>
      </ConnectionRow>
      {version !== undefined && (
        <ConnectionRow label="Версия">
          <dd className="tabular">{version}</dd>
        </ConnectionRow>
      )}
      <ConnectionRow label="Последнее событие">
        <dd className="tabular">
          {overview.last_panel_event_at ? formatDateTime(overview.last_panel_event_at, timeZone) : 'ещё не было'}
        </dd>
      </ConnectionRow>
      <ConnectionRow label="Сверка запускалась">
        <dd className="tabular">
          {overview.last_sync_at ? formatDateTime(overview.last_sync_at, timeZone) : 'ещё не было'}
        </dd>
      </ConnectionRow>
    </dl>
  )
}
