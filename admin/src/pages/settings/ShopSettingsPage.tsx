import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'

import { $api, errorMessage } from '@/api/client'
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
import { Textarea } from '@/components/ui/textarea'

function parseTesters(text: string): number[] | null {
  const parts = text.split(/[\s,;]+/).filter(Boolean)
  const ids = parts.map((part) => Number(part))
  return ids.every((id) => Number.isInteger(id) && id > 0) ? ids : null
}

/** Закрыть открытый магазин (1.22) — с описанием последствий. */
function CloseShop() {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  // Состояние — из сводки: она не ждёт ответа панели, в отличие от чек-листа
  const overview = $api.useQuery('get', '/api/admin/overview')
  const close = $api.useMutation('post', '/api/admin/shop/close', {
    onSuccess: () => {
      setOpen(false)
      void queryClient.invalidateQueries()
    },
  })
  const state = overview.data?.state
  return (
    <Module title="Магазин открыт или закрыт">
      <div className="flex flex-col gap-3 text-sm">
        {state === 'open' && <p>Магазин открыт для клиентов.</p>}
        {state === 'paused' && <p>Магазин временно закрыт. Открыть снова можно на главной.</p>}
        {state === 'not_opened' && <p>Магазин ещё не открыт. Открыть его можно на главной, когда выполнены обязательные пункты.</p>}
        {state === 'open' && (
          <Button variant="destructive" className="w-fit" onClick={() => setOpen(true)}>
            Закрыть магазин
          </Button>
        )}
      </div>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="glass-float">
          <DialogHeader>
            <DialogTitle>Закрыть магазин?</DialogTitle>
            <DialogDescription>
              Новые покупки, продления и триалы станут недоступны, клиенты увидят «Магазин временно закрыт».
              Свои подписки, ссылки и инструкции они видят по-прежнему, уже пришедшие оплаты применятся как
              обычно, промокоды на дни работают. Напоминания с предложением купить не отправляются. Для
              команды и тестировщиков бот работает как обычно.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="ghost">Отмена</Button>
            </DialogClose>
            <Button variant="destructive" onClick={() => close.mutate({})} disabled={close.isPending}>
              Закрыть
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Module>
  )
}

/** «Магазин»: контакт поддержки, часовой пояс (1.18), тестировщики (1.21), закрытие (1.22). */
export function ShopSettingsPage() {
  const queryClient = useQueryClient()
  const { data } = $api.useQuery('get', '/api/admin/settings/shop')
  const save = $api.useMutation('put', '/api/admin/settings/shop')
  const [form, setForm] = useState<{ support: string; timeZone: string; testers: string } | null>(null)
  const [saved, setSaved] = useState(false)
  const zones = useMemo(() => Intl.supportedValuesOf('timeZone'), [])

  useEffect(() => {
    if (data && form === null) {
      setForm({ support: data.support_contact, timeZone: data.time_zone, testers: data.testers.join('\n') })
    }
  }, [data, form])

  if (!form) return null
  const testers = parseTesters(form.testers)
  const submit = () => {
    if (testers === null) return
    setSaved(false)
    save.mutate(
      { body: { support_contact: form.support, time_zone: form.timeZone, testers } },
      {
        onSuccess: (next) => {
          setForm({ support: next.support_contact, timeZone: next.time_zone, testers: next.testers.join('\n') })
          setSaved(true)
          void queryClient.invalidateQueries()
        },
      },
    )
  }

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Module title="Магазин">
        <div className="flex flex-col gap-5">
          <Field id="support" label="Контакт поддержки" hint="Например, @support. Пока пусто, кнопки «Поддержка» в боте нет.">
            <Input id="support" value={form.support} maxLength={256} onChange={(e) => setForm({ ...form, support: e.target.value })} />
          </Field>
          <Field id="time-zone" label="Часовой пояс магазина" hint="Периоды и даты в админке и запасной текст дат в боте.">
            <select
              id="time-zone"
              value={form.timeZone}
              onChange={(e) => setForm({ ...form, timeZone: e.target.value })}
              className="h-9 rounded-lg border bg-transparent px-3 text-sm"
            >
              {zones.map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </select>
          </Field>
          <Field
            id="testers"
            label="Тестировщики"
            hint={
              testers === null
                ? 'Только Telegram ID — числа, по одному в строке.'
                : 'Telegram ID, для которых бот работает, пока магазин закрыт. По одному в строке.'
            }
          >
            <Textarea
              id="testers"
              rows={4}
              className="tabular font-mono"
              value={form.testers}
              onChange={(e) => setForm({ ...form, testers: e.target.value })}
              aria-invalid={testers === null}
            />
          </Field>
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={submit} disabled={save.isPending || testers === null}>
              Сохранить
            </Button>
            {save.error && <p className="text-sm text-danger-text">{errorMessage(save.error, 'Не удалось сохранить')}</p>}
            {saved && <p className="text-sm text-muted-foreground">Сохранено</p>}
          </div>
        </div>
      </Module>
      <CloseShop />
    </div>
  )
}
