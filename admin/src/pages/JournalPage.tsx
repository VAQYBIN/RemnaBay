import { ChevronDown, ChevronUp } from 'lucide-react'
import { type ReactNode, useEffect, useMemo, useState } from 'react'
import { Link, useOutletContext, useSearchParams } from 'react-router'

import { $api, errorMessage, type Schemas } from '@/api/client'
import { Chip } from '@/components/Chip'
import { Field } from '@/components/Field'
import { Module, PageTitle } from '@/components/frame/Module'
import { SearchSelect } from '@/components/SearchSelect'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { formatDateTime } from '@/lib/format'
import {
  ALL_ACTIONS,
  actionName,
  actionOptions,
  actionQuery,
  detailsLines,
  detailsSummary,
  NUMERIC_SUBJECTS,
  SUBJECT_TYPES,
} from '@/lib/journal'
import { actorName, clientLabel } from '@/lib/payments'
import { isOwner, type Member } from '@/lib/session'

type Row = Schemas['JournalRowOut']
type ActorType = Row['actor_type']
type Outcome = Row['outcome']
type Period = Schemas['Period']

// id объекта уходит в адрес и на сервер, когда оператор перестал печатать
const ID_DELAY_MS = 300
const ALL_SUBJECTS = ''

const ACTORS: { value: ActorType | null; label: string }[] = [
  { value: null, label: 'Все' },
  { value: 'team_member', label: 'Команда' },
  { value: 'client', label: 'Клиент' },
  { value: 'system', label: 'Система' },
  { value: 'panel', label: 'Панель' },
  { value: 'provider', label: 'Провайдер' },
]

const PERIODS: { value: Period | null; label: string }[] = [
  { value: null, label: 'За всё время' },
  { value: 'today', label: 'Сегодня' },
  { value: '7d', label: '7 дней' },
  { value: '30d', label: '30 дней' },
]

const OUTCOMES: { value: Outcome | null; label: string }[] = [
  { value: null, label: 'Все' },
  { value: 'success', label: 'Успешно' },
  { value: 'failure', label: 'С ошибкой' },
]

function pick<T extends string>(value: string | null, allowed: readonly { value: T | null }[]): T | null {
  return allowed.find((item) => item.value === value)?.value ?? null
}

function ChipRow<T extends string>({
  label,
  items,
  value,
  onChange,
}: {
  label: string
  items: readonly { value: T | null; label: string }[]
  value: T | null
  onChange: (value: T | null) => void
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <span className="text-sm font-medium">{label}</span>
      <nav className="flex gap-1 overflow-x-auto pb-1" aria-label={label}>
        {items.map((item) => (
          <Chip key={item.label} active={value === item.value} onClick={() => onChange(item.value)}>
            {item.label}
          </Chip>
        ))}
      </nav>
    </div>
  )
}

/** Кто действовал: участник по имени, клиент со ссылкой на карточку. */
function ActorText({ entry }: { entry: Row }) {
  if (entry.actor_type === 'team_member') return <>{entry.actor ?? actorName('team_member')}</>
  if (entry.actor_type === 'client') {
    return entry.actor_client ? (
      <Link to={`/clients/${entry.actor_client.id}`} className="text-brand-text hover:underline">
        {clientLabel(entry.actor_client)}
      </Link>
    ) : (
      <>{actorName('client')}</>
    )
  }
  if (entry.actor_type === 'provider' && entry.actor) return <>{`${actorName('provider')} ${entry.actor}`}</>
  return <>{actorName(entry.actor_type)}</>
}

function SubjectLink({ to, children }: { to: string | null; children: ReactNode }) {
  if (!to) return <>{children}</>
  return (
    <Link to={to} className="text-brand-text hover:underline">
      {children}
    </Link>
  )
}

