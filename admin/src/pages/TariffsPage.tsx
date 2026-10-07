import { useQueryClient } from '@tanstack/react-query'
import {
  Archive,
  ArchiveRestore,
  ArrowDown,
  ArrowUp,
  EllipsisVertical,
  GripVertical,
  Pencil,
  Plus,
  Trash2,
} from 'lucide-react'
import { type ComponentProps, type DragEvent, type ReactNode, useState } from 'react'
import { Navigate, useOutletContext } from 'react-router'

import { $api, errorMessage, type Schemas } from '@/api/client'
import { Field } from '@/components/Field'
import { Module, PageTitle } from '@/components/frame/Module'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { formatMoney, plural } from '@/lib/format'
import { isOwner, type Member } from '@/lib/session'
import { cn } from '@/lib/utils'

type Tariff = Schemas['TariffOut']
type Squad = Schemas['SquadOut']

// Пределы — как в API (src/remnabay/web/admin/_tariffs.py)
const MAX_NAME_LENGTH = 128
const MAX_DESCRIPTION_LENGTH = 1000
const MAX_DURATION_DAYS = 36500
const MAX_DEVICE_LIMIT = 1000

const LAST_ON_SALE_WARNING =
  'Это последний тариф в продаже: новые клиенты не смогут ничего купить, пока в продаже не появится другой тариф.'

function daysText(days: number): string {
  return `${days} ${plural(days, 'день', 'дня', 'дней')}`
}

function devicesText(limit: number): string {
  return `${limit} ${plural(limit, 'устройство', 'устройства', 'устройств')}`
}

function squadNames(uuids: string[], squads: Squad[] | undefined): string {
  if (!squads) return plural(uuids.length, `${uuids.length} сквад`, `${uuids.length} сквада`, `${uuids.length} сквадов`)
  const byUuid = new Map(squads.map((squad) => [squad.uuid, squad.name]))
  return uuids.map((uuid) => byUuid.get(uuid) ?? 'нет в панели').join(', ')
}

/** Сервер отказал: тариф — последний в продаже, нужно подтверждение (2.9). */
function isLastOnSale(error: unknown): boolean {
  if (typeof error !== 'object' || error === null || !('detail' in error)) return false
  const detail = (error as { detail: unknown }).detail
  return typeof detail === 'object' && detail !== null && 'reason' in detail && detail.reason === 'last_on_sale'
}

function editable(tariff: Tariff): boolean {
  return tariff.type === 'term_unlimited'
}

function moved(ids: number[], id: number, to: number): number[] {
  const next = ids.filter((other) => other !== id)
  next.splice(Math.max(0, Math.min(to, next.length)), 0, id)
  return next
}

function sameOrder(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((id, index) => id === b[index])
}

/** Строка тарифа: название, срок, цена, лимит устройств, сквады (А6). */
function TariffRow({
  tariff,
  currency,
  squads,
  actions,
  handle,
  className,
  ...rowProps
}: {
  tariff: Tariff
  currency: string
  squads: Squad[] | undefined
  actions: ReactNode
  handle?: ReactNode
  className?: string
} & Omit<ComponentProps<'li'>, 'children'>) {
  const missing = squads !== undefined && tariff.squad_uuids.some((uuid) => !squads.some((squad) => squad.uuid === uuid))
  return (
    <li className={cn('flex items-start gap-2 py-3', className)} {...rowProps}>
      {handle}
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <p className="font-medium break-words">{tariff.name}</p>
        <p className="tabular text-sm text-muted-foreground">
          {tariff.duration_days !== null && `${daysText(tariff.duration_days)} · `}
          {formatMoney(tariff.price, currency)} · {devicesText(tariff.device_limit)}
        </p>
        <p className={cn('text-sm', missing ? 'text-warning-text' : 'text-muted-foreground')}>
          Сквады: {squadNames(tariff.squad_uuids, squads)}
        </p>
        {tariff.description && <p className="text-sm whitespace-pre-line break-words">{tariff.description}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-1">{actions}</div>
    </li>
  )
}

type FormState = { name: string; description: string; days: string; price: string; devices: string; squads: string[] }

function initialForm(tariff: Tariff | null): FormState {
  if (!tariff) return { name: '', description: '', days: '30', price: '', devices: '1', squads: [] }
  return {
    name: tariff.name,
    description: tariff.description,
    days: String(tariff.duration_days ?? ''),
    price: tariff.price,
    devices: String(tariff.device_limit),
    squads: tariff.squad_uuids,
  }
}

function wholeNumber(text: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(text.trim())) return null
  const value = Number(text)
  return value >= min && value <= max ? value : null
}

