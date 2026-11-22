import { ArrowLeft } from 'lucide-react'
import { Link } from 'react-router'

import { $api } from '@/api/client'
import { Module, PageTitle } from '@/components/frame/Module'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { formatDateTime } from '@/lib/format'

/** Подписки, истекающие в ближайшие 3 дня (1.17). Карточка клиента — блок 10. */
export function ExpiringPage() {
  const { data } = $api.useQuery('get', '/api/admin/subscriptions/expiring')
  const stats = $api.useQuery('get', '/api/admin/stats', { params: { query: { period: 'today' } } })
  const timeZone = stats.data?.time_zone ?? 'Europe/Moscow'

  return (
    <>
      <PageTitle
        action={
          <Button asChild variant="ghost">
            <Link to="/">
              <ArrowLeft />
              На главную
            </Link>
          </Button>
        }
      >
        Истекают за 3 дня
      </PageTitle>
      <Module title={data ? `Подписок: ${data.length}` : 'Подписки'}>
        {data?.length === 0 && <p className="text-sm text-muted-foreground">Таких подписок нет.</p>}
        <ul className="divide-y">
          {data?.map((row) => (
            <li key={row.subscription_id} className="flex flex-wrap items-center justify-between gap-2 py-3">
              <div className="min-w-0">
                <p className="truncate font-medium">
                  {row.first_name ?? 'Без имени'}
                  {row.username && <span className="ml-2 text-sm text-muted-foreground">@{row.username}</span>}
                </p>
                <p className="text-sm text-muted-foreground">
                  {row.name}
                  {row.tariff_name ? ` · ${row.tariff_name}` : ' · без тарифа'}
                  {row.telegram_id && <span className="tabular"> · ID {row.telegram_id}</span>}
                </p>
              </div>
              <div className="flex items-center gap-2">
                {row.is_trial && <Badge variant="secondary">триал</Badge>}
                <time className="text-sm" dateTime={row.expires_at}>
                  {formatDateTime(row.expires_at, timeZone)}
                </time>
              </div>
            </li>
          ))}
        </ul>
      </Module>
    </>
  )
}
