import { useEffect, useState } from 'react'

import { $api, errorMessage } from '@/api/client'
import { Field } from '@/components/Field'
import { Module } from '@/components/frame/Module'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

/** «Вход в админку»: срок действия подтверждения входа (1.5) и срок сессии. */
export function LoginSettingsPage() {
  const { data } = $api.useQuery('get', '/api/admin/settings/login')
  const save = $api.useMutation('put', '/api/admin/settings/login')
  const [form, setForm] = useState<{ login: string; session: string } | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (data && form === null) {
      setForm({ login: String(data.login_ttl_minutes), session: String(data.session_ttl_days) })
    }
  }, [data, form])

  if (!form) return null
  const submit = () => {
    setSaved(false)
    save.mutate(
      { body: { login_ttl_minutes: Number(form.login), session_ttl_days: Number(form.session) } },
      { onSuccess: () => setSaved(true) },
    )
  }
  return (
    <Module title="Вход в админку" className="max-w-xl">
      <div className="flex flex-col gap-5">
        <Field id="login-ttl" label="Срок действия подтверждения входа, минут" hint="От 1 до 60. По умолчанию — 5.">
          <Input id="login-ttl" type="number" min={1} max={60} className="tabular w-32" value={form.login} onChange={(e) => setForm({ ...form, login: e.target.value })} />
        </Field>
        <Field id="session-ttl" label="Срок сессии, дней" hint="От 1 до 365. Отзыв доступа завершает сессию сразу.">
          <Input id="session-ttl" type="number" min={1} max={365} className="tabular w-32" value={form.session} onChange={(e) => setForm({ ...form, session: e.target.value })} />
        </Field>
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={submit} disabled={save.isPending}>
            Сохранить
          </Button>
          {save.error && <p className="text-sm text-danger-text">{errorMessage(save.error, 'Проверьте значения')}</p>}
          {saved && <p className="text-sm text-muted-foreground">Сохранено</p>}
        </div>
      </div>
    </Module>
  )
}
