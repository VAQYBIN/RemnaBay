import { useQueryClient } from '@tanstack/react-query'
import { Ban, CircleCheck, EllipsisVertical, ListChecks, Pencil, Plus, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { Link, useOutletContext } from 'react-router'

import { $api, errorMessage, type Schemas } from '@/api/client'
import { Field } from '@/components/Field'
import { Module, PageTitle } from '@/components/frame/Module'
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
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { formatCount, formatDateTime, plural } from '@/lib/format'
import { clientLabel } from '@/lib/payments'
import { isOwner, type Member } from '@/lib/session'
import { cn } from '@/lib/utils'

type Promo = Schemas['PromoOut']
type TariffRef = Schemas['TariffRefOut']
type Kind = 'percent_discount' | 'days'

// Пределы — как в API (src/remnabay/promo/_codes.py)
const MAX_CODE_LENGTH = 64
const MAX_DAYS = 3650
const MAX_ACTIVATIONS = 1_000_000
// Выбранный вариант — как активный раздел навигации: фирменным цветом
const CHOSEN = 'data-[state=on]:bg-brand-soft data-[state=on]:text-brand-text'

const STATES: Record<Promo['state'], string> = {
  active: 'Действует',
  disabled: 'Отключён',
  expired: 'Истёк',
  exhausted: 'Исчерпан',
}

const ACTIVATION_STATES: Record<Schemas['ActivationState'], string> = {
  applied: 'Применена',
  pending: 'Дни ждут панель',
  cancelled: 'Отменена командой',
}

function daysText(days: number): string {
  return `${days} ${plural(days, 'день', 'дня', 'дней')}`
}

function valueText(promo: Pick<Promo, 'kind' | 'value'>): string {
  return promo.kind === 'days' ? `+${daysText(promo.value)}` : `скидка ${promo.value}%`
}

/** «2026-12-31» → «31.12.2026». */
function dateText(iso: string): string {
  const [year, month, day] = iso.split('-')
  return `${day}.${month}.${year}`
}

/** «31.12.2026» → «2026-12-31»; пусто — без срока; `undefined` — дата неверная. */
function parseDate(text: string): string | null | undefined {
  const trimmed = text.trim()
  if (trimmed === '') return null
  const match = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(trimmed)
  if (!match) return undefined
  const [, day, month, year] = match.map(Number)
  const date = new Date(Date.UTC(year, month - 1, day))
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) {
    return undefined
  }
  return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

function wholeNumber(text: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(text.trim())) return null
  const value = Number(text.trim())
  return value >= min && value <= max ? value : null
}

/** Ограничения строкой: срок, активации, «только новые», тарифы (8.2). */
function limitsText(promo: Promo, tariffs: TariffRef[]): string[] {
  const parts: string[] = []
  parts.push(promo.valid_until ? `до ${dateText(promo.valid_until)} включительно` : 'без срока')
  parts.push(
    promo.max_activations === null
      ? `${formatCount(promo.activations)} ${plural(promo.activations, 'активация', 'активации', 'активаций')}`
      : `активаций ${formatCount(promo.activations)} из ${formatCount(promo.max_activations)}`,
  )
  if (promo.only_without_payments) parts.push('только для клиентов без оплат')
  if (promo.tariff_ids.length > 0) {
    const names = promo.tariff_ids.map((id) => tariffs.find((tariff) => tariff.id === id)?.name ?? `тариф ${id}`)
    parts.push(`тарифы: ${names.join(', ')}`)
  }
  return parts
}

function StateBadge({ state }: { state: Promo['state'] }) {
  return (
    <Badge variant={state === 'active' ? 'secondary' : 'outline'} className={cn(state !== 'active' && 'text-muted-foreground')}>
      {STATES[state]}
    </Badge>
  )
}

type FormState = {
  code: string
  kind: Kind
  value: string
  validUntil: string
  maxActivations: string
  onlyNew: boolean
  tariffIds: number[]
}

function initialForm(promo: Promo | null): FormState {
  if (!promo) {
    return { code: '', kind: 'percent_discount', value: '', validUntil: '', maxActivations: '', onlyNew: false, tariffIds: [] }
  }
  return {
    code: promo.code,
    kind: promo.kind === 'days' ? 'days' : 'percent_discount',
    value: String(promo.value),
    validUntil: promo.valid_until ? dateText(promo.valid_until) : '',
    maxActivations: promo.max_activations === null ? '' : String(promo.max_activations),
    onlyNew: promo.only_without_payments,
    tariffIds: promo.tariff_ids,
  }
}

