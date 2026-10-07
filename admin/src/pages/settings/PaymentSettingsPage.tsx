import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { $api, errorMessage, type Schemas } from '@/api/client'
import { CopyField } from '@/components/CopyField'
import { Field } from '@/components/Field'
import { Module } from '@/components/frame/Module'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

type Provider = Schemas['ProviderOut']

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
  const disconnect = $api.useMutation('delete', '/api/admin/settings/payment/providers/{code}', {
    onSuccess: refresh,
  })
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
            <Button
              variant="ghost"
              onClick={() => disconnect.mutate({ params: { path: { code: provider.code } } })}
              disabled={disconnect.isPending}
            >
              Отключить
            </Button>
          )}
          {connect.error && (
            <p className="text-sm text-danger-text">{errorMessage(connect.error, 'Ключи не подошли')}</p>
          )}
        </div>
      </div>
    </Module>
  )
}

/** А11 «Оплата»: подключённые провайдеры и время жизни счёта (3.8, 3.31, 3.33). */
export function PaymentSettingsPage() {
  const { data } = $api.useQuery('get', '/api/admin/settings/payment')
  const save = $api.useMutation('put', '/api/admin/settings/payment')
  const [lifetime, setLifetime] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (data && lifetime === null) setLifetime(String(data.invoice_lifetime_minutes))
  }, [data, lifetime])

  if (!data || lifetime === null) return null
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {data.providers.map((provider) => (
        <YooKassaModule key={provider.code} provider={provider} currency={data.currency} />
      ))}
      <Module title="Счёт">
        <div className="flex flex-col gap-4">
          <Field
            id="invoice-lifetime"
            label="Время жизни счёта, минут"
            hint="От 5 до 1440. Если клиент заплатит позже, оплата всё равно применится."
          >
            <Input
              id="invoice-lifetime"
              type="number"
              min={5}
              max={1440}
              className="tabular w-32"
              value={lifetime}
              onChange={(event) => {
                setLifetime(event.target.value)
                setSaved(false)
              }}
            />
          </Field>
          <div className="flex flex-wrap items-center gap-3">
            <Button
              onClick={() =>
                save.mutate(
                  { body: { invoice_lifetime_minutes: Number(lifetime) } },
                  { onSuccess: () => setSaved(true) },
                )
              }
              disabled={save.isPending}
            >
              Сохранить
            </Button>
            {save.error && <p className="text-sm text-danger-text">{errorMessage(save.error, 'Проверьте значение')}</p>}
            {saved && !save.error && <p className="text-sm text-muted-foreground">Сохранено</p>}
          </div>
        </div>
      </Module>
    </div>
  )
}