/** Цена в валюте магазина, до копейки, больше нуля. */
function priceValue(text: string): string | null {
  const normalized = text.trim().replace(/\s/g, '').replace(',', '.')
  if (!/^\d{1,12}(\.\d{1,2})?$/.test(normalized)) return null
  return Number(normalized) > 0 ? normalized : null
}

/** Форма тарифа «срок + безлимит» (2.1); сквады — из панели (2.2). */
function TariffForm({
  tariff,
  squads,
  squadsError,
  onDone,
}: {
  tariff: Tariff | null
  squads: Squad[] | undefined
  squadsError: boolean
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState(() => initialForm(tariff))
  const create = $api.useMutation('post', '/api/admin/tariffs')
  const update = $api.useMutation('put', '/api/admin/tariffs/{tariff_id}')
  const mutation = tariff ? update : create

  const name = form.name.trim()
  const days = wholeNumber(form.days, 1, MAX_DURATION_DAYS)
  const price = priceValue(form.price)
  const devices = wholeNumber(form.devices, 1, MAX_DEVICE_LIMIT)
  const valid = name !== '' && days !== null && price !== null && devices !== null && form.squads.length > 0
  // Выбранные сквады, которых нет в списке панели: удалены в панели или панель не ответила
  const unknown = form.squads.filter((uuid) => !squads?.some((squad) => squad.uuid === uuid))

  const toggle = (uuid: string, checked: boolean) =>
    setForm({ ...form, squads: checked ? [...form.squads, uuid] : form.squads.filter((other) => other !== uuid) })

  const submit = () => {
    if (!valid) return
    const body = {
      type: 'term_unlimited' as const,
      name,
      description: form.description.trim(),
      duration_days: days,
      price,
      device_limit: devices,
      squad_uuids: form.squads,
    }
    const done = {
      onSuccess: () => {
        void queryClient.invalidateQueries()
        onDone()
      },
    }
    if (tariff) update.mutate({ params: { path: { tariff_id: tariff.id } }, body }, done)
    else create.mutate({ body }, done)
  }

  return (
    <form
      className="flex flex-col gap-5"
      onSubmit={(event) => {
        event.preventDefault()
        submit()
      }}
    >
      <Field id="tariff-name" label="Название">
        <Input
          id="tariff-name"
          value={form.name}
          maxLength={MAX_NAME_LENGTH}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
      </Field>
      <Field id="tariff-description" label="Описание" hint="Клиент видит его при выборе тарифа.">
        <Textarea
          id="tariff-description"
          rows={3}
          value={form.description}
          maxLength={MAX_DESCRIPTION_LENGTH}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
        />
      </Field>
      <div className="grid gap-5 sm:grid-cols-3">
        <Field id="tariff-days" label="Срок, дней">
          <Input
            id="tariff-days"
            inputMode="numeric"
            className="tabular"
            value={form.days}
            aria-invalid={days === null}
            onChange={(e) => setForm({ ...form, days: e.target.value })}
          />
        </Field>
        <Field id="tariff-price" label="Цена" hint={price === null && form.price !== '' ? 'Больше нуля, до копейки.' : undefined}>
          <Input
            id="tariff-price"
            inputMode="decimal"
            className="tabular"
            value={form.price}
            aria-invalid={form.price !== '' && price === null}
            onChange={(e) => setForm({ ...form, price: e.target.value })}
          />
        </Field>
        <Field id="tariff-devices" label="Лимит устройств" hint="Не меньше 1.">
          <Input
            id="tariff-devices"
            inputMode="numeric"
            className="tabular"
            value={form.devices}
            aria-invalid={devices === null}
            onChange={(e) => setForm({ ...form, devices: e.target.value })}
          />
        </Field>
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1.5 text-sm font-medium">Сквады</legend>
        {squadsError && (
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
        <p className="text-xs text-muted-foreground">Хотя бы один сквад: к ним клиент получит доступ.</p>
      </fieldset>
      {mutation.error && <p className="text-sm text-danger-text">{errorMessage(mutation.error, 'Не удалось сохранить')}</p>}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="ghost">
            Отмена
          </Button>
        </DialogClose>
        <Button type="submit" disabled={!valid || mutation.isPending}>
          {tariff ? 'Сохранить' : 'Создать тариф'}
        </Button>
      </DialogFooter>
    </form>
  )
}

type Confirm = { kind: 'archive' | 'delete'; tariff: Tariff; last: boolean }

/** Подтверждение: архивация последнего тарифа в продаже (2.9), удаление (2.8). */
function ConfirmDialog({
  confirm,
  onClose,
  onLastOnSale,
}: {
  confirm: Confirm | null
  onClose: () => void
  onLastOnSale: () => void
}) {
  const queryClient = useQueryClient()
  const archive = $api.useMutation('post', '/api/admin/tariffs/{tariff_id}/archive')
  const remove = $api.useMutation('delete', '/api/admin/tariffs/{tariff_id}')
  const mutation = confirm?.kind === 'delete' ? remove : archive
  const done = {
    onSuccess: () => {
      void queryClient.invalidateQueries()
      onClose()
    },
  }
  const run = () => {
    if (!confirm) return
    const path = { tariff_id: confirm.tariff.id }
    if (confirm.kind === 'archive') archive.mutate({ params: { path }, body: { confirm_last: true } }, done)
    else
      remove.mutate(
        { params: { path, query: { confirm_last: confirm.last } } },
        {
          ...done,
          // Другие тарифы успели уйти из продажи — показать предупреждение (2.9)
          onError: (error) => {
            if (isLastOnSale(error)) {
              remove.reset()
              onLastOnSale()
            }
          },
        },
      )
  }
  const close = (open: boolean) => {
    if (open) return
    archive.reset()
    remove.reset()
    onClose()
  }

  return (
    <Dialog open={confirm !== null} onOpenChange={close}>
      <DialogContent className="glass-float">
        <DialogHeader>
          <DialogTitle>
            {confirm?.kind === 'delete' ? `Удалить тариф «${confirm.tariff.name}»?` : `Убрать «${confirm?.tariff.name}» в архив?`}
          </DialogTitle>
          <DialogDescription>
            {confirm?.kind === 'delete' && 'По тарифу ещё не было ни подписок, ни платежей. Удаление нельзя отменить. '}
            {confirm?.last && LAST_ON_SALE_WARNING}
          </DialogDescription>
        </DialogHeader>
        {mutation.error && <p className="text-sm text-danger-text">{errorMessage(mutation.error, 'Не получилось')}</p>}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Отмена</Button>
          </DialogClose>
          <Button variant="destructive" onClick={run} disabled={mutation.isPending}>
            {confirm?.kind === 'delete' ? 'Удалить' : 'В архив'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** А6. Тарифы: порядок показа клиентам, в продаже и в архиве (2.1–2.10). Только владелец. */
export function TariffsPage() {
  const member = useOutletContext<Member>()
  const queryClient = useQueryClient()
  const { data } = $api.useQuery('get', '/api/admin/tariffs')
  const squadsQuery = $api.useQuery('get', '/api/admin/panel/squads', {}, { retry: false, staleTime: 60_000 })
  const order = $api.useMutation('put', '/api/admin/tariffs/order')
  const archive = $api.useMutation('post', '/api/admin/tariffs/{tariff_id}/archive')
  const restore = $api.useMutation('post', '/api/admin/tariffs/{tariff_id}/restore')
  const [editing, setEditing] = useState<Tariff | 'new' | null>(null)
  const [confirm, setConfirm] = useState<Confirm | null>(null)
  // Порядок во время перетаскивания и до ответа сервера
  const [preview, setPreview] = useState<number[] | null>(null)
  const [dragged, setDragged] = useState<number | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  if (!isOwner(member)) return <Navigate to="/" replace />

  const tariffs = data?.tariffs ?? []
  const currency = data?.currency ?? 'RUB'
  const squads = squadsQuery.data
  const onSale = tariffs.filter((tariff) => tariff.state === 'on_sale')
  const archived = tariffs.filter((tariff) => tariff.state !== 'on_sale')
  const saved = onSale.map((tariff) => tariff.id)
  const ids = preview ?? saved
  const byId = new Map(onSale.map((tariff) => [tariff.id, tariff]))
  const shown = ids.map((id) => byId.get(id)).filter((tariff) => tariff !== undefined)

  const refresh = () => queryClient.invalidateQueries()

  const saveOrder = (next: number[]) => {
    if (sameOrder(next, saved)) {
      setPreview(null)
      return
    }
    setPreview(next)
    setActionError(null)
    order.mutate(
      { body: { tariff_ids: next } },
      {
        onError: (error) => setActionError(errorMessage(error, 'Не удалось сохранить порядок')),
        onSettled: async () => {
          await refresh()
          setPreview(null)
        },
      },
    )
  }

  const startArchive = (tariff: Tariff) => {
    const last = onSale.length === 1
    if (last) {
      setConfirm({ kind: 'archive', tariff, last })
      return
    }
    setActionError(null)
    archive.mutate(
      { params: { path: { tariff_id: tariff.id } }, body: { confirm_last: false } },
      {
        onSuccess: () => void refresh(),
        onError: (error) => {
          // Другие тарифы успели уйти из продажи — нужно подтверждение (2.9)
          if (isLastOnSale(error)) setConfirm({ kind: 'archive', tariff, last: true })
          else setActionError(errorMessage(error, 'Не удалось убрать в архив'))
        },
      },
    )
  }

  const startRestore = (tariff: Tariff) => {
    setActionError(null)
    restore.mutate(
      { params: { path: { tariff_id: tariff.id } } },
      {
        onSuccess: () => void refresh(),
        onError: (error) => setActionError(errorMessage(error, 'Не удалось вернуть в продажу')),
      },
    )
  }

  const menu = (tariff: Tariff) => (
    // Тарифы других типов (данные — MVP) в админке MVP не меняются и не возвращаются в продажу (2.3)
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={`Действия с тарифом ${tariff.name}`}>
          <EllipsisVertical />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="glass-float w-56">
        {editable(tariff) && (
          <DropdownMenuItem onSelect={() => setEditing(tariff)}>
            <Pencil />
            Изменить
          </DropdownMenuItem>
        )}
        {tariff.state === 'on_sale' ? (
          <DropdownMenuItem onSelect={() => startArchive(tariff)}>
            <Archive />В архив
          </DropdownMenuItem>
        ) : (
          tariff.state === 'archived' &&
          editable(tariff) && (
            <DropdownMenuItem onSelect={() => startRestore(tariff)}>
              <ArchiveRestore />
              Вернуть в продажу
            </DropdownMenuItem>
          )
        )}
        {tariff.in_use && (
          <DropdownMenuItem disabled className="whitespace-normal">
            <Trash2 />
            Удалить нельзя: по тарифу были подписки или платежи — только архив
          </DropdownMenuItem>
        )}
        {!tariff.in_use && (
          <DropdownMenuItem
            variant="destructive"
            onSelect={() =>
              setConfirm({ kind: 'delete', tariff, last: tariff.state === 'on_sale' && onSale.length === 1 })
            }
          >
            <Trash2 />
            Удалить
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )

  const dragProps = (tariff: Tariff, index: number) => ({
    draggable: true,
    onDragStart: (event: DragEvent<HTMLLIElement>) => {
      event.dataTransfer.effectAllowed = 'move'
      event.dataTransfer.setData('text/plain', String(tariff.id))
      setDragged(tariff.id)
      setPreview(ids)
    },
    onDragOver: (event: DragEvent<HTMLLIElement>) => {
      if (dragged === null) return
      event.preventDefault()
      event.dataTransfer.dropEffect = 'move'
      if (dragged !== tariff.id) setPreview(moved(ids, dragged, index))
    },
    onDrop: (event: DragEvent<HTMLLIElement>) => event.preventDefault(),
    onDragEnd: (event: DragEvent<HTMLLIElement>) => {
      setDragged(null)
      // Escape или отпустили мимо списка — перетаскивание отменено, порядок прежний
      if (event.dataTransfer.dropEffect === 'none') setPreview(null)
      else saveOrder(ids)
    },
  })

  return (
    <>
      <PageTitle
        action={
          <Button onClick={() => setEditing('new')}>
            <Plus />
            Новый тариф
          </Button>
        }
      >
        Тарифы
      </PageTitle>

      {actionError && <p className="text-sm text-danger-text">{actionError}</p>}
      {squadsQuery.isError && (
        <Alert className="border-warning-text/40 bg-warning-soft">
          <AlertDescription className="text-foreground">
            Панель недоступна — названия сквадов не показать, новые сквады не выбрать.
          </AlertDescription>
        </Alert>
      )}

      <Module title="В продаже" action={<Badge variant="secondary">{onSale.length}</Badge>}>
        {data && onSale.length === 0 && (
          <p className="text-sm text-muted-foreground">
            В продаже нет тарифов — новые клиенты не смогут ничего купить. Создайте тариф или верните из архива.
          </p>
        )}
        {onSale.length > 1 && (
          <p className="mb-2 text-xs text-muted-foreground">
            В этом порядке клиент видит тарифы. Перетащите строку или передвиньте стрелками.
          </p>
        )}
        <ul className="divide-y">
          {shown.map((tariff, index) => (
            <TariffRow
              key={tariff.id}
              tariff={tariff}
              currency={currency}
              squads={squads}
              className={cn(dragged === tariff.id && 'opacity-50')}
              {...(shown.length > 1 ? dragProps(tariff, index) : {})}
              handle={
                shown.length > 1 && (
                  <GripVertical className="mt-0.5 hidden size-4 shrink-0 cursor-grab text-muted-foreground sm:block" aria-hidden />
                )
              }
              actions={
                <>
                  {shown.length > 1 && (
                    <>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Поднять ${tariff.name}`}
                        disabled={index === 0 || order.isPending}
                        onClick={() => saveOrder(moved(ids, tariff.id, index - 1))}
                      >
                        <ArrowUp />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Опустить ${tariff.name}`}
                        disabled={index === shown.length - 1 || order.isPending}
                        onClick={() => saveOrder(moved(ids, tariff.id, index + 1))}
                      >
                        <ArrowDown />
                      </Button>
                    </>
                  )}
                  {menu(tariff)}
                </>
              }
            />
          ))}
        </ul>
      </Module>

      {archived.length > 0 && (
        <Module title="В архиве" action={<Badge variant="secondary">{archived.length}</Badge>}>
          <p className="mb-2 text-xs text-muted-foreground">
            Новым клиентам не показываются. Те, у кого такой тариф, продлевают его, пока подписка активна.
          </p>
          <ul className="divide-y">
            {archived.map((tariff) => (
              <TariffRow key={tariff.id} tariff={tariff} currency={currency} squads={squads} actions={menu(tariff)} />
            ))}
          </ul>
        </Module>
      )}

      <Dialog open={editing !== null} onOpenChange={(open) => !open && setEditing(null)}>
        <DialogContent className="glass-float max-h-[90dvh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>{editing === 'new' ? 'Новый тариф' : 'Тариф'}</DialogTitle>
            <DialogDescription>
              Срок + безлимит.{' '}
              {editing !== 'new' &&
                'Новые параметры применятся при следующей покупке или продлении; действующие подписки и созданные счета не меняются.'}
            </DialogDescription>
          </DialogHeader>
          {editing !== null && (
            <TariffForm
              key={editing === 'new' ? 'new' : editing.id}
              tariff={editing === 'new' ? null : editing}
              squads={squads}
              squadsError={squadsQuery.isError}
              onDone={() => setEditing(null)}
            />
          )}
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        confirm={confirm}
        onClose={() => setConfirm(null)}
        onLastOnSale={() => setConfirm((current) => current && { ...current, last: true })}
      />
    </>
  )
}
