import { useEffect, useState } from 'react'

import { $api, errorMessage } from '@/api/client'
import { Field } from '@/components/Field'
import { Module } from '@/components/frame/Module'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { whole } from '@/lib/format'

// Пределы — как в API (src/remnabay/promo/_entry.py)
const MAX_FAILED_ATTEMPTS = 100
const MAX_PAUSE_MINUTES = 1440

/** «Промокоды»: защита от перебора (8.13, 04-operator-settings). Неверной считается
 *  попытка, на которую клиент получил «нет или больше не действует». */
export function PromoSettingsPage() {
  const { data } = $api.useQuery('get', '/api/admin/settings/promo')
  const save = $api.useMutation('put', '/api/admin/settings/promo')
  const [form, setForm] = useState<{ attempts: string; pause: string } | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (data && form === null) {
      setForm({ attempts: String(data.max_failed_attempts), pause: String(data.pause_minutes) })
    }
  }, [data, form])

  if (!form) return null
  const attempts = whole(form.attempts, 1, MAX_FAILED_ATTEMPTS)
  const pause = whole(form.pause, 1, MAX_PAUSE_MINUTES)
  const change = (patch: Partial<typeof form>) => {
    setSaved(false)
    setForm({ ...form, ...patch })
  }
  const submit = () => {
    if (attempts === null || pause === null) return
    save.mutate(
      { body: { max_failed_attempts: attempts, pause_minutes: pause } },
      { onSuccess: () => setSaved(true) },
    )
  }
  return (
    <Module title="Защита от перебора" className="max-w-xl">
      <form
        className="flex flex-col gap-5"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <p className="text-sm text-muted-foreground">
          Если клиент несколько раз подряд вводит код, которого нет или который больше не действует, бот на время
          перестаёт принимать от него промокоды. Удачный ввод сбрасывает счётчик.
        </p>
        <div className="grid gap-5 sm:grid-cols-2">
          <Field id="promo-attempts" label="Неверных кодов подряд" hint={`От 1 до ${MAX_FAILED_ATTEMPTS}. По умолчанию — 5.`}>
            <Input
              id="promo-attempts"
              inputMode="numeric"
              className="tabular w-32"
              value={form.attempts}
              aria-invalid={attempts === null}
              onChange={(e) => change({ attempts: e.target.value })}
            />
          </Field>
          <Field id="promo-pause" label="Пауза, минут" hint={`От 1 до ${MAX_PAUSE_MINUTES}. По умолчанию — 15.`}>
            <Input
              id="promo-pause"
              inputMode="numeric"
              className="tabular w-32"
              value={form.pause}
              aria-invalid={pause === null}
              onChange={(e) => change({ pause: e.target.value })}
            />
          </Field>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={attempts === null || pause === null || save.isPending}>
            Сохранить
          </Button>
          {save.error && <p className="text-sm text-danger-text">{errorMessage(save.error, 'Проверьте значения')}</p>}
          {saved && !save.error && <p className="text-sm text-muted-foreground">Сохранено</p>}
        </div>
      </form>
    </Module>
  )
}
