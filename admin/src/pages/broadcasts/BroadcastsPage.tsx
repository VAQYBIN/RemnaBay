import { useQueryClient } from '@tanstack/react-query'
import { ChevronRight, Plus } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router'

import { $api, errorMessage } from '@/api/client'
import { Module, PageTitle } from '@/components/frame/Module'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { formatCount, formatDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'

import { type Broadcast, headline, SEGMENTS, STATES } from './broadcasts'

// Пока рассылка идёт, прогресс обновляется сам (11.5)
const PROGRESS_REFRESH_MS = 2000

/** Итог строкой: доставлено, бот заблокирован, ошибки (11.8). */
export function totalsText(broadcast: Broadcast): string {
  const { totals } = broadcast
  if (broadcast.state === 'draft') return SEGMENTS[broadcast.segment.kind]
  const parts = [`доставлено ${formatCount(totals.delivered)} из ${formatCount(totals.recipients)}`]
  if (totals.bot_blocked > 0) parts.push(`бот заблокирован ${formatCount(totals.bot_blocked)}`)
  if (totals.failed > 0) parts.push(`ошибки ${formatCount(totals.failed)}`)
  if (totals.skipped > 0) parts.push(`не отправлено ${formatCount(totals.skipped)}`)
  return parts.join(' · ')
}

export function StateBadge({ state }: { state: Broadcast['state'] }) {
  return (
    <Badge
      variant={state === 'finished' ? 'secondary' : 'outline'}
      className={cn(
        state === 'sending' && 'border-transparent bg-brand-soft text-brand-text',
        state === 'draft' && 'text-muted-foreground',
      )}
    >
      {STATES[state]}
    </Badge>
  )
}

function BroadcastRow({ broadcast, timeZone }: { broadcast: Broadcast; timeZone: string }) {
  const moment = broadcast.confirmed_at ?? broadcast.created_at
  return (
    <li>
      <Link to={`/broadcasts/${broadcast.id}`} className="flex items-center gap-3 py-3 hover:bg-muted/40">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="truncate font-medium">{headline(broadcast)}</p>
            <StateBadge state={broadcast.state} />
          </div>
          <p className="truncate text-sm text-muted-foreground">
            {formatDateTime(moment, timeZone)} · {totalsText(broadcast)}
          </p>
        </div>
        <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      </Link>
    </li>
  )
}

/** А8. Рассылки: история с итогами (11.8) и создание новой. */
export function BroadcastsPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const { data } = $api.useQuery('get', '/api/admin/broadcasts', undefined, {
    refetchInterval: (query) =>
      query.state.data?.broadcasts.some((broadcast) => broadcast.state === 'sending') ? PROGRESS_REFRESH_MS : false,
  })
  const create = $api.useMutation('post', '/api/admin/broadcasts')

  const start = () => {
    setError(null)
    create.mutate(
      { body: { content: { text: '', buy: false, renew: false, links: [] }, segment: { kind: 'all' } } },
      {
        onSuccess: (broadcast) => {
          void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/broadcasts'] })
          void navigate(`/broadcasts/${broadcast.id}`)
        },
        onError: (failure) => setError(errorMessage(failure, 'Не удалось создать рассылку')),
      },
    )
  }

  const broadcasts = data?.broadcasts ?? []
  return (
    <div className="flex flex-col gap-6">
      <PageTitle
        action={
          <Button onClick={start} disabled={create.isPending}>
            <Plus />
            Новая рассылка
          </Button>
        }
      >
        Рассылки
      </PageTitle>
      {error && <p className="text-sm text-danger-text">{error}</p>}
      <Module title="История">
        {!data ? (
          <p className="text-sm text-muted-foreground">Загрузка…</p>
        ) : broadcasts.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Рассылок ещё не было. Новая рассылка: текст, изображение и кнопки, сегмент клиентов, проверка
            сообщением себе.
          </p>
        ) : (
          <ul className="-my-3 divide-y">
            {broadcasts.map((broadcast) => (
              <BroadcastRow key={broadcast.id} broadcast={broadcast} timeZone={data.time_zone} />
            ))}
          </ul>
        )}
      </Module>
    </div>
  )
}
