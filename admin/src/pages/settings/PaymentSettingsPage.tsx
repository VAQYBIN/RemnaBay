import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { $api, errorMessage, type Schemas } from '@/api/client'
import { CopyField } from '@/components/CopyField'
import { Field } from '@/components/Field'
import { Module } from '@/components/frame/Module'
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
import { formatDateTime, plural, whole } from '@/lib/format'

type Provider = Schemas['ProviderOut']
type PayableInvoices = Schemas['PayableInvoicesOut']

/** Сервер отказал: клиенты ещё могут оплатить счета провайдера, нужно подтверждение (3.42). */
function payableInvoices(error: unknown): PayableInvoices | null {
  if (typeof error !== 'object' || error === null || !('detail' in error)) return null
  const detail = (error as { detail: unknown }).detail
  if (typeof detail === 'object' && detail !== null && 'reason' in detail && detail.reason === 'payable_invoices') {
    return detail as PayableInvoices
  }
  return null
}

/** Предупреждение перед отключением провайдера, пока клиенты могут оплатить его счета (3.42). */
function DisconnectWarning({
  title,
  payable,
  pending,
  onConfirm,
  onClose,
}: {
  title: string
  payable: PayableInvoices | null
  pending: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  const count = payable?.count ?? 0
  return (
    <Dialog open={payable !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="glass-float">
        <DialogHeader>
          <DialogTitle>Отключить провайдера «{title}»?</DialogTitle>
          <DialogDescription>
            У клиентов {count} {plural(count, 'счёт', 'счёта', 'счетов')}, {plural(count, 'который', 'которые', 'которые')}{' '}
            ещё {plural(count, 'может', 'могут', 'могут')} быть {plural(count, 'оплачен', 'оплачены', 'оплачены')}. Если
            отключить провайдера сейчас, поздняя оплата не применится сама, пока ключи не введут снова.
            {payable && (
              <>
                {' '}
                Лучше подождать до <span className="tabular">{formatDateTime(payable.window_ends_at, payable.time_zone)}</span>
                .
              </>
            )}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Отмена</Button>
          </DialogClose>
          <Button variant="destructive" onClick={onConfirm} disabled={pending}>
            Отключить сейчас
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

const STATE: Record<Provider['state'], { label: string; tone: 'success' | 'warning' | 'muted' }> = {
  ready: { label: 'Подключена', tone: 'success' },
  not_connected: { label: 'Не подключена', tone: 'muted' },
  keys_lost: { label: 'Ключи нужно ввести заново', tone: 'warning' },
  currency_unsupported: { label: 'Не принимает валюту магазина', tone: 'warning' },
}

const TONE = {
  success: 'bg-success-soft text-success-text',
  warning: 'bg-warning-soft text-warning-text',
  muted: 'bg-muted text-muted-foreground',
} as const

/** Подключение ЮКассы: идентификатор магазина и секретный ключ из личного кабинета.
 *  Ключи проверяются у ЮКассы до сохранения и хранятся зашифрованными. */
function YooKassaModule({ provider, currency }: { provider: Provider; currency: string }) {
  const queryClient = useQueryClient()
  const [shopId, setShopId] = useState('')
  const [secret, setSecret] = useState('')
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/settings/payment'] })
    void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/checklist'] })
  }
  const connect = $api.useMutation('put', '/api/admin/settings/payment/providers/{code}', {
    onSuccess: () => {
      setShopId('')
      setSecret('')
      refresh()
    },
  })
  const [payable, setPayable] = useState<PayableInvoices | null>(null)
  const disconnect = $api.useMutation('delete', '/api/admin/settings/payment/providers/{code}', {
    onSuccess: () => {
      setPayable(null)
      refresh()
    },
    onError: (error) => {
      const found = payableInvoices(error)
      if (found) setPayable(found)
    },
  })
  const requestDisconnect = (confirmPayable: boolean) =>
    disconnect.mutate({ params: { path: { code: provider.code }, query: { confirm_payable: confirmPayable } } })
  const state = STATE[provider.state]
  const connected = provider.state !== 'not_connected'
  const valid = /^\d+$/.test(shopId.trim()) && secret.trim() !== ''

  return (
    <Module
      title={provider.title}
      action={<Badge className={TONE[state.tone]}>{state.label}</Badge>}
    >
      <div className="flex flex-col gap-4">
        {provider.state === 'keys_lost' && (
          <p className="text-sm text-warning-text">
            Ключи не расшифровываются — сменился ключ шифрования магазина. Введите их заново, пока
            клиенты не могут оплатить.
          </p>
        )}
        {provider.state === 'currency_unsupported' && (
          <p className="text-sm text-warning-text">
            {provider.title} принимает счета в {provider.currencies.join(', ')}, а валюта магазина —{' '}
            {currency}. Клиенты не смогут платить через этого провайдера.
          </p>
        )}
        <CopyField label="Адрес для уведомлений (HTTP-уведомления в личном кабинете)" value={provider.webhook_url} />
        <p className="text-sm text-muted-foreground">
          В личном кабинете ЮКассы включите уведомления о событиях payment.succeeded и payment.canceled
          на этот адрес. Если уведомление не дойдёт, магазин узнает об оплате, запрашивая статус счёта.
        </p>
        <Field id="yookassa-shop" label="Идентификатор магазина (shopId)">
          <Input
            id="yookassa-shop"
            inputMode="numeric"
            value={shopId}
            onChange={(event) => setShopId(event.target.value)}
            className="tabular"
            autoComplete="off"
          />
        </Field>
        <Field
          id="yookassa-secret"
          label="Секретный ключ"
          hint={connected ? 'Ключ сохранён и не показывается. Чтобы заменить, введите новый.' : undefined}
        >
          <Input
            id="yookassa-secret"
            type="password"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            className="font-mono"
            autoComplete="off"
          />
        </Field>
        <div className="flex flex-wrap items-center gap-3">
          <Button
            onClick={() =>
              connect.mutate({
                params: { path: { code: provider.code } },
                body: { credentials: { shop_id: shopId.trim(), secret_key: secret.trim() } },
              })
            }
            disabled={!valid || connect.isPending}
          >
            {connected ? 'Заменить ключи' : 'Подключить'}
          </Button>
          {connected && (
            <Button variant="ghost" onClick={() => requestDisconnect(false)} disabled={disconnect.isPending}>
              Отключить
            </Button>
          )}
          {connect.error && (
            <p className="text-sm text-danger-text">{errorMessage(connect.error, 'Ключи не подошли')}</p>
          )}
          {disconnect.error && !payableInvoices(disconnect.error) && (
            <p className="text-sm text-danger-text">{errorMessage(disconnect.error, 'Не удалось отключить')}</p>
          )}
        </div>
      </div>
      <DisconnectWarning
        title={provider.title}
        payable={payable}
        pending={disconnect.isPending}
        onConfirm={() => requestDisconnect(true)}
        onClose={() => setPayable(null)}
      />
    </Module>
  )
}

type PaymentSettings = Schemas['PaymentSettingsOut']

// Пределы — как в API (src/remnabay/web/admin/_payment_settings.py, src/remnabay/payments/_checkout.py,
// src/remnabay/shop_settings.py)
const LIFETIME_MINUTES = [5, 1440] as const
const POLL_MINUTES = [1, 60] as const
const POLL_AFTER_HOURS = [1, 168] as const
const RETRY_WINDOW_MINUTES = [5, 1440] as const
const RETRY_ATTEMPTS = [3, 100] as const

function SavedNote({ error, saved, fallback }: { error: unknown; saved: boolean; fallback: string }) {
  if (error) return <p className="text-sm text-danger-text">{errorMessage(error, fallback)}</p>
  return saved ? <p className="text-sm text-muted-foreground">Сохранено</p> : null
}

function NumberField({
  id,
  label,
  hint,
  value,
  invalid,
  onChange,
}: {
  id: string
  label: string
  hint: string
  value: string
  invalid: boolean
  onChange: (value: string) => void
}) {
  return (
    <Field id={id} label={label} hint={hint}>
      <Input
        id={id}
        inputMode="numeric"
        className="tabular w-32"
        value={value}
        aria-invalid={invalid}
        onChange={(event) => onChange(event.target.value)}
      />
    </Field>
  )
}

/** Счёт: время жизни (3.8) и опрос неоплаченных счетов (3.29). */
function InvoiceModule({ data }: { data: PaymentSettings }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    lifetime: String(data.invoice_lifetime_minutes),
    poll: String(data.poll_interval_minutes),
    after: String(data.poll_after_expiry_hours),
  })
  const [saved, setSaved] = useState(false)
  const save = $api.useMutation('put', '/api/admin/settings/payment', {
    onSuccess: () => {
      setSaved(true)
      void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/settings/payment'] })
    },
  })
  const lifetime = whole(form.lifetime, ...LIFETIME_MINUTES)
  const poll = whole(form.poll, ...POLL_MINUTES)
  const after = whole(form.after, ...POLL_AFTER_HOURS)
  const valid = lifetime !== null && poll !== null && after !== null
  const change = (patch: Partial<typeof form>) => {
    setSaved(false)
    setForm({ ...form, ...patch })
  }
  return (
    <Module title="Счёт">
      <form
        className="flex flex-col gap-5"
        onSubmit={(event) => {
          event.preventDefault()
          if (!valid) return
          save.mutate({
            body: { invoice_lifetime_minutes: lifetime, poll_interval_minutes: poll, poll_after_expiry_hours: after },
          })
        }}
      >
        <NumberField
          id="invoice-lifetime"
          label="Время жизни счёта, минут"
          hint={`От ${LIFETIME_MINUTES[0]} до ${LIFETIME_MINUTES[1]}. Если клиент заплатит позже, оплата всё равно применится.`}
          value={form.lifetime}
          invalid={lifetime === null}
          onChange={(value) => change({ lifetime: value })}
        />
        <p className="text-sm text-muted-foreground">
          Если уведомление провайдера об оплате не дойдёт, магазин сам спрашивает статус неоплаченного счёта — пока
          счёт действует и ещё какое-то время после.
        </p>
        <div className="grid gap-5 sm:grid-cols-2">
          <NumberField
            id="poll-interval"
            label="Спрашивать статус каждые, минут"
            hint={`От ${POLL_MINUTES[0]} до ${POLL_MINUTES[1]}. По умолчанию — 5.`}
            value={form.poll}
            invalid={poll === null}
            onChange={(value) => change({ poll: value })}
          />
          <NumberField
            id="poll-after"
            label="После истечения счёта ещё, часов"
            hint={`От ${POLL_AFTER_HOURS[0]} до ${POLL_AFTER_HOURS[1]}. По умолчанию — 24.`}
            value={form.after}
            invalid={after === null}
            onChange={(value) => change({ after: value })}
          />
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={!valid || save.isPending}>
            Сохранить
          </Button>
          <SavedNote error={save.error} saved={saved} fallback="Проверьте значения" />
        </div>
      </form>
    </Module>
  )
}

/** Повтор операций (4.14): прекращение по времени или по числу попыток — что раньше. */
function RetryModule({ data }: { data: PaymentSettings }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    window: String(data.retry_window_minutes),
    attempts: String(data.retry_max_attempts),
  })
  const [saved, setSaved] = useState(false)
  const save = $api.useMutation('put', '/api/admin/settings/payment/retry', {
    onSuccess: () => {
      setSaved(true)
      void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/settings/payment'] })
    },
  })
  const windowMinutes = whole(form.window, ...RETRY_WINDOW_MINUTES)
  const attempts = whole(form.attempts, ...RETRY_ATTEMPTS)
  const valid = windowMinutes !== null && attempts !== null
  const change = (patch: Partial<typeof form>) => {
    setSaved(false)
    setForm({ ...form, ...patch })
  }
  return (
    <Module title="Повтор операций">
      <form
        className="flex flex-col gap-5"
        onSubmit={(event) => {
          event.preventDefault()
          if (!valid) return
          save.mutate({ body: { retry_window_minutes: windowMinutes, retry_max_attempts: attempts } })
        }}
      >
        <p className="text-sm text-muted-foreground">
          Если операция не удалась — например, применение оплаты, — магазин повторяет её с нарастающими паузами.
          Повторы прекращаются по времени или по числу попыток — что наступит раньше; тогда операция попадает в
          «Требуют внимания», а команда получает уведомление.
        </p>
        <div className="grid gap-5 sm:grid-cols-2">
          <NumberField
            id="retry-window"
            label="Прекращать через, минут"
            hint={`От ${RETRY_WINDOW_MINUTES[0]} до ${RETRY_WINDOW_MINUTES[1]}. По умолчанию — 60.`}
            value={form.window}
            invalid={windowMinutes === null}
            onChange={(value) => change({ window: value })}
          />
          <NumberField
            id="retry-attempts"
            label="Или после попыток"
            hint={`От ${RETRY_ATTEMPTS[0]} до ${RETRY_ATTEMPTS[1]}. По умолчанию — 20.`}
            value={form.attempts}
            invalid={attempts === null}
            onChange={(value) => change({ attempts: value })}
          />
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={!valid || save.isPending}>
            Сохранить
          </Button>
          <SavedNote error={save.error} saved={saved} fallback="Проверьте значения" />
        </div>
      </form>
    </Module>
  )
}

/** А11 «Оплата»: подключённые провайдеры, время жизни счёта, опрос неоплаченных счетов
 *  и повтор операций (3.8, 3.29, 3.31, 3.33, 4.14). */
export function PaymentSettingsPage() {
  const { data } = $api.useQuery('get', '/api/admin/settings/payment')
  if (!data) return null
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {data.providers.map((provider) => (
        <YooKassaModule key={provider.code} provider={provider} currency={data.currency} />
      ))}
      <InvoiceModule data={data} />
      <RetryModule data={data} />
    </div>
  )
}
