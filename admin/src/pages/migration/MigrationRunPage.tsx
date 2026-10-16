import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Play, RotateCcw } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, Navigate, useNavigate, useOutletContext, useParams } from 'react-router'

import { $api, errorMessage } from '@/api/client'
import { Module, PageTitle } from '@/components/frame/Module'
import { SearchSelect } from '@/components/SearchSelect'
import { Badge } from '@/components/ui/badge'
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
import { formatCount, formatDateTime } from '@/lib/format'
import { isOwner, type Member } from '@/lib/session'
import { cn } from '@/lib/utils'

import { StateBadge } from './MigrationPage'
import {
  devicesText,
  type Group,
  groupKey,
  isConflict,
  type MappingRule,
  type Problem,
  PROGRESS_REFRESH_MS,
  progressShare,
  progressText,
  REASONS,
  type Report,
  type Run,
  running,
  runTitle,
} from './migration'

// Без тарифа — значение в выборе тарифа группы
const NO_TARIFF = ''
// Сколько строк списка показывать свёрнутым
const COLLAPSED_ROWS = 5

function Stat({ label, value, tone }: { label: string; value: number; tone?: 'danger' | 'warning' }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span
        className={cn(
          'text-xl font-semibold tabular-nums',
          tone === 'danger' && value > 0 && 'text-danger-text',
          tone === 'warning' && value > 0 && 'text-warning-text',
        )}
      >
        {formatCount(value)}
      </span>
    </div>
  )
}

/** Числа отчёта: усыновление (9.2) и импорт (9.11). */
function Totals({ run, report }: { run: Run; report: Report }) {
  const done = !run.dry_run
  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-4">
        <Stat label={done ? 'Усыновлено пользователей панели' : 'Будет усыновлено'} value={report.adopt} />
        <Stat label="Клиентов с подписками" value={report.clients} />
        <Stat label="Из них новых" value={report.clients_new} />
        <Stat label="Уже были в магазине" value={report.clients_existing} />
        <Stat label="С несколькими подписками" value={report.clients_several} />
        <Stat label="Без Telegram ID — останутся в панели" value={report.without_telegram_id} tone="warning" />
        <Stat label="Усыновлены раньше" value={report.already_adopted} />
        <Stat label="Созданы магазином" value={report.already_in_shop} />
        <Stat label="Всего пользователей в панели" value={report.panel_users} />
        {done && <Stat label="Пропущено при применении" value={report.skipped} tone="warning" />}
      </div>
      {report.file && (
        <div className="flex flex-col gap-3">
          <h3 className="text-sm font-medium">Из файла</h3>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-4">
            <Stat label="Клиентов в файле" value={report.file.file_clients} />
            <Stat label="Связей с панелью найдено" value={report.file.links_found} />
            <Stat label="Связей не найдено" value={report.file.links_not_found} tone="warning" />
            <Stat label="Из них без Telegram ID в панели" value={report.file.links_without_telegram_id} />
            <Stat label="Без пользователя в панели" value={report.file.without_panel_user} />
            <Stat label="Дополнятся данными файла" value={report.file.filled} />
            <Stat label="Уже перенесены — пропуск" value={report.file.skipped_imported} />
            <Stat label="Реферальных кодов" value={report.file.referral_codes} />
            <Stat label="Реферальных связей" value={report.file.referral_links} />
            <Stat label="Использовали триал" value={report.file.trial_used} />
            <Stat label="Платили в старом боте" value={report.file.paid_yes} />
            <Stat label="Не платили" value={report.file.paid_no} />
            <Stat label="Об оплатах неизвестно" value={report.file.paid_unknown} />
          </div>
        </div>
      )}
    </div>
  )
}

