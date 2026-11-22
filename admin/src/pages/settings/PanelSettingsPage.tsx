import { useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { useState } from 'react'

import { $api, errorMessage } from '@/api/client'
import { CopyField } from '@/components/CopyField'
import { Field } from '@/components/Field'
import { Module } from '@/components/frame/Module'
import { PanelConnection } from '@/components/PanelConnection'
import { Alert, AlertDescription } from '@/components/ui/alert'
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
import { formatDateTime, whole } from '@/lib/format'
import { panelText } from '@/pages/home/Checklist'

// Пределы — как в API (src/remnabay/shop_settings.py)
const SYNC_MINUTES = [5, 1440] as const
const OUTAGE_ALERT_MINUTES = [1, 1440] as const

/** Состояние подключения (А11): на связи ли панель, её версия, последняя сверка и
 *  последнее событие — те же данные, что на главной. */
function ConnectionModule({ timeZone }: { timeZone: string }) {
  const overview = $api.useQuery('get', '/api/admin/overview', {}, { refetchInterval: 60_000 })
  const checklist = $api.useQuery('get', '/api/admin/checklist')
  const item = checklist.data?.items.find((candidate) => candidate.key === 'panel')
  if (!overview.data) return null
  const version = item ? (item.panel?.version ?? '—') : 'проверяется…'
  return (
    <Module title="Состояние подключения">
      <div className="flex flex-col gap-4">
        <PanelConnection overview={overview.data} timeZone={timeZone} version={version} />
        {item?.panel?.error && <p className="text-sm text-warning-text">{panelText(item)}</p>}
      </div>
    </Module>
  )
}

/** Интервал сверки с панелью (4.8) и уведомление о недоступности (4.13). Действуют сразу. */
function IntervalsModule({ sync, alert }: { sync: number; alert: number }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState({ sync: String(sync), alert: String(alert) })
  const [saved, setSaved] = useState(false)
  const save = $api.useMutation('put', '/api/admin/settings/panel/intervals', {
    onSuccess: () => {
      setSaved(true)
      void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/settings/panel'] })
    },
  })
  const syncMinutes = whole(form.sync, ...SYNC_MINUTES)
  const alertMinutes = whole(form.alert, ...OUTAGE_ALERT_MINUTES)
  const change = (patch: Partial<typeof form>) => {
    setSaved(false)
    setForm({ ...form, ...patch })
  }
  const submit = () => {
    if (syncMinutes === null || alertMinutes === null) return
    save.mutate({ body: { sync_interval_minutes: syncMinutes, outage_alert_minutes: alertMinutes } })
  }
  return (
    <Module title="Сверка и недоступность">
      <form
        className="flex flex-col gap-5"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <Field
          id="sync-interval"
          label="Интервал сверки с панелью, минут"
          hint={`От ${SYNC_MINUTES[0]} до ${SYNC_MINUTES[1]}. По умолчанию — 15. Сверка находит изменения, о которых панель не сообщила вебхуком.`}
        >
          <Input
            id="sync-interval"
            inputMode="numeric"
            className="tabular w-32"
            value={form.sync}
            aria-invalid={syncMinutes === null}
            onChange={(event) => change({ sync: event.target.value })}
          />
        </Field>
        <Field
          id="outage-alert"
          label="Уведомить о недоступности панели через, минут"
          hint={`От ${OUTAGE_ALERT_MINUTES[0]} до ${OUTAGE_ALERT_MINUTES[1]}. По умолчанию — 5. Одно уведомление на простой и ещё одно — когда связь вернётся.`}
        >
          <Input
            id="outage-alert"
            inputMode="numeric"
            className="tabular w-32"
            value={form.alert}
            aria-invalid={alertMinutes === null}
            onChange={(event) => change({ alert: event.target.value })}
          />
        </Field>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={syncMinutes === null || alertMinutes === null || save.isPending}>
            Сохранить
          </Button>
          {save.error && <p className="text-sm text-danger-text">{errorMessage(save.error, 'Проверьте значения')}</p>}
          {saved && !save.error && <p className="text-sm text-muted-foreground">Сохранено</p>}
        </div>
      </form>
    </Module>
  )
}

/** «Панель»: состояние подключения, адрес и секрет вебхука (1.9). Секрет — созданный
 *  магазином или свой, если в панели он уже задан, например для прежнего бота (решение
 *  0052). Интервал сверки (4.8) и уведомление о недоступности (4.13). Префикс имён
 *  пользователей в панели задаётся один раз (решение 0057). */
export function PanelSettingsPage() {
  const queryClient = useQueryClient()
  const { data } = $api.useQuery('get', '/api/admin/settings/panel')
  const [own, setOwn] = useState('')
  const [confirmGenerate, setConfirmGenerate] = useState(false)
  const [saved, setSaved] = useState(false)
  const [prefix, setPrefix] = useState('')
  const [confirmPrefix, setConfirmPrefix] = useState(false)
  const refresh = () => {
    setSaved(true)
    void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/settings/panel'] })
    void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/checklist'] })
  }
  const save = $api.useMutation('put', '/api/admin/settings/panel/webhook-secret', {
    onSuccess: () => {
      setOwn('')
      refresh()
    },
  })
  const generate = $api.useMutation('post', '/api/admin/settings/panel/webhook-secret/generate', {
    onSuccess: () => {
      setConfirmGenerate(false)
      refresh()
    },
  })
  const savePrefix = $api.useMutation('put', '/api/admin/settings/panel/username-prefix', {
    onSuccess: () => {
      setConfirmPrefix(false)
      setPrefix('')
      refresh()
    },
  })
  const refreshGuide = $api.useMutation('post', '/api/admin/settings/panel/guide/refresh', {
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/settings/panel'] }),
  })
  const shop = $api.useQuery('get', '/api/admin/settings/shop')
  if (!data) return null
  const timeZone = shop.data?.time_zone ?? 'Europe/Moscow'
  const ownValid = /^[A-Za-z0-9]{32,256}$/.test(own.trim())
  const prefixValid = /^[A-Za-z0-9-]{1,16}$/.test(prefix.trim())

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <ConnectionModule timeZone={timeZone} />
      <IntervalsModule sync={data.sync_interval_minutes} alert={data.outage_alert_minutes} />
      <Module title="Вебхук панели">
        <div className="flex flex-col gap-4">
          <p className="text-sm text-muted-foreground">
            Укажите в панели адрес и секрет: в .env панели это WEBHOOK_URL и WEBHOOK_SECRET_HEADER.
          </p>
          <CopyField label="Адрес" value={data.webhook_url} />
          {data.webhook_secret && !data.webhook_secret_fits_panel && (
            <p className="text-sm text-warning-text">
              Этот секрет панель не примет: нужны только латинские буквы и цифры, не короче 32 символов.
              Создайте новый или укажите свой.
            </p>
          )}
          {data.webhook_secret ? (
            <CopyField label="Секрет" value={data.webhook_secret} />
          ) : (
            <p className="text-sm text-danger-text">
              Секрет не расшифровывается — сменился ключ шифрования. Задайте секрет заново.
            </p>
          )}
          <Button variant="outline" className="w-fit" onClick={() => setConfirmGenerate(true)}>
            <RefreshCw />
            Создать новый секрет
          </Button>
        </div>
      </Module>

      <Module title="Инструкция по подключению">
        <div className="flex flex-col gap-4">
          <p className="text-sm text-muted-foreground">
            Бот собирает инструкцию из конфига Subscription Page, назначенного подписке в панели, и обновляет
            его раз в час. Править инструкцию нужно в панели — после правки нажмите «Обновить сейчас».
          </p>
          {data.webpage_allowed === false && (
            <Alert className="border-warning-text/40 bg-warning-soft">
              <AlertTriangle className="text-warning-text" />
              <AlertDescription className="text-foreground">
                Панель не пускает браузер на страницу подписки, поэтому клиенты не видят кнопку «Открыть в
                браузере»: по ссылке подписки браузер получил бы ошибку. Если страница подписки нужна клиентам,
                разрешите её браузерам в правилах ответов панели.
              </AlertDescription>
            </Alert>
          )}
          <p className="text-sm">
            {data.guide_refreshed_at
              ? `Конфиг получен ${formatDateTime(data.guide_refreshed_at, timeZone)}.`
              : 'Конфиг ещё не получен — бот получит его при первом открытии инструкции.'}
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <Button
              variant="outline"
              className="w-fit"
              onClick={() => refreshGuide.mutate({})}
              disabled={refreshGuide.isPending}
            >
              <RefreshCw />
              Обновить сейчас
            </Button>
            {refreshGuide.isError && (
              <p className="text-sm text-danger-text">
                {errorMessage(refreshGuide.error as unknown, 'Не удалось обновить')}
              </p>
            )}
            {refreshGuide.isSuccess && <p className="text-sm text-muted-foreground">Обновлено</p>}
          </div>
        </div>
      </Module>

      <Module title="Свой секрет">
        <div className="flex flex-col gap-4">
          <Field
            id="own-secret"
            label="Секрет, уже заданный в панели"
            hint="Если панель уже отправляет вебхуки с секретом, например прежнему боту, вставьте его сюда — менять настройки панели не придётся. Только латинские буквы и цифры, не короче 32 символов: так требует панель."
          >
            <Input
              id="own-secret"
              value={own}
              onChange={(event) => {
                setOwn(event.target.value)
                setSaved(false)
              }}
              className="font-mono"
              autoComplete="off"
              spellCheck={false}
              aria-invalid={own !== '' && !ownValid}
            />
          </Field>
          <div className="flex flex-wrap items-center gap-3">
            <Button
              onClick={() => save.mutate({ body: { webhook_secret: own.trim() } })}
              disabled={!ownValid || save.isPending}
            >
              Сохранить секрет
            </Button>
            {save.error && <p className="text-sm text-danger-text">{errorMessage(save.error, 'Секрет не подходит')}</p>}
            {saved && !save.error && <p className="text-sm text-muted-foreground">Сохранено</p>}
          </div>
        </div>
      </Module>

      <Module title="Имена пользователей в панели">
        <div className="flex flex-col gap-4">
          <p className="text-sm text-muted-foreground">
            Пользователь, которого магазин создаёт в панели при покупке, получает имя{' '}
            <span className="font-mono text-foreground">
              {data.username_prefix}_&lt;Telegram ID&gt;_1
            </span>
            ; у следующих подписок клиента номер растёт: _2, _3.
          </p>
          {data.username_prefix_locked ? (
            <p className="text-sm text-muted-foreground">
              Префикс <span className="font-mono text-foreground">{data.username_prefix}</span>{' '}
              зафиксирован и больше не меняется — так имена в панели не смешиваются.
            </p>
          ) : (
            <>
              <Field
                id="username-prefix"
                label="Префикс"
                hint="Латинские буквы, цифры и дефис, до 16 символов. Задаётся один раз: после сохранения или первой покупки изменить его нельзя."
              >
                <Input
                  id="username-prefix"
                  value={prefix}
                  placeholder={data.username_prefix}
                  onChange={(event) => setPrefix(event.target.value)}
                  className="font-mono"
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={prefix !== '' && !prefixValid}
                />
              </Field>
              <div className="flex flex-wrap items-center gap-3">
                <Button onClick={() => setConfirmPrefix(true)} disabled={!prefixValid}>
                  Сохранить префикс
                </Button>
                {savePrefix.error && (
                  <p className="text-sm text-danger-text">
                    {errorMessage(savePrefix.error, 'Префикс не подходит')}
                  </p>
                )}
              </div>
            </>
          )}
        </div>
      </Module>

      <Dialog open={confirmPrefix} onOpenChange={setConfirmPrefix}>
        <DialogContent className="glass-float">
          <DialogHeader>
            <DialogTitle>Сохранить префикс «{prefix.trim()}»?</DialogTitle>
            <DialogDescription>
              Изменить префикс потом будет нельзя: все пользователи, которых магазин создаст в панели,
              получат имена вида {prefix.trim()}_&lt;Telegram ID&gt;_1.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="ghost">Отмена</Button>
            </DialogClose>
            <Button
              onClick={() => savePrefix.mutate({ body: { username_prefix: prefix.trim() } })}
              disabled={savePrefix.isPending}
            >
              Сохранить навсегда
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmGenerate} onOpenChange={setConfirmGenerate}>
        <DialogContent className="glass-float">
          <DialogHeader>
            <DialogTitle>Создать новый секрет?</DialogTitle>
            <DialogDescription>
              Прежний секрет перестанет действовать: вебхуки, подписанные им, магазин будет отклонять,
              пока вы не укажете новый секрет в панели. Пропущенные события найдёт сверка с панелью.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="ghost">Отмена</Button>
            </DialogClose>
            <Button onClick={() => generate.mutate({})} disabled={generate.isPending}>
              Создать
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
