import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ChevronDown, ChevronRight, ChevronUp, Plus, RefreshCw, Smartphone, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { Link, useLocation, useNavigate, useOutletContext, useParams } from 'react-router'

import { $api, errorMessage, type Schemas } from '@/api/client'
import { CopyField } from '@/components/CopyField'
import { Field } from '@/components/Field'
import { Module, PageTitle } from '@/components/frame/Module'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { journalDetails, journalName, REQUEST_STATES, SUBSCRIPTION_STATUSES } from '@/lib/clients'
import { formatDate, formatDateTime, formatMoney, plural } from '@/lib/format'
import { actorName, clientLabel, PAYMENT_STATES, PURPOSES } from '@/lib/payments'
import { isOwner, type Member } from '@/lib/session'
import { cn } from '@/lib/utils'
import { CommentDialog } from '@/pages/payments/CommentDialog'

import { ClientFlags } from './ClientsPage'

type Card = Schemas['ClientCardOut']
type Subscription = Schemas['SubscriptionOut']

// Пока изменения подписки ждут в очереди (например, выданные дни), карточка
// обновляется сама: оператор видит новую дату, не нажимая «Обновить»
const WAITING_REFRESH_MS = 3_000
const QUICK_DAYS = [1, 3, 7, 30] as const

function daysText(days: number): string {
  return `${days} ${plural(days, 'день', 'дня', 'дней')}`
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap justify-between gap-x-3 gap-y-0.5 py-1.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-right">{children}</dd>
    </div>
  )
}

const STATUS_CLASS: Record<NonNullable<Subscription['status']>, string> = {
  active: 'bg-success-soft text-success-text',
  limited: 'bg-warning-soft text-warning-text',
  expired: 'bg-muted text-muted-foreground',
  disabled: 'bg-danger-soft text-danger-text',
}

function StatusBadge({ subscription }: { subscription: Subscription }) {
  if (subscription.deleted) return <Badge className="bg-muted text-muted-foreground">удалена в панели</Badge>
  if (!subscription.status) return null
  return <Badge className={STATUS_CLASS[subscription.status]}>{SUBSCRIPTION_STATUSES[subscription.status]}</Badge>
}

function useRefreshCard() {
  const queryClient = useQueryClient()
  return () => void queryClient.invalidateQueries()
}

