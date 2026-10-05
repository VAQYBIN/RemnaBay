import { ChevronRight } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'

import { $api, type Schemas } from '@/api/client'
import { Module } from '@/components/frame/Module'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { formatCount, formatMoney } from '@/lib/format'

type Period = Schemas['Period']

const PERIODS: { value: Period; label: string }[] = [
  { value: 'today', label: 'Сегодня' },
  { value: '7d', label: '7 дней' },
  { value: '30d', label: '30 дней' },
]

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-xl bg-muted p-4">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="tabular text-2xl font-semibold">{value}</span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </div>
  )
}

/** Цифры на главной (1.17–1.20): период — в часовом поясе магазина. */
export function StatsModule() {
  const [period, setPeriod] = useState<Period>('today')
  const { data } = $api.useQuery('get', '/api/admin/stats', { params: { query: { period } } })

  return (
    <Module
      title="Цифры"
      action={
        <ToggleGroup
          type="single"
          value={period}
          onValueChange={(value) => value && setPeriod(value as Period)}
          aria-label="Период"
          variant="outline"
          size="sm"
        >
          {PERIODS.map(({ value, label }) => (
            <ToggleGroupItem key={value} value={value}>
              {label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      }
    >
      {!data ? (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
          {Array.from({ length: 6 }, (_, index) => (
            <Skeleton key={index} className="h-24 rounded-xl" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
          <Tile label="Активные подписки" value={formatCount(data.active_paid)} hint="без триалов" />
          <Tile label="Активные триалы" value={formatCount(data.active_trials)} />
          <Tile label="Новые клиенты" value={formatCount(data.new_clients)} hint="за период" />
          {data.revenue && (
            <>
              <Tile
                label="Выручка"
                value={formatMoney(data.revenue.amount, data.revenue.currency)}
                hint="за период, за вычетом возвратов"
              />
              <Tile label="Оплаты" value={formatCount(data.revenue.payments)} hint="за период" />
            </>
          )}
          <Link
            to="/expiring"
            className="group flex min-w-0 flex-col gap-1 rounded-xl bg-muted p-4 transition-colors hover:bg-brand-soft"
          >
            <span className="flex items-center justify-between text-sm text-muted-foreground">
              Истекают за 3 дня
              <ChevronRight className="size-4" />
            </span>
            <span className="tabular text-2xl font-semibold">{formatCount(data.expiring_soon)}</span>
          </Link>
        </div>
      )}
    </Module>
  )
}