/** Форма промокода (8.1, 8.2). После создания меняются только ограничения (8.16). */
function PromoForm({ promo, tariffs, onDone }: { promo: Promo | null; tariffs: TariffRef[]; onDone: () => void }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState(() => initialForm(promo))
  const create = $api.useMutation('post', '/api/admin/promo-codes')
  const limits = $api.useMutation('put', '/api/admin/promo-codes/{promo_id}/limits')
  const generate = $api.useMutation('get', '/api/admin/promo-codes/generate')
  const mutation = promo ? limits : create

  const code = form.code.trim().toUpperCase()
  const codeValid = /^[A-Z0-9-]+$/.test(code) && code.length <= MAX_CODE_LENGTH
  const value = form.kind === 'days' ? wholeNumber(form.value, 1, MAX_DAYS) : wholeNumber(form.value, 1, 100)
  const validUntil = parseDate(form.validUntil)
  const maxActivations = form.maxActivations.trim() === '' ? null : wholeNumber(form.maxActivations, 1, MAX_ACTIVATIONS)
  const activationsValid = form.maxActivations.trim() === '' || maxActivations !== null
  const valid = (promo !== null || (codeValid && value !== null)) && validUntil !== undefined && activationsValid

  const change = (patch: Partial<FormState>) => setForm({ ...form, ...patch })
  const toggleTariff = (id: number, checked: boolean) =>
    change({ tariffIds: checked ? [...form.tariffIds, id] : form.tariffIds.filter((other) => other !== id) })

  const submit = () => {
    if (!valid || validUntil === undefined) return
    const body = {
      valid_until: validUntil,
      max_activations: maxActivations,
      only_without_payments: form.onlyNew,
      tariff_ids: form.kind === 'percent_discount' ? form.tariffIds : [],
    }
    const done = {
      onSuccess: () => {
        void queryClient.invalidateQueries()
        onDone()
      },
    }
    if (promo) limits.mutate({ params: { path: { promo_id: promo.id } }, body }, done)
    else if (value !== null) create.mutate({ body: { ...body, code, kind: form.kind, value } }, done)
  }

  return (
    <form
      className="flex flex-col gap-5"
      onSubmit={(event) => {
        event.preventDefault()
        submit()
      }}
    >
      {promo ? (
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="font-mono text-lg font-medium">{promo.code}</span>
          <span className="text-sm text-muted-foreground">{valueText(promo)}</span>
          <p className="w-full text-xs text-muted-foreground">Код, вид и значение после создания не меняются.</p>
        </div>
      ) : (
        <>
          <Field
            id="promo-code"
            label="Код"
            hint="Латинские буквы, цифры и дефис. Регистр не важен: sale10 и SALE10 — один код."
          >
            <div className="flex gap-2">
              <Input
                id="promo-code"
                className="font-mono uppercase"
                value={form.code}
                maxLength={MAX_CODE_LENGTH}
                aria-invalid={form.code !== '' && !codeValid}
                onChange={(e) => change({ code: e.target.value })}
              />
              <Button
                type="button"
                variant="outline"
                disabled={generate.isPending}
                onClick={() => generate.mutate({}, { onSuccess: (data) => change({ code: data.code }) })}
              >
                <Sparkles />
                Сгенерировать
              </Button>
            </div>
          </Field>
          <div className="flex flex-col gap-1.5">
            <span className="text-sm font-medium">Что даёт</span>
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={form.kind}
              onValueChange={(kind) => kind && change({ kind: kind as Kind })}
              aria-label="Вид промокода"
            >
              <ToggleGroupItem value="percent_discount" className={CHOSEN}>
                Скидку в процентах
              </ToggleGroupItem>
              <ToggleGroupItem value="days" className={CHOSEN}>
                Дни к подписке
              </ToggleGroupItem>
            </ToggleGroup>
            <p className="text-xs text-muted-foreground">
              {form.kind === 'days'
                ? 'Клиент вводит код через «Ввести промокод» в меню бота.'
                : 'Клиент вводит код на экране подтверждения покупки или продления.'}
            </p>
          </div>
          <Field
            id="promo-value"
            label={form.kind === 'days' ? 'Дней' : 'Скидка, %'}
            hint={form.kind === 'days' ? `От 1 до ${MAX_DAYS}.` : 'От 1 до 100. Скидка округляется вниз до целого рубля.'}
          >
            <Input
              id="promo-value"
              inputMode="numeric"
              className="tabular w-32"
              value={form.value}
              aria-invalid={form.value !== '' && value === null}
              onChange={(e) => change({ value: e.target.value })}
            />
          </Field>
        </>
      )}

      <fieldset className="flex flex-col gap-4">
        <legend className="mb-1.5 text-sm font-medium">Ограничения</legend>
        <div className="grid gap-5 sm:grid-cols-2">
          <Field id="promo-until" label="Действует по" hint="Последний день, включительно. Пусто — без срока.">
            <Input
              id="promo-until"
              inputMode="numeric"
              placeholder="ДД.ММ.ГГГГ"
              className="tabular"
              value={form.validUntil}
              aria-invalid={validUntil === undefined}
              onChange={(e) => change({ validUntil: e.target.value })}
            />
          </Field>
          <Field id="promo-max" label="Всего активаций" hint="Пусто — без ограничения.">
            <Input
              id="promo-max"
              inputMode="numeric"
              className="tabular"
              value={form.maxActivations}
              aria-invalid={!activationsValid}
              onChange={(e) => change({ maxActivations: e.target.value })}
            />
          </Field>
        </div>
        <label className="flex items-start gap-3 text-sm">
          <Checkbox checked={form.onlyNew} onCheckedChange={(checked) => change({ onlyNew: checked === true })} />
          <span>
            <span className="font-medium">Только для клиентов без оплат</span>
            <span className="block text-muted-foreground">
              Клиент ещё ничего не покупал и не перенесён в магазин с платной подпиской. Триал не в счёт.
            </span>
          </span>
        </label>
        {form.kind === 'percent_discount' && (
          <div className="flex flex-col gap-2">
            <span className="text-sm font-medium">Тарифы</span>
            {tariffs.map((tariff) => (
              <label key={tariff.id} className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={form.tariffIds.includes(tariff.id)}
                  onCheckedChange={(checked) => toggleTariff(tariff.id, checked === true)}
                />
                {tariff.name}
                {tariff.state !== 'on_sale' && <span className="text-muted-foreground">· в архиве</span>}
              </label>
            ))}
            <p className="text-xs text-muted-foreground">Ничего не выбрано — подходит к любому тарифу.</p>
          </div>
        )}
        <p className="text-xs text-muted-foreground">Один клиент может воспользоваться промокодом только один раз.</p>
      </fieldset>

      {mutation.error && <p className="text-sm text-danger-text">{errorMessage(mutation.error, 'Не удалось сохранить')}</p>}
      {generate.isError && <p className="text-sm text-danger-text">Не удалось сгенерировать код — попробуйте ещё раз</p>}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="ghost">
            Отмена
          </Button>
        </DialogClose>
        <Button type="submit" disabled={!valid || mutation.isPending}>
          {promo ? 'Сохранить' : 'Создать промокод'}
        </Button>
      </DialogFooter>
    </form>
  )
}