function GroupRow({
  group,
  tariffId,
  tariffs,
  editable,
  onChange,
}: {
  group: Group
  tariffId: string
  tariffs: Readonly<Record<string, string>>
  editable: boolean
  onChange: (tariffId: string) => void
}) {
  const key = groupKey(
    group.squads.map((squad) => squad.uuid),
    group.device_limit,
  )
  const hints = Object.entries(group.hints ?? {})
  return (
    <li className="flex flex-col gap-3 py-3 sm:flex-row sm:items-center">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          {group.squads.length === 0 ? (
            <span className="text-sm text-muted-foreground">Без сквадов</span>
          ) : (
            group.squads.map((squad) => (
              <Badge key={squad.uuid} variant="outline">
                {squad.name}
              </Badge>
            ))
          )}
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          {devicesText(group.device_limit)} · {formatCount(group.users)} польз.
          {group.tariff_id !== null && group.differs > 0 && (
            <span className="text-warning-text"> · параметры отличаются у {formatCount(group.differs)}</span>
          )}
        </p>
        {hints.length > 0 && (
          <p className="text-xs text-muted-foreground">
            В старом боте: {hints.map(([name, count]) => `${name} — ${formatCount(count)}`).join(', ')}
          </p>
        )}
      </div>
      <div className="w-full sm:w-64">
        {editable ? (
          <SearchSelect
            id={`tariff-${key}`}
            value={tariffId}
            options={[NO_TARIFF, ...Object.keys(tariffs)]}
            labels={{ [NO_TARIFF]: 'Без тарифа', ...tariffs }}
            onChange={onChange}
            searchPlaceholder="Найти тариф"
          />
        ) : (
          <p className="text-sm">{tariffId === NO_TARIFF ? 'Без тарифа' : (tariffs[tariffId] ?? 'Тариф удалён')}</p>
        )}
      </div>
    </li>
  )
}

/** Сопоставление «сквады и лимит устройств → тариф» по группам (9.15, решение 0071). */
function Mapping({
  run,
  report,
  choice,
  tariffs,
  editable,
  onChange,
}: {
  run: Run
  report: Report
  choice: Record<string, string>
  tariffs: Readonly<Record<string, string>>
  editable: boolean
  onChange: (key: string, tariffId: string) => void
}) {
  const groups = report.groups ?? []
  if (groups.length === 0) {
    return <p className="text-sm text-muted-foreground">Усыновлять некого — групп нет.</p>
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        {run.dry_run
          ? 'Пользователи панели собраны в группы по сквадам и лимиту устройств. Подписки группы с тарифом продлеваются этим тарифом; без тарифа — клиент выберет тариф при продлении. Параметры в панели выставятся по тарифу при первом продлении.'
          : 'Сопоставление, с которым применён прогон.'}
      </p>
      <p className="text-sm">
        Без тарифа: <span className="font-medium tabular-nums">{formatCount(report.no_tariff)}</span>
        {report.differs > 0 && (
          <>
            {' '}
            · параметры изменятся при продлении:{' '}
            <span className="font-medium tabular-nums text-warning-text">{formatCount(report.differs)}</span>
          </>
        )}
      </p>
      <ul className="-my-3 divide-y">
        {groups.map((group) => {
          const key = groupKey(
            group.squads.map((squad) => squad.uuid),
            group.device_limit,
          )
          return (
            <GroupRow
              key={key}
              group={group}
              tariffId={choice[key] ?? NO_TARIFF}
              tariffs={tariffs}
              editable={editable}
              onChange={(tariffId) => onChange(key, tariffId)}
            />
          )
        })}
      </ul>
    </div>
  )
}

/** Конфликты (9.12) или нераспознанное (9.11) списком; свёрнут до пяти строк. */
function Problems({ title, problems, total }: { title: string; problems: Problem[]; total: number }) {
  const [expanded, setExpanded] = useState(false)
  if (total === 0) return null
  const shown = expanded ? problems : problems.slice(0, COLLAPSED_ROWS)
  return (
    <Module
      title={`${title}: ${formatCount(total)}`}
      footer={
        problems.length > COLLAPSED_ROWS && (
          <Button variant="ghost" size="sm" onClick={() => setExpanded(!expanded)}>
            {expanded ? 'Свернуть' : `Показать все (${formatCount(problems.length)})`}
          </Button>
        )
      }
    >
      <ul className="-my-3 divide-y">
        {shown.map((problem, index) => (
          <li key={index} className="flex flex-col gap-0.5 py-3 sm:flex-row sm:items-baseline sm:gap-4">
            <span className="w-36 shrink-0 text-sm tabular-nums">
              {problem.telegram_id !== null && problem.telegram_id !== undefined ? problem.telegram_id : '—'}
            </span>
            <span className="min-w-0 flex-1 text-sm">
              {REASONS[problem.reason]}
              {problem.detail && <span className="text-muted-foreground"> · {problem.detail}</span>}
            </span>
            {problem.panel_user && (
              <span className="truncate text-sm text-muted-foreground">{problem.panel_user}</span>
            )}
          </li>
        ))}
      </ul>
      {problems.length < total && (
        <p className="pt-3 text-xs text-muted-foreground">В списке — первые {formatCount(problems.length)}.</p>
      )}
    </Module>
  )
}

