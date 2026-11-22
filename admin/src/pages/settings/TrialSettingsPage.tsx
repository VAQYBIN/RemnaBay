import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { $api, errorMessage } from '@/api/client'
import { Field } from '@/components/Field'
import { Module } from '@/components/frame/Module'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { whole } from '@/lib/format'
import { cn } from '@/lib/utils'

type Form = { enabled: boolean; days: string; traffic: string; devices: string; squads: string[] }

/** «Триал» (5.1): включён ли и параметры — срок, лимит трафика (0 — без ограничения),
 *  лимит устройств, сквады. Трафик триала не сбрасывается: лимит — на весь пробный период. */
export function TrialSettingsPage() {
  const queryClient = useQueryClient()
  const { data } = $api.useQuery('get', '/api/admin/settings/trial')
  const squadsQuery = $api.useQuery('get', '/api/admin/panel/squads', {}, { retry: false, staleTime: 60_000 })
  const save = $api.useMutation('put', '/api/admin/settings/trial', {
    onSuccess: () => {
      setSaved(true)
      void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/checklist'] })
    },
  })
  const [form, setForm] = useState<Form | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (data && form === null) {
      setForm({
        enabled: data.enabled,
        days: String(data.duration_days),
        traffic: String(data.traffic_limit_gb),
        devices: String(data.device_limit),
        squads: data.squad_uuids,
      })
    }
  }, [data, form])

  if (!form) return null
  const change = (patch: Partial<Form>) => {
    setSaved(false)
    setForm({ ...form, ...patch })
  }
  const days = whole(form.days, 1, 365)
  const traffic = whole(form.traffic, 0, 100_000)
  const devices = whole(form.devices, 1, 100)
  const squads = squadsQuery.data
  const unknown = form.squads.filter((uuid) => !squads?.some((squad) => squad.uuid === uuid))
  const needsSquad = form.enabled && form.squads.length === 0
  const valid = days !== null && traffic !== null && devices !== null && !needsSquad
  const toggle = (uuid: string, checked: boolean) =>
    change({ squads: checked ? [...form.squads, uuid] : form.squads.filter((other) => other !== uuid) })
  const submit = () => {
    if (!valid) return
    save.mutate({
      body: {
        enabled: form.enabled,
        duration_days: days,
        traffic_limit_gb: traffic,
        device_limit: devices,
        squad_uuids: form.squads,
      },
    })
  }

  return (
    <Module title="Триал" className="max-w-2xl">
      <form
        className="flex flex-col gap-5"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <label className="flex items-start gap-3 text-sm">
          <Checkbox checked={form.enabled} onCheckedChange={(checked) => change({ enabled: checked === true })} />
          <span>
            <span className="font-medium">Триал включён</span>
            <span className="block text-muted-foreground">
              Новые клиенты, которые ещё не платили и не получали триал, увидят «Попробовать бесплатно» в
              главном меню и под тарифами.
            </span>
          </span>
        </label>
        <div className="grid gap-5 sm:grid-cols-3">
          <Field id="trial-days" label="Срок, дней" hint="От 1 до 365.">
            <Input
              id="trial-days"
              inputMode="numeric"
              className="tabular"
              value={form.days}
              aria-invalid={days === null}
              onChange={(e) => change({ days: e.target.value })}
            />
          </Field>
          <Field id="trial-traffic" label="Трафик, ГБ" hint="0 — без ограничения. Не сбрасывается.">
            <Input
              id="trial-traffic"
              inputMode="numeric"
              className="tabular"
              value={form.traffic}
              aria-invalid={traffic === null}
              onChange={(e) => change({ traffic: e.target.value })}
            />
          </Field>
          <Field id="trial-devices" label="Лимит устройств" hint="Не меньше 1.">
            <Input
              id="trial-devices"
              inputMode="numeric"
              className="tabular"
              value={form.devices}
              aria-invalid={devices === null}
              onChange={(e) => change({ devices: e.target.value })}
            />
          </Field>
        </div>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1.5 text-sm font-medium">Сквады</legend>
          {squadsQuery.isError && (
            <Alert className="border-warning-text/40 bg-warning-soft">
              <AlertDescription className="text-foreground">
                Панель недоступна — список сквадов не получить. Сохранить можно только с уже выбранными сквадами.
              </AlertDescription>
            </Alert>
          )}
          {squads?.length === 0 && <p className="text-sm text-muted-foreground">В панели нет внутренних сквадов.</p>}
          {squads?.map((squad) => (
            <label key={squad.uuid} className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={form.squads.includes(squad.uuid)}
                onCheckedChange={(checked) => toggle(squad.uuid, checked === true)}
              />
              {squad.name}
            </label>
          ))}
          {unknown.map((uuid) => (
            <label key={uuid} className="flex items-center gap-2 text-sm">
              <Checkbox checked onCheckedChange={() => toggle(uuid, false)} />
              <span className={cn('break-all', squads && 'text-warning-text')}>
                {squads ? 'Нет в панели' : 'Сквад'} <span className="font-mono text-xs">{uuid}</span>
              </span>
            </label>
          ))}
          <p className={cn('text-xs', needsSquad ? 'text-danger-text' : 'text-muted-foreground')}>
            Чтобы включить триал, выберите хотя бы один сквад — к нему клиент получит доступ на пробный период.
          </p>
        </fieldset>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={!valid || save.isPending}>
            Сохранить
          </Button>
          {save.error && <p className="text-sm text-danger-text">{errorMessage(save.error, 'Не удалось сохранить')}</p>}
          {saved && !save.error && <p className="text-sm text-muted-foreground">Сохранено</p>}
        </div>
      </form>
    </Module>
  )
}