/** Выдать дни: число дней и обязательный комментарий (10.3). */
function GrantDialog({
  card,
  subscription,
  open,
  onOpenChange,
}: {
  card: Card
  subscription: Subscription
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const refresh = useRefreshCard()
  const [days, setDays] = useState('')
  const [comment, setComment] = useState('')
  const grant = $api.useMutation('post', '/api/admin/clients/{client_id}/subscriptions/{subscription_id}/grant-days', {
    onSuccess: () => {
      close(false)
      refresh()
    },
  })
  const close = (next: boolean) => {
    if (!next) {
      setDays('')
      setComment('')
      grant.reset()
    }
    onOpenChange(next)
  }
  const value = /^\d+$/.test(days.trim()) ? Number(days.trim()) : null
  const valid = value !== null && value >= card.min_grant_days && value <= card.max_grant_days
  const expired = subscription.expires_at !== null && new Date(subscription.expires_at).getTime() < Date.now()

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="glass-float">
        <DialogHeader>
          <DialogTitle>Выдать дни</DialogTitle>
          <DialogDescription>
            Подписка «{subscription.name}»
            {subscription.expires_at &&
              (expired
                ? ` истекла ${formatDateTime(subscription.expires_at, card.time_zone)} — дни отсчитаются от момента выдачи`
                : ` действует до ${formatDateTime(subscription.expires_at, card.time_zone)}`)}
            . Клиент получит сообщение, когда дни добавятся в панели.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <Field id="grant-days" label="Сколько дней" hint={`От ${card.min_grant_days} до ${card.max_grant_days}.`}>
            <Input
              id="grant-days"
              inputMode="numeric"
              className="tabular"
              value={days}
              aria-invalid={days.trim() !== '' && !valid}
              onChange={(event) => setDays(event.target.value)}
            />
            <div className="flex flex-wrap gap-1.5">
              {QUICK_DAYS.map((quick) => (
                <Button
                  key={quick}
                  type="button"
                  size="sm"
                  variant="outline"
                  className={cn(value === quick && 'bg-brand-soft text-brand-text')}
                  onClick={() => setDays(String(quick))}
                >
                  {daysText(quick)}
                </Button>
              ))}
            </div>
          </Field>
          <Field id="grant-comment" label="Причина" hint="Видна только команде — в журнале и карточке клиента.">
            <Textarea id="grant-comment" value={comment} onChange={(event) => setComment(event.target.value)} />
          </Field>
          {grant.error && <p className="text-sm text-danger-text">{errorMessage(grant.error, 'Не получилось')}</p>}
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Отмена</Button>
          </DialogClose>
          <Button
            disabled={!valid || !comment.trim() || grant.isPending}
            onClick={() =>
              value !== null &&
              grant.mutate({
                params: { path: { client_id: card.client.id, subscription_id: subscription.id } },
                body: { days: value, comment: comment.trim() },
              })
            }
          >
            {valid && value !== null ? `Выдать ${daysText(value)}` : 'Выдать'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Устройства подписки — из панели при открытии карточки (10.2, 6.2). */
function Devices({ card, subscription }: { card: Card; subscription: Subscription }) {
  const { data, error } = $api.useQuery(
    'get',
    '/api/admin/clients/{client_id}/subscriptions/{subscription_id}/devices',
    { params: { path: { client_id: card.client.id, subscription_id: subscription.id } } },
    { retry: false },
  )
  if (error) return <p className="text-sm text-warning-text">{errorMessage(error, 'Панель не ответила')}</p>
  if (!data) return <p className="text-sm text-muted-foreground">Спрашиваем панель…</p>
  if (!data.enabled) return <p className="text-sm text-muted-foreground">Лимит устройств в панели выключен.</p>
  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-sm">
        Устройств: <span className="tabular">{data.devices.length}</span>
        {data.limit !== null && <span className="text-muted-foreground"> из {data.limit}</span>}
      </p>
      {data.devices.length > 0 && (
        <ul className="flex flex-col gap-1.5">
          {data.devices.map((device) => (
            <li key={device.hwid} className="flex items-start gap-2 rounded-xl bg-muted/60 px-3 py-2 text-sm">
              <Smartphone className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
              <span className="min-w-0 flex-1">
                <span className="block truncate">
                  {[device.model, device.platform, device.os_version].filter(Boolean).join(' · ') || 'Устройство'}
                </span>
                <span className="block truncate text-xs text-muted-foreground">
                  {device.app && `${device.app} · `}с {formatDateTime(device.connected_at, card.time_zone)}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** Подписка: тариф, статус и срок из панели, устройства; «Выдать дни», «Сбросить
 *  устройства», «Синхронизировать с панелью» (10.2–10.5). */
function SubscriptionModule({ card, subscription }: { card: Card; subscription: Subscription }) {
  const refresh = useRefreshCard()
  const [dialog, setDialog] = useState<'grant' | 'reset' | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const path = { params: { path: { client_id: card.client.id, subscription_id: subscription.id } } }
  const reset = $api.useMutation('post', '/api/admin/clients/{client_id}/subscriptions/{subscription_id}/reset-devices', {
    onSuccess: (result) => {
      setDialog(null)
      setNotice(
        result.result === 'done'
          ? 'Устройства удалены.'
          : 'Панель недоступна — устройства удалятся, когда она снова ответит.',
      )
      refresh()
    },
  })
  const sync = $api.useMutation('post', '/api/admin/clients/{client_id}/subscriptions/{subscription_id}/sync', {
    onSuccess: () => {
      setNotice('Данные подписки обновлены из панели.')
      refresh()
    },
  })
  const zone = card.time_zone
  const error = sync.error ?? reset.error

  return (
    <Module title={subscription.name} action={<StatusBadge subscription={subscription} />}>
      <div className="flex flex-col gap-4">
        <dl className="divide-y text-sm">
          <Row label="Тариф">
            {subscription.tariff_name ?? 'без тарифа'}
            {subscription.is_trial && <span className="text-muted-foreground"> · триал</span>}
          </Row>
          <Row label="Действует до">
            {subscription.expires_at ? <span className="tabular">{formatDateTime(subscription.expires_at, zone)}</span> : '—'}
          </Row>
          <Row label="В панели">
            <span className="font-mono text-xs">{subscription.panel_username}</span>
          </Row>
          {subscription.synced_at && <Row label="Сверено с панелью">{formatDateTime(subscription.synced_at, zone)}</Row>}
        </dl>
        {subscription.changes_waiting > 0 && (
          <p className="rounded-xl bg-muted/60 px-3 py-2 text-sm text-muted-foreground">
            Изменения в очереди: {subscription.changes_waiting}. Карточка обновится, когда они применятся в панели.
          </p>
        )}
        {!subscription.deleted && (
          <>
            <CopyField label="Ссылка подписки" value={subscription.subscription_url} />
            <Devices card={card} subscription={subscription} />
            <div className="flex flex-wrap gap-2">
              {subscription.can_grant && (
                <Button onClick={() => setDialog('grant')}>
                  <Plus />
                  Выдать дни
                </Button>
              )}
              <Button variant="outline" onClick={() => sync.mutate(path)} disabled={sync.isPending}>
                <RefreshCw className={cn(sync.isPending && 'animate-spin')} />
                Синхронизировать
              </Button>
              <Button variant="ghost" onClick={() => setDialog('reset')}>
                Сбросить устройства
              </Button>
            </div>
          </>
        )}
        {notice && !error && <p className="text-sm text-muted-foreground">{notice}</p>}
        {error && <p className="text-sm text-danger-text">{errorMessage(error, 'Не получилось')}</p>}
      </div>
      <GrantDialog
        card={card}
        subscription={subscription}
        open={dialog === 'grant'}
        onOpenChange={(open) => setDialog(open ? 'grant' : null)}
      />
      <Dialog open={dialog === 'reset'} onOpenChange={(open) => setDialog(open ? 'reset' : null)}>
        <DialogContent className="glass-float">
          <DialogHeader>
            <DialogTitle>Сбросить все устройства?</DialogTitle>
            <DialogDescription>
              Все устройства подписки «{subscription.name}» удалятся в панели. Клиенту придётся заново добавить
              подписку в приложения — ссылка останется прежней.
            </DialogDescription>
          </DialogHeader>
          {reset.error && <p className="text-sm text-danger-text">{errorMessage(reset.error, 'Не получилось')}</p>}
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="ghost">Отмена</Button>
            </DialogClose>
            <Button variant="destructive" onClick={() => reset.mutate(path)} disabled={reset.isPending}>
              Сбросить устройства
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Module>
  )
}

/** Запросы на возврат: зафиксировать — любой участник, отказать — только владелец (10.6). */
// Длинные списки карточки свёрнуты до последних записей: у давнего клиента их сотни
const FOLDED = 5

/** Свёрнутый список: первые `FOLDED` строк (или `shown`, если больше) и кнопка
 *  «Показать все». */
function useFolded<T>(items: T[], shown = FOLDED) {
  const [expanded, setExpanded] = useState(false)
  const visible = Math.max(FOLDED, shown)
  return {
    items: expanded ? items : items.slice(0, visible),
    toggle:
      items.length > visible ? (
        <Button variant="ghost" size="sm" className="-ml-2 mt-2" onClick={() => setExpanded(!expanded)}>
          {expanded ? <ChevronUp aria-hidden /> : <ChevronDown aria-hidden />}
          {expanded ? 'Свернуть' : `Показать все (${items.length})`}
        </Button>
      ) : null,
    expanded,
  }
}

function RefundRequestsModule({ card, member }: { card: Card; member: Member }) {
  const refresh = useRefreshCard()
  const [dialog, setDialog] = useState<{ kind: 'file' } | { kind: 'decline'; id: number } | null>(null)
  const done = () => {
    setDialog(null)
    refresh()
  }
  const file = $api.useMutation('post', '/api/admin/clients/{client_id}/refund-requests', { onSuccess: done })
  const decline = $api.useMutation('post', '/api/admin/clients/{client_id}/refund-requests/{request_id}/decline', {
    onSuccess: done,
  })
  const owner = isOwner(member)
  const zone = card.time_zone
  const open = card.refund_requests.filter((request) => request.state === 'open')
  // Открытые запросы — сверху и всегда видны: по ним нужно решение
  const folded = useFolded(card.refund_requests, open.length)

  return (
    <Module
      title="Запросы на возврат"
      action={
        <Button size="sm" variant="outline" onClick={() => setDialog({ kind: 'file' })}>
          Зафиксировать запрос
        </Button>
      }
    >
      {card.refund_requests.length === 0 ? (
        <p className="text-sm text-muted-foreground">Клиент не просил вернуть деньги.</p>
      ) : (
        <ul className="divide-y text-sm">
          {folded.items.map((request) => (
            <li key={request.id} className="flex flex-col gap-1 py-2.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Badge
                  className={
                    request.state === 'open' ? 'bg-warning-soft text-warning-text' : 'bg-muted text-muted-foreground'
                  }
                >
                  {REQUEST_STATES[request.state]}
                </Badge>
                <time className="text-muted-foreground">{formatDateTime(request.created_at, zone)}</time>
              </div>
              <p className="break-words">«{request.comment}»</p>
              <p className="text-xs text-muted-foreground">Участник: {request.created_by}</p>
              {request.state === 'rejected' && (
                <p className="text-xs text-muted-foreground">
                  Отказ{request.closed_by && ` (${request.closed_by})`}
                  {request.close_comment && `: «${request.close_comment}»`}
                </p>
              )}
              {request.state === 'done' && request.payment_id && (
                <Link to={`/payments/${request.payment_id}`} className="text-xs text-brand-text hover:underline">
                  Возврат по платежу № {request.payment_id}
                </Link>
              )}
              {request.state === 'open' && owner && (
                <div className="mt-1 flex flex-wrap gap-2">
                  <Button size="sm" variant="ghost" onClick={() => setDialog({ kind: 'decline', id: request.id })}>
                    Отказать
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {folded.toggle}
      {open.length > 0 && (
        <p className="mt-3 text-xs text-muted-foreground">
          {owner
            ? 'Вернуть деньги — из карточки платежа: возврат закроет запрос.'
            : 'Решение о возврате принимает владелец.'}
        </p>
      )}
      <CommentDialog
        open={dialog?.kind === 'file'}
        onOpenChange={(next) => setDialog(next ? { kind: 'file' } : null)}
        title="Зафиксировать запрос на возврат"
        description="Запишите, что просит клиент и почему. Решение о возврате принимает владелец."
        confirm="Зафиксировать"
        pending={file.isPending}
        error={file.error ? errorMessage(file.error, 'Не получилось') : null}
        onConfirm={(comment) => file.mutate({ params: { path: { client_id: card.client.id } }, body: { comment } })}
      />
      <CommentDialog
        open={dialog?.kind === 'decline'}
        onOpenChange={(next) => !next && setDialog(null)}
        title="Отказать в возврате?"
        description="Клиент получит сообщение «Мы рассмотрели ваш запрос на возврат и не можем его выполнить». Причину видит только команда."
        confirm="Отказать"
        pending={decline.isPending}
        error={decline.error ? errorMessage(decline.error, 'Не получилось') : null}
        onConfirm={(comment) =>
          dialog?.kind === 'decline' &&
          decline.mutate({
            params: { path: { client_id: card.client.id, request_id: dialog.id } },
            body: { comment },
          })
        }
      />
    </Module>
  )
}

/** Платежи клиента со статусами; переход в карточку платежа (А3). */
function PaymentsModule({ card }: { card: Card }) {
  return (
    <Module title="Платежи">
      {card.payments.length === 0 ? (
        <p className="text-sm text-muted-foreground">Платежей нет.</p>
      ) : (
        <ul className="divide-y">
          {card.payments.map((payment) => (
            <li key={payment.id}>
              <Link to={`/payments/${payment.id}`} className="flex items-center gap-3 py-2.5 hover:bg-muted/40">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm">
                    {payment.purpose ? PURPOSES[payment.purpose] : 'Платёж'}
                    {payment.tariff_name && ` · ${payment.tariff_name}`}
                  </p>
                  <p className="truncate text-xs text-muted-foreground">
                    {PAYMENT_STATES[payment.state]} · {formatDateTime(payment.paid_at ?? payment.created_at, card.time_zone)}
                  </p>
                </div>
                <span className="tabular shrink-0 text-sm font-medium">{formatMoney(payment.amount, payment.currency)}</span>
                <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Module>
  )
}

function ClientLink({ client }: { client: Schemas['ClientRef'] }) {
  return (
    <Link to={`/clients/${client.id}`} className="text-brand-text hover:underline">
      {clientLabel(client)}
    </Link>
  )
}

/** Триал и реферальные связи: кто пригласил, кого пригласил (10.2). */
function TrialAndReferrals({ card }: { card: Card }) {
  const zone = card.time_zone
  const { trial } = card
  return (
    <Module title="Триал и приглашения">
      <dl className="divide-y text-sm">
        <Row label="Триал">
          {trial
            ? `${trial.imported ? 'перенесён' : 'получен'} ${formatDate(trial.used_at, zone)}${trial.revoked_at ? ', отозван' : ''}`
            : 'не получал'}
        </Row>
        <Row label="Пригласил клиента">{card.inviter ? <ClientLink client={card.inviter} /> : '—'}</Row>
        <Row label="Пригласил сам">
          {card.invited.length === 0
            ? '—'
            : `${card.invited.length} ${plural(card.invited.length, 'клиент', 'клиента', 'клиентов')}`}
        </Row>
      </dl>
      {card.invited.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1 text-sm">
          {card.invited.map((client) => (
            <li key={client.id} className="truncate">
              <ClientLink client={client} />
            </li>
          ))}
        </ul>
      )}
    </Module>
  )
}

/** Журнал по клиенту: кто, что и когда сделал (10.2, 10.7). */
function JournalModule({ card }: { card: Card }) {
  const folded = useFolded(card.journal)
  return (
    <Module title="Журнал">
      {card.journal.length === 0 ? (
        <p className="text-sm text-muted-foreground">Записей пока нет.</p>
      ) : (
        <ol className="divide-y text-sm">
          {folded.items.map((entry, index) => {
            const details = journalDetails(entry)
            return (
              <li key={index} className="flex flex-col gap-0.5 py-2">
                <div className="flex flex-wrap justify-between gap-2">
                  <span className={entry.outcome === 'failure' ? 'text-warning-text' : undefined}>
                    {journalName(entry.action)}
                  </span>
                  <time className="text-muted-foreground">{formatDateTime(entry.occurred_at, card.time_zone)}</time>
                </div>
                <span className="break-words text-xs text-muted-foreground">
                  {entry.actor_type === 'team_member' && entry.actor ? entry.actor : actorName(entry.actor_type)}
                  {details && ` · ${details}`}
                </span>
              </li>
            )
          })}
        </ol>
      )}
      {folded.expanded && card.journal.length >= card.journal_limit && (
        <p className="mt-2 text-xs text-muted-foreground">Показаны последние {card.journal_limit} записей.</p>
      )}
      {folded.toggle}
    </Module>
  )
}

/** А3. Карточка клиента (10.2): шапка, подписки с действиями, запросы на возврат,
 *  платежи, триал и приглашения, журнал. На телефоне — одна колонка в порядке
 *  важности. */
export function ClientCardPage() {
  const id = Number(useParams().clientId)
  const member = useOutletContext<Member>()
  const navigate = useNavigate()
  const location = useLocation()
  const { data: card, error } = $api.useQuery(
    'get',
    '/api/admin/clients/{client_id}',
    { params: { path: { client_id: id } } },
    {
      retry: false,
      refetchInterval: (query) =>
        query.state.data?.subscriptions.some((subscription) => subscription.changes_waiting > 0)
          ? WAITING_REFRESH_MS
          : false,
    },
  )
  // «Назад» возвращает к тем же результатам поиска; открыли карточку по ссылке — к списку
  const back = (
    <Button variant="ghost" onClick={() => void (location.key === 'default' ? navigate('/clients') : navigate(-1))}>
      <ArrowLeft />
      Клиенты
    </Button>
  )
  if (error) {
    return (
      <>
        <PageTitle action={back}>Клиент</PageTitle>
        <Module title="Не найден">
          <p className="text-sm text-muted-foreground">{errorMessage(error, 'Такого клиента нет.')}</p>
        </Module>
      </>
    )
  }
  if (!card) return null
  const live = card.subscriptions.filter((subscription) => !subscription.deleted)
  const deleted = card.subscriptions.filter((subscription) => subscription.deleted)

  return (
    <>
      <PageTitle action={back}>{clientLabel({ ...card.client, telegram_id: null })}</PageTitle>
      <Module
        title="Клиент"
        action={
          <div className="flex flex-wrap gap-1.5">
            <ClientFlags botBlocked={card.bot_blocked} adopted={card.adopted} />
          </div>
        }
      >
        <dl className="grid gap-x-8 text-sm sm:grid-cols-2">
          <Row label="Telegram ID">
            <span className="tabular">{card.client.telegram_id ?? '—'}</span>
          </Row>
          <Row label="Username">{card.client.username ? `@${card.client.username}` : '—'}</Row>
          <Row label="Появился">{formatDate(card.first_seen_at ?? card.created_at, card.time_zone)}</Row>
          <Row label="Подписок">{live.length}</Row>
        </dl>
      </Module>
      {!card.panel_available && (
        <p className="glass-module flex items-start gap-2 p-4 text-sm text-warning-text">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          Панель сейчас недоступна — состояние подписок может быть неактуальным.
        </p>
      )}
      <div className="grid gap-4 lg:grid-cols-[3fr_2fr]">
        <div className="flex min-w-0 flex-col gap-4">
          {live.length === 0 && (
            <Module title="Подписки">
              <p className="text-sm text-muted-foreground">У клиента нет подписок.</p>
            </Module>
          )}
          {live.map((subscription) => (
            <SubscriptionModule key={subscription.id} card={card} subscription={subscription} />
          ))}
          <PaymentsModule card={card} />
          {deleted.length > 0 && (
            <Module title="Удалены в панели">
              <ul className="divide-y text-sm">
                {deleted.map((subscription) => (
                  <li key={subscription.id} className="flex flex-wrap justify-between gap-2 py-2">
                    <span>{subscription.name}</span>
                    <span className="font-mono text-xs text-muted-foreground">{subscription.panel_username}</span>
                  </li>
                ))}
              </ul>
            </Module>
          )}
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <RefundRequestsModule card={card} member={member} />
          <TrialAndReferrals card={card} />
          <JournalModule card={card} />
        </div>
      </div>
    </>
  )
}