/** Над чем: объект понятными словами; клиент, платёж, рассылка, прогон — ссылкой. */
function SubjectText({
  subject,
  owner,
  actorClientId,
}: {
  subject: NonNullable<Row['subject']>
  owner: boolean
  actorClientId: number | null
}) {
  const kind = SUBJECT_TYPES[subject.type] ?? subject.type
  const named = subject.name ? `${kind} «${subject.name}»` : `${kind} ${subject.id}`
  // Клиент, который действовал сам, уже назван слева от стрелки
  const client = subject.client && subject.client.id !== actorClientId && (
    <SubjectLink to={`/clients/${subject.client.id}`}>{clientLabel(subject.client)}</SubjectLink>
  )
  switch (subject.type) {
    case 'client':
      return subject.client ? (
        <SubjectLink to={`/clients/${subject.client.id}`}>{clientLabel(subject.client)}</SubjectLink>
      ) : (
        <>{named}</>
      )
    case 'subscription':
      return (
        <>
          {named}
          {client && <> · {client}</>}
        </>
      )
    case 'payment':
      return (
        <>
          <SubjectLink to={`/payments/${subject.id}`}>Платёж № {subject.id}</SubjectLink>
          {client && <> · {client}</>}
        </>
      )
    // Рассылки и миграция — экраны владельца (01-domain)
    case 'broadcast':
      return <SubjectLink to={owner ? `/broadcasts/${subject.id}` : null}>Рассылка № {subject.id}</SubjectLink>
    case 'migration_run':
      return <SubjectLink to={owner ? `/migration/${subject.id}` : null}>Прогон миграции № {subject.id}</SubjectLink>
    case 'promo_code':
      return <SubjectLink to="/promo-codes">{subject.name ? `Промокод ${subject.name}` : named}</SubjectLink>
    case 'tariff':
      return <SubjectLink to={owner ? '/tariffs' : null}>{named}</SubjectLink>
    // У бота в Telegram один профиль: id ничего не говорит
    case 'telegram_bot':
      return <>{kind}</>
    default:
      return <>{named}</>
  }
}

