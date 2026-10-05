import { useOutletContext } from 'react-router'

import { $api } from '@/api/client'
import { PageTitle } from '@/components/frame/Module'
import { isOwner, type Member } from '@/lib/session'

import { ChecklistModule } from './home/Checklist'
import { OverviewModule } from './home/Overview'
import { StatsModule } from './home/Stats'

/** А1. Главная: чек-лист, пока есть невыполненные пункты (1.15), состояние и цифры. */
export function HomePage() {
  const member = useOutletContext<Member>()
  const checklist = $api.useQuery('get', '/api/admin/checklist')
  const stats = $api.useQuery('get', '/api/admin/stats', { params: { query: { period: 'today' } } })
  const timeZone = stats.data?.time_zone ?? 'Europe/Moscow'
  const showChecklist = checklist.data && (!checklist.data.complete || checklist.data.state !== 'open')

  return (
    <>
      <PageTitle>Главная</PageTitle>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="flex min-w-0 flex-col gap-4">
          {showChecklist && <ChecklistModule checklist={checklist.data} isOwner={isOwner(member)} />}
          <StatsModule />
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <OverviewModule timeZone={timeZone} />
        </div>
      </div>
    </>
  )
}
