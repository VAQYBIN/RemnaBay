import { useQueryClient } from '@tanstack/react-query'
import { ChevronRight, Hourglass } from 'lucide-react'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { $api, errorMessage, type Schemas } from '@/api/client'
import { Module, PageTitle } from '@/components/frame/Module'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { formatDateTime, formatMoney } from '@/lib/format'
import { clientLabel, operationName, PAYMENT_STATES, PURPOSES } from '@/lib/payments'
import { cn } from '@/lib/utils'

import { CommentDialog } from './CommentDialog'

type Row = Schemas['PaymentRow']
type Operation = Schemas['OperationRow']
type Filter = Row['state'] | 'unknown'

const FILTERS: { value: Filter | null; label: string }[] = [
  { value: null, label: 'Все' },
  { value: 'pending', label: PAYMENT_STATES.pending },
  { value: 'paid', label: PAYMENT_STATES.paid },
  { value: 'applied', label: PAYMENT_STATES.applied },
  { value: 'paid_not_applied', label: PAYMENT_STATES.paid_not_applied },
  { value: 'declined', label: PAYMENT_STATES.declined },
  { value: 'expired', label: PAYMENT_STATES.expired },
  { value: 'cancelled', label: PAYMENT_STATES.cancelled },
  { value: 'resolved_manually', label: PAYMENT_STATES.resolved_manually },
  { value: 'refunded', label: PAYMENT_STATES.refunded },
  { value: 'partially_refunded', label: PAYMENT_STATES.partially_refunded },
  { value: 'unknown', label: 'Неизвестные' },
]

const PERIODS = [
  { days: null, label: 'За всё время' },
  { days: 0, label: 'Сегодня' },
  { days: 7, label: '7 дней' },
  { days: 30, label: '30 дней' },
] as const

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'shrink-0 rounded-full px-3 py-1 text-sm transition-colors',
        active ? 'bg-brand-soft font-medium text-brand-text' : 'text-muted-foreground hover:bg-muted',
      )}
    >
      {children}
    </button>
  )
}

/** Строка платежа: клиент, что оплачено, сумма, состояние; ведёт в карточку (А5). */
function PaymentRowItem({ row, timeZone }: { row: Row; timeZone: string }) {
  return (
    <li>
      <Link to={`/payments/${row.id}`} className="flex items-center gap-3 py-3 hover:bg-muted/40">
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">
            {row.is_unknown ? 'Неизвестный платёж' : clientLabel(row.client)}
          </p>
          <p className="truncate text-sm text-muted-foreground">
            {row.purpose ? PURPOSES[row.purpose] : 'Не привязан'}
            {row.tariff_name && ` · ${row.tariff_name}`}
            {' · '}
            {formatDateTime(row.paid_at ?? row.created_at, timeZone)}
          </p>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <Badge variant="secondary">{PAYMENT_STATES[row.state]}</Badge>
            {row.is_unknown && <Badge className="bg-warning-soft text-warning-text">неизвестный</Badge>}
            {row.amount_mismatch && <Badge className="bg-warning-soft text-warning-text">сумма не совпала</Badge>}
            {row.waiting_panel && <Badge className="bg-muted text-muted-foreground">ждёт панель</Badge>}
          </div>
        </div>
        <span className="tabular shrink-0 text-sm font-medium">{formatMoney(row.amount, row.currency)}</span>
        <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      </Link>
    </li>
  )
}

/** Проваленная операция, которая не платёж: «Повторить», «Отменить», «Отметить
 *  решённым вручную» (4.31, 4.32). */
