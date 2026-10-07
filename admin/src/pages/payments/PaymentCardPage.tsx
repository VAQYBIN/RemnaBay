import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { useState } from 'react'
import { Link, useParams } from 'react-router'

import { $api, errorMessage, type Schemas } from '@/api/client'
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
import { formatDateTime, formatMoney } from '@/lib/format'
import { actorName, clientLabel, historyName, PAYMENT_STATES, PURPOSES } from '@/lib/payments'

import { CommentDialog } from './CommentDialog'

type Card = Schemas['PaymentCardOut']

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap justify-between gap-x-3 gap-y-0.5 py-1.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-right">{children}</dd>
    </div>
  )
}

const ATTEMPT_RESULTS: Record<NonNullable<Schemas['AttemptOut']['result']>, string> = {
  done: 'выполнена',
  error: 'ошибка',
  unavailable: 'панель недоступна',
  aborted: 'оборвалась',
  rejected: 'отказ',
}

/** Привязать неизвестный платёж к клиенту и выбрать, что применить (4.23). */
function BindDialog({ card, open, onOpenChange }: { card: Card; open: boolean; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient()
  const [telegramId, setTelegramId] = useState('')
  const [purpose, setPurpose] = useState<'purchase' | 'renewal'>('purchase')
  const [subscriptionId, setSubscriptionId] = useState<number | null>(null)
  const [tariffId, setTariffId] = useState<number | null>(null)
  const id = Number(telegramId)
  const lookup = $api.useQuery(
    'get',
    '/api/admin/clients/by-telegram/{telegram_id}',
    { params: { path: { telegram_id: id } } },
    { enabled: open && Number.isInteger(id) && id > 0, retry: false },
  )
  const tariffs = $api.useQuery('get', '/api/admin/tariff-options', {}, { enabled: open })
  const bind = $api.useMutation('post', '/api/admin/payments/{payment_id}/bind', {
    onSuccess: () => {
      onOpenChange(false)
      void queryClient.invalidateQueries()
    },
  })
  const subscriptions = lookup.data?.subscriptions ?? []
  const ready =
    lookup.data !== undefined && tariffId !== null && (purpose === 'purchase' || subscriptionId !== null)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="glass-float">
        <DialogHeader>
          <DialogTitle>Привязать к клиенту</DialogTitle>
          <DialogDescription>
            Оплата на {formatMoney(card.payment.amount, card.payment.currency)} применится к клиенту выбранным
            тарифом: новой подпиской или продлением.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <Field id="bind-telegram" label="Telegram ID клиента">
            <Input
              id="bind-telegram"
              inputMode="numeric"
              className="tabular"
              value={telegramId}
              onChange={(event) => {
                setTelegramId(event.target.value.trim())
                setSubscriptionId(null)
              }}
            />
          </Field>
          {lookup.data && <p className="text-sm">{clientLabel(lookup.data.client)}</p>}
          {lookup.error && <p className="text-sm text-danger-text">Клиента с таким Telegram ID нет.</p>}
          {lookup.data && (
            <fieldset className="flex flex-col gap-2">
              <legend className="mb-1 text-sm font-medium">Что применить</legend>
              <label className="flex items-center gap-2 text-sm">
                <input type="radio" checked={purpose === 'purchase'} onChange={() => setPurpose('purchase')} />
                Новая подписка
              </label>
              {subscriptions.map((subscription) => (
                <label key={subscription.id} className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    checked={purpose === 'renewal' && subscriptionId === subscription.id}
                    onChange={() => {
                      setPurpose('renewal')
                      setSubscriptionId(subscription.id)
                    }}
                  />
                  Продлить «{subscription.name}» <span className="text-muted-foreground">{subscription.panel_username}</span>
                </label>
              ))}
            </fieldset>
          )}
          {lookup.data && (
            <fieldset className="flex flex-col gap-2">
              <legend className="mb-1 text-sm font-medium">Тариф</legend>
              {tariffs.data?.map((tariff) => (
                <label key={tariff.id} className="flex items-center gap-2 text-sm">
                  <input type="radio" checked={tariffId === tariff.id} onChange={() => setTariffId(tariff.id)} />
                  {tariff.name} · {tariff.duration_days ?? '—'} дн. ·{' '}
                  {formatMoney(tariff.price, card.payment.currency)}
                  {tariff.state === 'archived' && <Badge variant="secondary">архив</Badge>}
                </label>
              ))}
            </fieldset>
          )}
          {bind.error && <p className="text-sm text-danger-text">{errorMessage(bind.error, 'Не получилось')}</p>}
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Отмена</Button>
          </DialogClose>
          <Button
            disabled={!ready || bind.isPending}
            onClick={() =>
              tariffId !== null &&
              bind.mutate({
                params: { path: { payment_id: card.payment.id } },
                body: {
                  telegram_id: id,
                  purpose,
                  tariff_id: tariffId,
                  subscription_id: purpose === 'renewal' ? subscriptionId : null,
                },
              })
            }
          >
            Привязать и применить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Действия по ситуации (4.22, 4.23); возврат — этап 9. */
function Actions({ card }: { card: Card }) {
  const queryClient = useQueryClient()
  const [dialog, setDialog] = useState<'resolve' | 'bind' | 'new' | null>(null)
  const refresh = () => {
    setDialog(null)
    void queryClient.invalidateQueries()
  }
  const path = { params: { path: { payment_id: card.payment.id } } }
  const retry = $api.useMutation('post', '/api/admin/payments/{payment_id}/retry', { onSuccess: refresh })
  const asNew = $api.useMutation('post', '/api/admin/payments/{payment_id}/apply-as-new', { onSuccess: refresh })
  const resolve = $api.useMutation('post', '/api/admin/payments/{payment_id}/resolve', { onSuccess: refresh })
  if (card.actions.length === 0) return null
  const error = retry.error ?? asNew.error

  return (
    <Module title="Что сделать">
      <div className="flex flex-col gap-3">
        {card.payment.amount_mismatch && (
          <p className="text-sm text-warning-text">
            Провайдер подтвердил другую сумму. Проверьте оплату в кабинете провайдера, прежде чем применять.
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          {card.actions.includes('retry') && (
            <Button onClick={() => retry.mutate(path)} disabled={retry.isPending}>
              Применить повторно
            </Button>
          )}
          {card.actions.includes('apply_as_new') && (
            <Button variant="outline" className="h-auto min-h-9 whitespace-normal" onClick={() => setDialog('new')}>
              Применить созданием новой подписки
            </Button>
          )}
          {card.actions.includes('bind') && <Button onClick={() => setDialog('bind')}>Привязать к клиенту</Button>}
          {card.actions.includes('resolve') && (
            <Button variant="ghost" className="h-auto min-h-9 whitespace-normal" onClick={() => setDialog('resolve')}>
              Отметить решённым вручную
            </Button>
          )}
        </div>
        {error && <p className="text-sm text-danger-text">{errorMessage(error, 'Не получилось')}</p>}
      </div>
      <Dialog open={dialog === 'new'} onOpenChange={(open) => setDialog(open ? 'new' : null)}>
        <DialogContent className="glass-float">
          <DialogHeader>
            <DialogTitle>Применить созданием новой подписки?</DialogTitle>
            <DialogDescription>
              Магазин создаст клиенту нового пользователя в панели с условиями этого платежа — например, если
              прежнего пользователя удалили в панели. Клиент получит новую ссылку.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="ghost">Отмена</Button>
            </DialogClose>
            <Button onClick={() => asNew.mutate(path)} disabled={asNew.isPending}>
              Создать подписку
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <CommentDialog
        open={dialog === 'resolve'}
        onOpenChange={(open) => setDialog(open ? 'resolve' : null)}
        title="Отметить решённым вручную?"
        description="Используйте, если вы сами всё сделали в панели. Клиент получит нейтральное сообщение «Мы разобрались с вашим платежом»."
        confirm="Отметить"
        pending={resolve.isPending}
        error={resolve.error ? errorMessage(resolve.error, 'Не получилось') : null}
        onConfirm={(comment) => resolve.mutate({ ...path, body: { comment } })}
      />
      <BindDialog card={card} open={dialog === 'bind'} onOpenChange={(open) => setDialog(open ? 'bind' : null)} />
    </Module>
  )
}

/** А5. Карточка платежа: клиент, что должно было примениться, попытки и ошибка (4.21). */
export function PaymentCardPage() {
  const id = Number(useParams().paymentId)
  const { data: card } = $api.useQuery('get', '/api/admin/payments/{payment_id}', {
    params: { path: { payment_id: id } },
  })
  if (!card) return null
  const { payment } = card
  const zone = card.time_zone

  return (
    <>
      <PageTitle
        action={
          <Button asChild variant="ghost">
            <Link to="/payments">
              <ArrowLeft />
              Платежи
            </Link>
          </Button>
        }
      >
        Платёж № {payment.id}
      </PageTitle>
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="flex flex-col gap-4">
          <Module
            title={PAYMENT_STATES[payment.state]}
            action={
              <div className="flex flex-wrap gap-1.5">
                {payment.is_unknown && <Badge className="bg-warning-soft text-warning-text">неизвестный</Badge>}
                {payment.amount_mismatch && (
                  <Badge className="bg-warning-soft text-warning-text">сумма не совпала</Badge>
                )}
                {payment.waiting_panel && <Badge variant="secondary">ждёт панель</Badge>}
              </div>
            }
          >
            <dl className="divide-y text-sm">
              <Row label="Клиент">{payment.is_unknown && !payment.client ? 'не привязан' : clientLabel(payment.client)}</Row>
              <Row label="Операция">{payment.purpose ? PURPOSES[payment.purpose] : '—'}</Row>
              {card.subscription && (
                <Row label="Подписка">
                  {card.subscription.name} <span className="text-muted-foreground">{card.subscription.panel_username}</span>
                </Row>
              )}
              {card.snapshot && (
                <Row label="Тариф">
                  {card.snapshot.name} · {card.snapshot.duration_days ?? '—'} дн. · устройств {card.snapshot.device_limit}
                </Row>
              )}
              <Row label="Сумма счёта">
                <span className="tabular">{formatMoney(payment.amount, payment.currency)}</span>
              </Row>
              {card.paid_amount && card.paid_currency && (
                <Row label="Подтверждено провайдером">
                  <span className={payment.amount_mismatch ? 'tabular text-warning-text' : 'tabular'}>
                    {formatMoney(card.paid_amount, card.paid_currency)}
                  </span>
                </Row>
              )}
              <Row label="Создан">{formatDateTime(payment.created_at, zone)}</Row>
              {card.expires_at && <Row label="Счёт действует до">{formatDateTime(card.expires_at, zone)}</Row>}
              {payment.paid_at && <Row label="Оплачен">{formatDateTime(payment.paid_at, zone)}</Row>}
              {card.applied_at && <Row label="Применён">{formatDateTime(card.applied_at, zone)}</Row>}
              {card.provider && (
                <Row label="Провайдер">
                  {card.provider} <span className="font-mono text-xs text-muted-foreground">{card.provider_payment_id}</span>
                </Row>
              )}
            </dl>
          </Module>
          <Actions card={card} />
        </div>
        <div className="flex flex-col gap-4">
          <Module title="Попытки применения">
            {card.last_error && (
              <p className="mb-3 break-words rounded-lg bg-danger-soft p-3 font-mono text-xs text-danger-text">
                {card.last_error}
              </p>
            )}
            {card.attempts.length === 0 ? (
              <p className="text-sm text-muted-foreground">Попыток не было.</p>
            ) : (
              <ol className="divide-y text-sm">
                {card.attempts.map((attempt) => (
                  <li key={`${attempt.retry_round}-${attempt.number}`} className="flex flex-col gap-1 py-2">
                    <div className="flex flex-wrap justify-between gap-2">
                      <span>
                        Попытка {attempt.number}
                        {attempt.retry_round > 1 && ` (круг ${attempt.retry_round})`}
                        {attempt.result && ` — ${ATTEMPT_RESULTS[attempt.result]}`}
                      </span>
                      <time className="text-muted-foreground">{formatDateTime(attempt.started_at, zone)}</time>
                    </div>
                    {attempt.error && (
                      <p className="break-words font-mono text-xs text-muted-foreground">{attempt.error}</p>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </Module>
          <Module title="История">
            <ol className="divide-y text-sm">
              {card.history.map((entry, index) => (
                <li key={index} className="flex flex-col gap-0.5 py-2">
                  <div className="flex flex-wrap justify-between gap-2">
                    <span className={entry.outcome === 'failure' ? 'text-warning-text' : undefined}>
                      {historyName(entry.action)}
                    </span>
                    <time className="text-muted-foreground">{formatDateTime(entry.occurred_at, zone)}</time>
                  </div>
                  <span className="text-xs text-muted-foreground">
                    {actorName(entry.actor_type)}
                    {typeof entry.details.comment === 'string' && ` · «${entry.details.comment}»`}
                  </span>
                </li>
              ))}
            </ol>
          </Module>
        </div>
      </div>
    </>
  )
}
