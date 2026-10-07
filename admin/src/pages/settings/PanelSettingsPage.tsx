import { useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { useState } from 'react'

import { $api, errorMessage } from '@/api/client'
import { CopyField } from '@/components/CopyField'
import { Field } from '@/components/Field'
import { Module } from '@/components/frame/Module'
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

/** «Панель»: адрес и секрет вебхука (1.9). Секрет — созданный магазином или свой,
 *  если в панели он уже задан, например для прежнего бота (решение 0052). Префикс имён
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
  if (!data) return null
  const ownValid = /^[A-Za-z0-9]{32,256}$/.test(own.trim())
  const prefixValid = /^[A-Za-z0-9-]{1,16}$/.test(prefix.trim())

  return (
    <div className="grid gap-4 lg:grid-cols-2">
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