function Progress({ run }: { run: Run }) {
  const share = progressShare(run)
  return (
    <Module title="Идёт прогон">
      <div className="flex flex-col gap-2">
        <div
          className="h-2 overflow-hidden rounded-full bg-muted"
          role="progressbar"
          aria-valuenow={Math.round(share)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div className="h-full rounded-full bg-brand transition-[width] duration-500" style={{ width: `${share}%` }} />
        </div>
        <p className="text-sm text-muted-foreground">{progressText(run)}</p>
      </div>
    </Module>
  )
}

function initialChoice(run: Run): Record<string, string> {
  return Object.fromEntries(
    run.mapping.map((rule) => [groupKey(rule.squads, rule.device_limit ?? null), String(rule.tariff_id)]),
  )
}

/** А9. Прогон миграции: ход, отчёт, сопоставление тарифов, конфликты, «Применить» (9.1–9.3, 9.11, 9.15). */
export function MigrationRunPage() {
  const member = useOutletContext<Member>()
  const { runId } = useParams()
  const id = Number(runId)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const owner = isOwner(member)
  const { data: run } = $api.useQuery(
    'get',
    '/api/admin/migration/runs/{run_id}',
    { params: { path: { run_id: id } } },
    { enabled: owner, refetchInterval: (query) => (query.state.data && running(query.state.data) ? PROGRESS_REFRESH_MS : false) },
  )
  const history = $api.useQuery('get', '/api/admin/migration/runs', undefined, { enabled: owner })
  const tariffList = $api.useQuery('get', '/api/admin/tariffs', undefined, { enabled: owner })
  const rerun = $api.useMutation('post', '/api/admin/migration/runs/{run_id}/rerun')
  const apply = $api.useMutation('post', '/api/admin/migration/runs/{run_id}/apply')
  const [edited, setEdited] = useState<Record<string, string> | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const tariffs = useMemo(
    () => Object.fromEntries((tariffList.data?.tariffs ?? []).map((tariff) => [String(tariff.id), tariff.name])),
    [tariffList.data],
  )

  if (!owner) return <Navigate to="/" replace />
  if (!run) return <p className="text-sm text-muted-foreground">Загрузка…</p>

  const choice = edited ?? initialChoice(run)
  const changed = edited !== null && JSON.stringify(edited) !== JSON.stringify(initialChoice(run))
  const report = run.report
  const timeZone = history.data?.time_zone
  const problems = report?.problems ?? []
  const conflicts = problems.filter(isConflict)
  const unrecognized = problems.filter((problem) => !isConflict(problem))

  const opened = (next: Run) => {
    setEdited(null)
    void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/migration/runs'] })
    void navigate(`/migration/${next.id}`)
  }

  const mapping = (): MappingRule[] =>
    (report?.groups ?? []).flatMap((group) => {
      const tariffId = choice[groupKey(group.squads.map((squad) => squad.uuid), group.device_limit)]
      return tariffId
        ? [{ squads: group.squads.map((squad) => squad.uuid), device_limit: group.device_limit, tariff_id: Number(tariffId) }]
        : []
    })

  const startRerun = () => {
    setError(null)
    rerun.mutate(
      { params: { path: { run_id: run.id } }, body: { mapping: mapping() } },
      { onSuccess: opened, onError: (failure) => setError(errorMessage(failure, 'Не удалось запустить прогон')) },
    )
  }

  const startApply = () =>
    apply.mutate(
      { params: { path: { run_id: run.id } } },
      {
        onSuccess: (next) => {
          setConfirming(false)
          opened(next)
        },
        onError: (failure) => {
          setConfirming(false)
          setError(errorMessage(failure, 'Не удалось применить прогон'))
        },
      },
    )

  const editable = run.dry_run && run.state === 'done' && run.applied_by_run_id === null
  return (
    <div className="flex flex-col gap-6">
      <Link to="/migration" className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" />
        Миграция
      </Link>
      <PageTitle
        action={
          editable &&
          (changed ? (
            <Button onClick={startRerun} disabled={rerun.isPending}>
              <RotateCcw />
              Пробный прогон заново
            </Button>
          ) : (
            run.can_apply && (
              <Button onClick={() => setConfirming(true)}>
                <Play />
                Применить
              </Button>
            )
          ))
        }
      >
        <span className="flex flex-wrap items-center gap-3">
          {runTitle(run)}
          <StateBadge run={run} />
        </span>
      </PageTitle>
      <div className="flex flex-col gap-1 text-sm text-muted-foreground">
        {timeZone && (
          <p>
            Запущен {formatDateTime(run.started_at, timeZone)}
            {run.started_by_telegram_id !== null && ` · ID ${run.started_by_telegram_id}`}
            {run.source && typeof run.source.name === 'string' && ` · источник: ${run.source.name}`}
            {run.source && typeof run.source.version === 'string' && ` (${run.source.version})`}
          </p>
        )}
        {changed && <p className="text-warning-text">Сопоставление изменено — применить можно после нового пробного прогона.</p>}
        {!changed && run.can_apply && run.apply_until && timeZone && (
          <p>Применить можно до {formatDateTime(run.apply_until, timeZone)}.</p>
        )}
        {editable && !run.can_apply && !changed && (
          <p>Применить нельзя: есть более новый пробный прогон, прошли сутки или идёт другой прогон.</p>
        )}
        {run.applied_by_run_id !== null && (
          <p>
            Применён:{' '}
            <Link to={`/migration/${run.applied_by_run_id}`} className="text-brand-text underline-offset-4 hover:underline">
              итоговый отчёт
            </Link>
          </p>
        )}
        {run.dry_run_id !== null && (
          <p>
            <Link to={`/migration/${run.dry_run_id}`} className="text-brand-text underline-offset-4 hover:underline">
              Пробный прогон
            </Link>
            , который применён
          </p>
        )}
      </div>
      {error && <p className="text-sm text-danger-text">{error}</p>}
      {running(run) && <Progress run={run} />}
      {run.state === 'failed' && (
        <Module title="Прогон не удался">
          <p className="text-sm text-danger-text">{run.error ?? 'Прогон прервался'}</p>
          <p className="mt-2 text-sm text-muted-foreground">
            Уже применённое сохранено. Запустите пробный прогон заново — повторное применение пропустит перенесённое.
          </p>
        </Module>
      )}
      {report && run.state !== 'applying' && (
        <>
          <Module title={run.dry_run ? 'Отчёт пробного прогона' : 'Итоговый отчёт'}>
            <Totals run={run} report={report} />
          </Module>
          <Module title="Тарифы">
            <Mapping
              run={run}
              report={report}
              choice={choice}
              tariffs={tariffs}
              editable={editable}
              onChange={(key, tariffId) => setEdited({ ...choice, [key]: tariffId })}
            />
          </Module>
          <Problems title="Конфликты — не применяются" problems={conflicts} total={report.conflicts} />
          <Problems title="Не распознано" problems={unrecognized} total={report.unrecognized} />
        </>
      )}

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent className="glass-float sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Применить {run.kind === 'adoption' ? 'усыновление' : 'импорт'}?</DialogTitle>
            <DialogDescription>
              Магазин заново прочитает панель и применит прогон по тем же правилам: усыновит{' '}
              {formatCount(report?.adopt ?? 0)} польз. панели
              {report?.file && `, перенесёт данные ${formatCount(report.file.file_clients)} клиентов из файла`}.
              Конфликты не применяются. В панели ничего не изменится. Отменить применение нельзя.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="ghost">
                Не применять
              </Button>
            </DialogClose>
            <Button onClick={startApply} disabled={apply.isPending}>
              Применить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