/** Запись журнала: что, когда, кто, над чем, результат и подробности (4.35, 4.9). */
function EntryItem({ entry, timeZone, owner }: { entry: Row; timeZone: string; owner: boolean }) {
  const [open, setOpen] = useState(false)
  const summary = detailsSummary(entry)
  const lines = detailsLines(entry)
  const failed = entry.outcome === 'failure'
  return (
    <li className="flex min-w-0 flex-col gap-1 py-3 text-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <span className="flex min-w-0 flex-wrap items-center gap-2 font-medium">
          <span className="break-words">{actionName(entry.action)}</span>
          {failed && <Badge className="bg-danger-soft text-danger-text">ошибка</Badge>}
        </span>
        <time className="tabular shrink-0 text-muted-foreground">{formatDateTime(entry.occurred_at, timeZone)}</time>
      </div>
      <p className="min-w-0 break-words text-muted-foreground">
        <ActorText entry={entry} />
        {entry.subject && (
          <>
            {' → '}
            <SubjectText subject={entry.subject} owner={owner} actorClientId={entry.actor_client?.id ?? null} />
          </>
        )}
      </p>
      {(summary || lines.length > 0) && (
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          {summary && <span className={failed ? 'break-words text-warning-text' : 'break-words'}>{summary}</span>}
          {lines.length > 0 && (
            <Button
              variant="ghost"
              size="xs"
              className="-ml-1 text-muted-foreground"
              aria-expanded={open}
              onClick={() => setOpen(!open)}
            >
              Подробности
              {open ? <ChevronUp /> : <ChevronDown />}
            </Button>
          )}
        </div>
      )}
      {open && (
        <dl className="mt-1 grid min-w-0 gap-1 rounded-lg bg-muted/40 p-3 font-mono text-xs">
          {lines.map((line) => (
            <div key={line.key} className="min-w-0">
              <dt className="inline text-muted-foreground">{line.key}: </dt>
              <dd className="inline break-all">{line.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  )
}

/** А10. Журнал (4.35): все записи, новые сверху, с фильтрами «кто», «что», «над
 *  чем», период и результат. Только чтение (4.26). Фильтры — в адресе: «Назад» из
 *  карточки клиента или платежа возвращает к тем же записям. */
export function JournalPage() {
  const member = useOutletContext<Member>()
  const owner = isOwner(member)
  const [params, setParams] = useSearchParams()
  const actor = pick(params.get('who'), ACTORS)
  const period = pick(params.get('period'), PERIODS)
  const outcome = pick(params.get('result'), OUTCOMES)
  const action = params.get('what') ?? ALL_ACTIONS
  const subjectParam = params.get('on') ?? ALL_SUBJECTS
  const subjectType = subjectParam in SUBJECT_TYPES ? subjectParam : ALL_SUBJECTS
  const subjectId = params.get('id') ?? ''
  const [idText, setIdText] = useState(subjectId)
  const withId = NUMERIC_SUBJECTS.has(subjectType)

  const update = (name: string, value: string | null) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current)
        if (value) next.set(name, value)
        else next.delete(name)
        // id имеет смысл только вместе со своим видом объекта
        if (name === 'on') next.delete('id')
        return next
      },
      { replace: true },
    )

  useEffect(() => setIdText(subjectId), [subjectId])
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const value = idText.trim()
      if (value !== subjectId) update('id', /^\d+$/.test(value) ? value : null)
    }, ID_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [idText, subjectId])

  const actions = useMemo(actionOptions, [])
  const subjectOptions = [ALL_SUBJECTS, ...Object.keys(SUBJECT_TYPES)]
  const subjectLabels = { [ALL_SUBJECTS]: 'Все объекты', ...SUBJECT_TYPES }

  const query = {
    actor_type: actor ?? undefined,
    ...actionQuery(action),
    subject_type: subjectType || undefined,
    subject_id: withId && subjectId ? subjectId : undefined,
    period: period ?? undefined,
    outcome: outcome ?? undefined,
  }
  const { data, error, isFetching, fetchNextPage, hasNextPage, isFetchingNextPage } = $api.useInfiniteQuery(
    'get',
    '/api/admin/journal',
    { params: { query } },
    {
      pageParamName: 'before',
      initialPageParam: null,
      getNextPageParam: (last) => last.next_before ?? undefined,
    },
  )
  const entries = data?.pages.flatMap((page) => page.entries) ?? []
  const timeZone = data?.pages[0]?.time_zone ?? 'Europe/Moscow'
  const filtered = actor || period || outcome || action || subjectType

  return (
    <>
      <PageTitle>Журнал</PageTitle>
      <Module title="Фильтры">
        <div className="grid min-w-0 gap-4 lg:grid-cols-2">
          <div className="min-w-0 lg:col-span-2">
            <ChipRow label="Кто" items={ACTORS} value={actor} onChange={(value) => update('who', value)} />
          </div>
          <Field id="journal-what" label="Что">
            <SearchSelect
              id="journal-what"
              value={action in actions.labels ? action : ALL_ACTIONS}
              options={actions.options}
              labels={actions.labels}
              onChange={(value) => update('what', value || null)}
              searchPlaceholder="Найти действие"
            />
          </Field>
          <div className={withId ? 'grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-end gap-2' : 'min-w-0'}>
            <Field id="journal-on" label="Над чем">
              <SearchSelect
                id="journal-on"
                value={subjectType}
                options={subjectOptions}
                labels={subjectLabels}
                onChange={(value) => update('on', value || null)}
                searchPlaceholder="Найти вид объекта"
              />
            </Field>
            {withId && (
              <Field id="journal-id" label="№">
                <Input
                  id="journal-id"
                  className="h-9 w-24"
                  inputMode="numeric"
                  placeholder="любой"
                  value={idText}
                  onChange={(event) => setIdText(event.target.value)}
                />
              </Field>
            )}
          </div>
          <ChipRow label="Период" items={PERIODS} value={period} onChange={(value) => update('period', value)} />
          <ChipRow label="Результат" items={OUTCOMES} value={outcome} onChange={(value) => update('result', value)} />
        </div>
      </Module>
      <Module title="Записи">
        {error ? (
          <p className="text-sm text-danger-text">{errorMessage(error, 'Не получилось загрузить журнал')}</p>
        ) : (
          <div className="flex flex-col gap-3">
            {data && entries.length === 0 && (
              <p className="text-sm text-muted-foreground">
                {filtered ? 'Записей с такими фильтрами нет.' : 'Записей пока нет.'}
              </p>
            )}
            <ol className={isFetching && !isFetchingNextPage ? 'divide-y opacity-60 transition-opacity' : 'divide-y transition-opacity'}>
              {entries.map((entry) => (
                <EntryItem key={entry.id} entry={entry} timeZone={timeZone} owner={owner} />
              ))}
            </ol>
            {hasNextPage && (
              <Button variant="outline" className="self-start" onClick={() => void fetchNextPage()} disabled={isFetchingNextPage}>
                {isFetchingNextPage ? 'Загружаем…' : 'Показать ещё'}
              </Button>
            )}
          </div>
        )}
      </Module>
    </>
  )
}