/** Активации промокода (8.3): кто, когда, к какому платежу или подписке. */
function Activations({ promo, timeZone }: { promo: Promo; timeZone: string }) {
  const { data, isError } = $api.useQuery('get', '/api/admin/promo-codes/{promo_id}/activations', {
    params: { path: { promo_id: promo.id } },
  })
  if (isError) return <p className="text-sm text-danger-text">Не удалось загрузить активации</p>
  if (!data) return null
  if (data.length === 0) return <p className="text-sm text-muted-foreground">Промокодом ещё никто не воспользовался.</p>
  return (
    <ul className="divide-y">
      {data.map((activation) => (
        <li key={activation.id} className="flex flex-col gap-0.5 py-3 text-sm">
          <span className="font-medium break-words">{clientLabel(activation.client)}</span>
          <span className="text-muted-foreground">
            {formatDateTime(activation.applied_at ?? activation.created_at, timeZone)} ·{' '}
            {ACTIVATION_STATES[activation.state]}
            {activation.subscription_name && ` · подписка «${activation.subscription_name}»`}
          </span>
          {activation.payment_id !== null && (
            <Link to={`/payments/${activation.payment_id}`} className="w-fit text-brand-text hover:underline">
              Платёж № {activation.payment_id}
            </Link>
          )}
        </li>
      ))}
    </ul>
  )
}

/** А7. Промокоды: список, создание, ограничения, отключение (8.1–8.3, 8.16).
 *  Помощник видит список и активации без действий (решение 0065). */