function OperationItem({ operation, timeZone }: { operation: Operation; timeZone: string }) {
  const queryClient = useQueryClient()
  const [dialog, setDialog] = useState<'cancel' | 'resolve' | null>(null)
  const refresh = () => {
    setDialog(null)
    void queryClient.invalidateQueries()
  }
  const path = { params: { path: { task_id: operation.task_id } } }
  const retry = $api.useMutation('post', '/api/admin/operations/{task_id}/retry', { onSuccess: refresh })
  const cancel = $api.useMutation('post', '/api/admin/operations/{task_id}/cancel', { onSuccess: refresh })
  const resolve = $api.useMutation('post', '/api/admin/operations/{task_id}/resolve', { onSuccess: refresh })
  const error = retry.error ?? cancel.error ?? resolve.error

  return (
    <li className="flex flex-col gap-2 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="font-medium">{operationName(operation.name)}</p>
        {operation.failed_at && (
          <time className="text-sm text-muted-foreground">{formatDateTime(operation.failed_at, timeZone)}</time>
        )}
      </div>
      {operation.last_error && (
        <p className="break-words font-mono text-xs text-muted-foreground">{operation.last_error}</p>
      )}
      {operation.waiting_behind > 0 && (
        <p className="text-sm text-warning-text">Ждут за ней: {operation.waiting_behind}</p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={() => retry.mutate(path)} disabled={retry.isPending}>
          Повторить
        </Button>
        {operation.cancellable && (
          <Button size="sm" variant="outline" onClick={() => setDialog('cancel')}>
            Отменить
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => setDialog('resolve')}>
          Решено вручную
        </Button>
      </div>
      {error && !dialog && <p className="text-sm text-danger-text">{errorMessage(error, 'Не получилось')}</p>}
      <CommentDialog
        open={dialog === 'cancel'}
        onOpenChange={(open) => setDialog(open ? 'cancel' : null)}
        title="Отменить операцию?"
        description="Действие будет считаться невыполненным, клиент получит «Не удалось выполнить действие, напишите в поддержку». Следующие операции подписки пойдут дальше."
        confirm="Отменить операцию"
        pending={cancel.isPending}
        error={cancel.error ? errorMessage(cancel.error, 'Не получилось') : null}
        onConfirm={(comment) => cancel.mutate({ ...path, body: { comment } })}
      />
      <CommentDialog
        open={dialog === 'resolve'}
        onOpenChange={(open) => setDialog(open ? 'resolve' : null)}
        title="Отметить решённой вручную?"
        description="Используйте, если вы сами сделали всё нужное в панели. Следующие операции подписки пойдут дальше."
        confirm="Отметить"
        pending={resolve.isPending}
        error={resolve.error ? errorMessage(resolve.error, 'Не получилось') : null}
        onConfirm={(comment) => resolve.mutate({ ...path, body: { comment } })}
      />
    </li>
  )
}

/** Вкладка «Требуют внимания» (4.20, 4.23, 4.30, 4.31). */
function AttentionTab() {
  const { data } = $api.useQuery('get', '/api/admin/attention', {}, { refetchInterval: 30_000 })
  if (!data) return null
  const empty = data.payments.length === 0 && data.operations.length === 0
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Module
        title="Платежи"
        action={data.payments.length > 0 && <Badge className="bg-danger-soft text-danger-text">{data.payments.length}</Badge>}
      >
        {data.payments.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {empty ? 'Всё в порядке: разбирать нечего.' : 'Зависших платежей нет.'}
          </p>
        ) : (
          <ul className="divide-y">
            {data.payments.map((row) => (
              <PaymentRowItem key={row.id} row={row} timeZone={data.time_zone} />
            ))}
          </ul>
        )}
      </Module>
      <div className="flex flex-col gap-4">
        <Module
          title="Операции"
          action={data.operations.length > 0 && <Badge className="bg-danger-soft text-danger-text">{data.operations.length}</Badge>}
        >
          {data.operations.length === 0 ? (
            <p className="text-sm text-muted-foreground">Проваленных операций нет.</p>
          ) : (
            <ul className="divide-y">
              {data.operations.map((operation) => (
                <OperationItem key={operation.task_id} operation={operation} timeZone={data.time_zone} />
              ))}
            </ul>
          )}
        </Module>
        <Module title="Ждут панель" action={<Badge variant="secondary">{data.waiting_panel.length}</Badge>}>
          {data.waiting_panel.length === 0 ? (
            <p className="text-sm text-muted-foreground">Ничего не ждёт панель.</p>
          ) : (
            <>
              <p className="mb-2 flex items-center gap-2 text-sm text-muted-foreground">
                <Hourglass className="size-4" aria-hidden />
                Продолжатся сами, когда панель снова станет доступна.
              </p>
              <ul className="divide-y">
                {data.waiting_panel.map((task) => (
                  <li key={task.task_id} className="flex items-center justify-between gap-2 py-2 text-sm">
                    {task.payment_id ? (
                      <Link to={`/payments/${task.payment_id}`} className="text-brand-text hover:underline">
                        {operationName(task.name)} № {task.payment_id}
                      </Link>
                    ) : (
                      <span>{operationName(task.name)}</span>
                    )}
                    <time className="text-muted-foreground">{formatDateTime(task.created_at, data.time_zone)}</time>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Module>
      </div>
    </div>
  )
}

function isoDaysAgo(days: number, timeZone: string): string {
  const date = new Date(Date.now() - days * 24 * 60 * 60 * 1000)
  return new Intl.DateTimeFormat('en-CA', { timeZone }).format(date)
}

/** Вкладка «Все платежи»: фильтры по состоянию и периоду (А4). */
function AllTab() {
  const [state, setState] = useState<Filter | null>(null)
  const [days, setDays] = useState<number | null>(null)
  const zone = $api.useQuery('get', '/api/admin/attention')
  const timeZone = zone.data?.time_zone ?? 'Europe/Moscow'
  const since = days === null ? undefined : isoDaysAgo(days, timeZone)
  const { data } = $api.useQuery('get', '/api/admin/payments', {
    params: { query: { state: state ?? undefined, since } },
  })
  return (
    <Module title="Платежи">
      <div className="flex flex-col gap-3">
        <nav className="flex gap-1 overflow-x-auto pb-1" aria-label="Состояние">
          {FILTERS.map((filter) => (
            <Chip key={filter.label} active={state === filter.value} onClick={() => setState(filter.value)}>
              {filter.label}
            </Chip>
          ))}
        </nav>
        <nav className="flex gap-1 overflow-x-auto pb-1" aria-label="Период">
          {PERIODS.map((period) => (
            <Chip key={period.label} active={days === period.days} onClick={() => setDays(period.days)}>
              {period.label}
            </Chip>
          ))}
        </nav>
        {data?.payments.length === 0 && <p className="text-sm text-muted-foreground">Платежей нет.</p>}
        <ul className="divide-y">
          {data?.payments.map((row) => (
            <PaymentRowItem key={row.id} row={row} timeZone={data.time_zone} />
          ))}
        </ul>
        {data && data.payments.length >= 100 && (
          <p className="text-sm text-muted-foreground">Показаны последние 100 — уточните фильтр.</p>
        )}
      </div>
    </Module>
  )
}

/** А4. Платежи: вкладки «Требуют внимания» и «Все платежи». */
export function PaymentsPage() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'all' ? 'all' : 'attention'
  const overview = $api.useQuery('get', '/api/admin/overview')
  const attention = overview.data?.attention ?? 0
  return (
    <>
      <PageTitle>Платежи</PageTitle>
      <nav className="flex gap-1 overflow-x-auto" aria-label="Разделы платежей">
        <Chip active={tab === 'attention'} onClick={() => setParams({})}>
          {attention > 0 ? `Требуют внимания · ${attention}` : 'Требуют внимания'}
        </Chip>
        <Chip active={tab === 'all'} onClick={() => setParams({ tab: 'all' })}>
          Все платежи
        </Chip>
      </nav>
      {tab === 'attention' ? <AttentionTab /> : <AllTab />}
    </>
  )
}
