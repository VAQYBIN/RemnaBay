import { Link } from 'react-router'

import { $api } from '@/api/client'
import { Module } from '@/components/frame/Module'
import { PanelConnection } from '@/components/PanelConnection'
import { cn } from '@/lib/utils'

const STATES = {
  not_opened: 'Магазин ещё не открыт: бот работает только для команды и тестировщиков.',
  open: 'Магазин открыт.',
  paused: 'Магазин временно закрыт: новые покупки, продления и триалы недоступны.',
} as const

/** «Требуют внимания» со ссылкой на список (4.20, 4.30) и состояние связей с панелью (А1). */
export function OverviewModule({ timeZone }: { timeZone: string }) {
  const { data } = $api.useQuery('get', '/api/admin/overview', {}, { refetchInterval: 60_000 })
  if (!data) return null
  return (
    <Module title="Состояние">
      <div className="flex flex-col gap-4">
        <p className="text-sm">{STATES[data.state]}</p>
        <div className="grid grid-cols-2 gap-3">
          <Link to="/payments" className="rounded-xl bg-muted p-4 transition-colors hover:bg-muted/70">
            <p className="text-sm text-muted-foreground">Требуют внимания</p>
            <p className={cn('tabular text-2xl font-semibold', data.attention > 0 && 'text-danger-text')}>
              {data.attention}
            </p>
          </Link>
          <Link to="/payments" className="rounded-xl bg-muted p-4 transition-colors hover:bg-muted/70">
            <p className="text-sm text-muted-foreground">Ждут панель</p>
            <p className="tabular text-2xl font-semibold">{data.waiting_panel}</p>
          </Link>
        </div>
        <PanelConnection overview={data} timeZone={timeZone} />
      </div>
    </Module>
  )
}
