import { Plus, X } from 'lucide-react'
import { useEffect, useState } from 'react'

import { $api, errorMessage } from '@/api/client'
import { Module } from '@/components/frame/Module'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'

/** В каждом расписании — до 10 напоминаний, от 1 до 720 часов до или после окончания (0061). */
const MAX_REMINDERS = 10
const MAX_HOURS = 720

type Row = { key: number; hours: string; after: boolean }
type Form = { paid: Row[]; trial: Row[] }
type Kind = keyof Form

let nextKey = 0

/** Смещение API: часы до окончания, после окончания — отрицательные. */
function toRows(offsets: number[]): Row[] {
  return offsets.map((offset) => ({ key: nextKey++, hours: String(Math.abs(offset)), after: offset < 0 }))
}

function hoursOf(row: Row): number | null {
  if (!/^\d+$/.test(row.hours.trim())) return null
  const value = Number(row.hours.trim())
  return value >= 1 && value <= MAX_HOURS ? value : null
}

function toOffsets(rows: Row[]): number[] | null {
  const offsets: number[] = []
  for (const row of rows) {
    const hours = hoursOf(row)
    if (hours === null) return null
    offsets.push(row.after ? -hours : hours)
  }
  return offsets
}

function hasRepeats(rows: Row[]): boolean {
  const offsets = toOffsets(rows)
  return offsets !== null && new Set(offsets).size < offsets.length
}

const SCHEDULES: { kind: Kind; title: string; atEnd: string; note: string }[] = [
  {
    kind: 'paid',
    title: 'Платные подписки',
    atEnd: 'В момент окончания клиент получает «Подписка истекла» с кнопкой «Продлить».',
    note: 'В напоминании — название подписки, дата окончания и «Продлить». Если тариф в архиве, напоминание до окончания предупредит, что потом придётся выбрать новый.',
  },
  {
    kind: 'trial',
    title: 'Триал',
    atEnd: 'В момент окончания клиент получает «Пробный период закончился» с кнопкой «Купить подписку».',
    note: 'У триала свои тексты: до окончания и после него. Кнопка — «Купить подписку».',
  },
]

/** «Напоминания» (7.1–7.3): расписания для платных подписок и для триала. */
export function ReminderSettingsPage() {
  const { data } = $api.useQuery('get', '/api/admin/settings/reminders')
  const save = $api.useMutation('put', '/api/admin/settings/reminders', {
    onSuccess: (saved) => {
      // Сервер схлопывает повторы и упорядочивает по моментам — форма показывает итог
      setForm({ paid: toRows(saved.paid), trial: toRows(saved.trial) })
      setSaved(true)
    },
  })
  const [form, setForm] = useState<Form | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (data && form === null) setForm({ paid: toRows(data.paid), trial: toRows(data.trial) })
  }, [data, form])

  if (!form) return null
  const change = (kind: Kind, rows: Row[]) => {
    setSaved(false)
    setForm({ ...form, [kind]: rows })
  }
  const update = (kind: Kind, key: number, patch: Partial<Row>) =>
    change(
      kind,
      form[kind].map((row) => (row.key === key ? { ...row, ...patch } : row)),
    )
  const paid = toOffsets(form.paid)
  const trial = toOffsets(form.trial)
  const valid = paid !== null && trial !== null

  return (
    <Module title="Напоминания" className="max-w-2xl">
      <form
        className="flex flex-col gap-6"
        onSubmit={(event) => {
          event.preventDefault()
          if (paid !== null && trial !== null) save.mutate({ body: { paid, trial } })
        }}
      >
        <p className="text-sm text-muted-foreground">
          Напоминание приходит ровно за столько часов до окончания подписки или через столько часов после.
          Изменения действуют сразу: уже запланированные напоминания пересчитываются. Если момент нового
          напоминания уже прошёл, оно не отправляется.
        </p>
        {SCHEDULES.map(({ kind, title, atEnd, note }) => (
          <fieldset key={kind} className="flex flex-col gap-3">
            <legend className="mb-1.5 text-sm font-medium">{title}</legend>
            {form[kind].length === 0 && (
              <p className="text-sm text-muted-foreground">Напоминаний нет.</p>
            )}
            {form[kind].map((row, index) => {
              const invalid = hoursOf(row) === null
              const id = `${kind}-${row.key}`
              return (
                <div key={row.key} className="flex flex-wrap items-center gap-2">
                  <label htmlFor={id} className="sr-only">
                    Напоминание {index + 1}, часов
                  </label>
                  <Input
                    id={id}
                    inputMode="numeric"
                    className="tabular w-20"
                    value={row.hours}
                    aria-invalid={invalid}
                    onChange={(e) => update(kind, row.key, { hours: e.target.value })}
                  />
                  <span className="text-sm text-muted-foreground">ч</span>
                  <ToggleGroup
                    type="single"
                    value={row.after ? 'after' : 'before'}
                    onValueChange={(value) => value && update(kind, row.key, { after: value === 'after' })}
                    aria-label={`Напоминание ${index + 1}: до или после окончания`}
                    variant="outline"
                    size="sm"
                    spacing={0}
                  >
                    <ToggleGroupItem value="before">до окончания</ToggleGroupItem>
                    <ToggleGroupItem value="after">после окончания</ToggleGroupItem>
                  </ToggleGroup>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Убрать напоминание ${index + 1}`}
                    onClick={() =>
                      change(
                        kind,
                        form[kind].filter((other) => other.key !== row.key),
                      )
                    }
                  >
                    <X />
                  </Button>
                </div>
              )
            })}
            <div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={form[kind].length >= MAX_REMINDERS}
                onClick={() => change(kind, [...form[kind], { key: nextKey++, hours: '24', after: false }])}
              >
                <Plus data-icon="inline-start" />
                Добавить напоминание
              </Button>
            </div>
            <p
              className={cn(
                'text-xs',
                form[kind].some((row) => hoursOf(row) === null) ? 'text-danger-text' : 'text-muted-foreground',
              )}
            >
              От 1 до {MAX_HOURS} часов (30 дней), не больше {MAX_REMINDERS} напоминаний.
              {hasRepeats(form[kind]) && ' Одинаковые напоминания сольются в одно при сохранении.'}
            </p>
            <p className="text-sm">{atEnd}</p>
            <p className="text-xs text-muted-foreground">{note}</p>
          </fieldset>
        ))}
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
