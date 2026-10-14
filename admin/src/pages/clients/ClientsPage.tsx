import { ChevronRight, Search, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { $api, type Schemas } from '@/api/client'
import { Module, PageTitle } from '@/components/frame/Module'
import { Badge } from '@/components/ui/badge'
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from '@/components/ui/input-group'
import { formatDate, plural } from '@/lib/format'
import { clientLabel } from '@/lib/payments'

type Row = Schemas['ClientRowOut']

// Поиск уходит на сервер, когда оператор перестал печатать
const SEARCH_DELAY_MS = 300

export function ClientFlags({ botBlocked, adopted }: { botBlocked: boolean; adopted: boolean }) {
  if (!botBlocked && !adopted) return null
  return (
    <>
      {botBlocked && <Badge className="bg-warning-soft text-warning-text">бот заблокирован</Badge>}
      {adopted && <Badge variant="secondary">усыновлён</Badge>}
    </>
  )
}

function subscriptionsText(row: Row, timeZone: string): string {
  if (row.subscriptions === 0) return 'Подписок нет'
  const count = `${row.subscriptions} ${plural(row.subscriptions, 'подписка', 'подписки', 'подписок')}`
  if (!row.nearest_end) return count
  const ended = new Date(row.nearest_end).getTime() < Date.now()
  return `${count} · ${ended ? 'истекла' : 'до'} ${formatDate(row.nearest_end, timeZone)}`
}

/** Строка клиента: имя, username, подписки, ближайшая дата окончания, пометки (А2). */
function ClientRowItem({ row, timeZone }: { row: Row; timeZone: string }) {
  return (
    <li>
      <Link to={`/clients/${row.client.id}`} className="flex items-center gap-3 py-3 hover:bg-muted/40">
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{clientLabel(row.client)}</p>
          <p className="truncate text-sm text-muted-foreground">
            {row.client.username && `@${row.client.username} · `}
            {subscriptionsText(row, timeZone)}
          </p>
          {(row.bot_blocked || row.adopted) && (
            <div className="mt-1 flex flex-wrap gap-1.5">
              <ClientFlags botBlocked={row.bot_blocked} adopted={row.adopted} />
            </div>
          )}
        </div>
        <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      </Link>
    </li>
  )
}

/** А2. Клиенты: поиск по Telegram ID, username, имени, ссылке подписки, имени
 *  пользователя в панели (10.1). Строка поиска — в адресе: «Назад» из карточки
 *  возвращает к тем же результатам. */
export function ClientsPage() {
  const [params, setParams] = useSearchParams()
  const query = params.get('q') ?? ''
  const [text, setText] = useState(query)
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (text.trim() !== query) setParams(text.trim() ? { q: text.trim() } : {}, { replace: true })
    }, SEARCH_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [text, query, setParams])
  const { data, isFetching } = $api.useQuery(
    'get',
    '/api/admin/clients',
    { params: { query: { q: query } } },
    { placeholderData: (previous) => previous },
  )

  return (
    <>
      <PageTitle>Клиенты</PageTitle>
      <Module title={query ? 'Найдено' : 'Последние клиенты'}>
        <div className="flex flex-col gap-3">
          <InputGroup className="h-10">
            <InputGroupAddon>
              <Search aria-hidden />
            </InputGroupAddon>
            <InputGroupInput
              inputMode="search"
              enterKeyHint="search"
              placeholder="Telegram ID, @username, имя, ссылка подписки"
              aria-label="Поиск клиента"
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
            {text && (
              <InputGroupAddon align="inline-end">
                <InputGroupButton size="icon-xs" aria-label="Очистить поиск" onClick={() => setText('')}>
                  <X />
                </InputGroupButton>
              </InputGroupAddon>
            )}
          </InputGroup>
          {data && data.clients.length === 0 && (
            <p className="text-sm text-muted-foreground">
              {query ? 'Никого не нашли. Проверьте, нет ли опечатки.' : 'Клиентов пока нет.'}
            </p>
          )}
          <ul className={isFetching ? 'divide-y opacity-60 transition-opacity' : 'divide-y transition-opacity'}>
            {data?.clients.map((row) => (
              <ClientRowItem key={row.client.id} row={row} timeZone={data.time_zone} />
            ))}
          </ul>
          {data && data.clients.length >= 50 && (
            <p className="text-sm text-muted-foreground">Показаны первые 50 — уточните поиск.</p>
          )}
        </div>
      </Module>
    </>
  )
}
