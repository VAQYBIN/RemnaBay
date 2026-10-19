import { useQueryClient } from '@tanstack/react-query'
import { ChevronRight, FileUp, Play } from 'lucide-react'
import { type ChangeEvent, useRef, useState } from 'react'
import { Link, Navigate, useNavigate, useOutletContext } from 'react-router'

import { $api, errorMessage, fetchClient } from '@/api/client'
import { Module, PageTitle } from '@/components/frame/Module'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { formatDateTime } from '@/lib/format'
import { isOwner, type Member } from '@/lib/session'
import { cn } from '@/lib/utils'

import { PROGRESS_REFRESH_MS, type Run, running, runTitle, STATES, summaryText } from './migration'

export function StateBadge({ run }: { run: Run }) {
  return (
    <Badge
      variant={run.state === 'done' ? 'secondary' : 'outline'}
      className={cn(
        running(run) && 'border-transparent bg-brand-soft text-brand-text',
        run.state === 'failed' && 'border-transparent bg-danger-soft text-danger-text',
      )}
    >
      {STATES[run.state]}
    </Badge>
  )
}

function RunRow({ run, timeZone }: { run: Run; timeZone: string }) {
  return (
    <li>
      <Link
        to={`/migration/${run.id}`}
        className="-mx-3 flex items-center gap-3 rounded-lg px-3 py-3 transition-colors duration-150 hover:bg-muted/40"
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="truncate font-medium">{runTitle(run)}</p>
            <StateBadge run={run} />
          </div>
          <p className="truncate text-sm text-muted-foreground">
            {formatDateTime(run.started_at, timeZone)} · {summaryText(run)}
          </p>
        </div>
        <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      </Link>
    </li>
  )
}

/** А9. Миграция: усыновление и импорт — всегда сначала пробный прогон (9.1, 9.10); история. */
export function MigrationPage() {
  const member = useOutletContext<Member>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const { data } = $api.useQuery('get', '/api/admin/migration/runs', undefined, {
    enabled: isOwner(member),
    refetchInterval: (query) => (query.state.data?.runs.some(running) ? PROGRESS_REFRESH_MS : false),
  })
  const adopt = $api.useMutation('post', '/api/admin/migration/adoption')

  if (!isOwner(member)) return <Navigate to="/" replace />

  const opened = (run: Run) => {
    void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/migration/runs'] })
    void navigate(`/migration/${run.id}`)
  }

  const startAdoption = () => {
    setError(null)
    adopt.mutate(
      { body: { mapping: [] } },
      { onSuccess: opened, onError: (failure) => setError(errorMessage(failure, 'Не удалось запустить прогон')) },
    )
  }

  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setError(null)
    setUploading(true)
    try {
      const { data: run, error: failure } = await fetchClient.POST('/api/admin/migration/import', {
        body: file as unknown as never,
        bodySerializer: (body: unknown) => body as Blob,
        headers: { 'Content-Type': 'application/json' },
      })
      if (run) opened(run)
      else setError(errorMessage(failure, 'Не удалось загрузить файл'))
    } catch {
      setError('Не удалось загрузить файл: нет связи с магазином')
    } finally {
      setUploading(false)
    }
  }

  const runs = data?.runs ?? []
  const busy = runs.some(running)
  return (
    <div className="flex flex-col gap-6">
      <PageTitle>Миграция</PageTitle>
      {error && <p className="text-sm text-danger-text">{error}</p>}
      <div className="grid gap-6 lg:grid-cols-2">
        <Module
          title="Усыновление"
          action={
            <Button onClick={startAdoption} disabled={busy || adopt.isPending}>
              <Play />
              Пробный прогон
            </Button>
          }
        >
          <p className="text-sm text-muted-foreground">
            Пользователи панели с Telegram ID становятся клиентами магазина: клиент откроет бот и увидит свои
            подписки, ссылки не меняются. В панели ничего не меняется. Сначала — пробный прогон с отчётом, в нём же —
            тарифы для подписок.
          </p>
        </Module>
        <Module
          title="Импорт из прежнего бота"
          action={
            <>
              <input
                ref={input}
                type="file"
                accept="application/json,.json"
                className="hidden"
                onChange={(event) => void upload(event)}
              />
              <Button variant="outline" onClick={() => input.current?.click()} disabled={busy || uploading}>
                <FileUp />
                Загрузить файл
              </Button>
            </>
          }
        >
          <p className="text-sm text-muted-foreground">
            Файл в промежуточном формате переносит то, что знал прежний бот: реферальные коды и пригласивших, триал,
            оплаты. Пользователи панели усыновляются в том же прогоне. Для remnawave-tg-shop файл делает команда{' '}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">remnabay adapter remnawave-tg-shop</code>.
          </p>
        </Module>
      </div>
      {busy && <p className="text-sm text-muted-foreground">Идёт прогон — следующий можно запустить после него.</p>}
      <Module title="История">
        {!data ? (
          <p className="text-sm text-muted-foreground">Загрузка…</p>
        ) : runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">Прогонов ещё не было.</p>
        ) : (
          <ul className="-my-3 divide-y">
            {runs.map((run) => (
              <RunRow key={run.id} run={run} timeZone={data.time_zone} />
            ))}
          </ul>
        )}
      </Module>
    </div>
  )
}