export function PromoCodesPage() {
  const member = useOutletContext<Member>()
  const owner = isOwner(member)
  const queryClient = useQueryClient()
  const { data } = $api.useQuery('get', '/api/admin/promo-codes')
  const disable = $api.useMutation('post', '/api/admin/promo-codes/{promo_id}/disable')
  const enable = $api.useMutation('post', '/api/admin/promo-codes/{promo_id}/enable')
  const [editing, setEditing] = useState<Promo | 'new' | null>(null)
  const [watching, setWatching] = useState<Promo | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const promos = data?.promo_codes ?? []
  const tariffs = data?.tariffs ?? []
  const timeZone = data?.time_zone ?? 'Europe/Moscow'

  const toggle = (promo: Promo) => {
    setActionError(null)
    const mutation = promo.disabled ? enable : disable
    mutation.mutate(
      { params: { path: { promo_id: promo.id } } },
      {
        onSuccess: () => void queryClient.invalidateQueries(),
        onError: (error) => setActionError(errorMessage(error, 'Не получилось')),
      },
    )
  }

  const menu = (promo: Promo) => (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={`Действия с промокодом ${promo.code}`}>
          <EllipsisVertical />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="glass-float w-56">
        <DropdownMenuItem onSelect={() => setWatching(promo)}>
          <ListChecks />
          Активации
        </DropdownMenuItem>
        {owner && (
          <>
            <DropdownMenuItem onSelect={() => setEditing(promo)}>
              <Pencil />
              Изменить ограничения
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => toggle(promo)}>
              {promo.disabled ? <CircleCheck /> : <Ban />}
              {promo.disabled ? 'Включить' : 'Отключить'}
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )

  return (
    <>
      <PageTitle
        action={
          owner && (
            <Button onClick={() => setEditing('new')}>
              <Plus />
              Новый промокод
            </Button>
          )
        }
      >
        Промокоды
      </PageTitle>

      {actionError && <p className="text-sm text-danger-text">{actionError}</p>}

      <Module title="Все промокоды" action={<Badge variant="secondary">{promos.length}</Badge>}>
        {data && promos.length === 0 && (
          <p className="text-sm text-muted-foreground">
            Промокодов пока нет.{owner && ' Создайте первый — скидку на покупку или дни к подписке.'}
          </p>
        )}
        <ul className="divide-y">
          {promos.map((promo) => (
            <li key={promo.id} className="flex items-start gap-2 py-3">
              <button
                type="button"
                className="flex min-w-0 flex-1 flex-col gap-1 text-left"
                onClick={() => setWatching(promo)}
              >
                <span className="flex flex-wrap items-center gap-2">
                  <span className={cn('font-mono font-medium break-all', promo.state !== 'active' && 'text-muted-foreground')}>
                    {promo.code}
                  </span>
                  <span className="text-sm">{valueText(promo)}</span>
                  <StateBadge state={promo.state} />
                </span>
                <span className="tabular text-sm text-muted-foreground">{limitsText(promo, tariffs).join(' · ')}</span>
              </button>
              <div className="flex shrink-0 items-center">{menu(promo)}</div>
            </li>
          ))}
        </ul>
      </Module>

      <Dialog open={editing !== null} onOpenChange={(open) => !open && setEditing(null)}>
        <DialogContent className="glass-float max-h-[90dvh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>{editing === 'new' ? 'Новый промокод' : 'Ограничения промокода'}</DialogTitle>
            <DialogDescription>
              {editing === 'new'
                ? 'Скидка в процентах на покупку или продление, или дни к подписке клиента.'
                : 'Новые ограничения действуют сразу. Кто уже оплатил со скидкой, получит её в любом случае.'}
            </DialogDescription>
          </DialogHeader>
          {editing !== null && (
            <PromoForm
              key={editing === 'new' ? 'new' : editing.id}
              promo={editing === 'new' ? null : editing}
              tariffs={tariffs}
              onDone={() => setEditing(null)}
            />
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={watching !== null} onOpenChange={(open) => !open && setWatching(null)}>
        <DialogContent className="glass-float max-h-[90dvh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle className="font-mono">{watching?.code}</DialogTitle>
            <DialogDescription>
              {watching && `${valueText(watching)} · ${STATES[watching.state].toLowerCase()}`}
            </DialogDescription>
          </DialogHeader>
          {watching && <Activations promo={watching} timeZone={timeZone} />}
        </DialogContent>
      </Dialog>
    </>
  )
}
