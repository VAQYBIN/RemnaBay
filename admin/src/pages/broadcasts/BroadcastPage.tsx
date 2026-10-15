import { useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  Bold,
  Code,
  EyeOff,
  ImagePlus,
  Italic,
  Link2,
  Plus,
  Send,
  Strikethrough,
  TextQuote,
  Trash2,
  Underline,
  X,
} from 'lucide-react'
import { type ChangeEvent, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import { $api, errorMessage, fetchClient, type Schemas } from '@/api/client'
import { Field } from '@/components/Field'
import { Module, PageTitle } from '@/components/frame/Module'
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
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Textarea } from '@/components/ui/textarea'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatCount, formatDateTime, plural } from '@/lib/format'
import {
  escapeMarkup,
  FORMATS,
  MAX_CAPTION_LENGTH,
  MAX_TEXT_LENGTH,
  parseMarkup,
} from '@/lib/telegramMarkup'
import { cn } from '@/lib/utils'

import { type Broadcast, imageUrl, SEGMENTS, type SegmentKind } from './broadcasts'
import { StateBadge } from './BroadcastsPage'
import { MessagePreview } from './MessagePreview'

type Content = Schemas['BroadcastContent']
type LinkButton = Schemas['LinkButton']

// Как в API (src/remnabay/broadcasts/_content.py)
const MAX_LINKS = 3
const MAX_BUTTON_TEXT = 64
const LINK_URL = /^(https?|tg):\/\/\S+$/
// Черновик сохраняется, когда владелец перестал печатать
const SAVE_DELAY_MS = 800
// Пока рассылка идёт, прогресс обновляется сам (11.5)
const PROGRESS_REFRESH_MS = 2000
// Выбранный вариант — как активный раздел навигации: фирменным цветом
const CHOSEN = 'data-[state=on]:bg-brand-soft data-[state=on]:text-brand-text'

type Form = { content: Content; segment: SegmentKind }

function formOf(broadcast: Broadcast): Form {
  return { content: broadcast.content, segment: broadcast.segment.kind }
}

function sameForm(left: Form, right: Form): boolean {
  return JSON.stringify(left) === JSON.stringify(right)
}

function linkProblem(link: LinkButton): string | null {
  if (!link.text.trim()) return 'Нужна подпись кнопки'
  if (!LINK_URL.test(link.url.trim())) return 'Адрес — https://, http:// или tg://'
  return null
}

function ToolButton({ label, onClick, children }: { label: string; onClick: () => void; children: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button type="button" variant="ghost" size="icon" className="size-8" onClick={onClick} aria-label={label}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

/** Поле текста с кнопками форматирования над ним и счётчиком знаков (11.1, А8). */
function TextEditor({
  value,
  onChange,
  length,
  limit,
  problem,
}: {
  value: string
  onChange: (value: string) => void
  length: number
  limit: number
  problem: string | null
}) {
  const area = useRef<HTMLTextAreaElement>(null)
  const [linkOpen, setLinkOpen] = useState(false)
  const [linkUrl, setLinkUrl] = useState('https://')

  /** Ставит теги вокруг выделенного и оставляет выделенным тот же текст. */
  const wrap = (open: string, close: string) => {
    const element = area.current
    if (!element) return
    const { selectionStart: start, selectionEnd: end } = element
    const next = value.slice(0, start) + open + value.slice(start, end) + close + value.slice(end)
    onChange(next)
    requestAnimationFrame(() => {
      element.focus()
      element.setSelectionRange(start + open.length, end + open.length)
    })
  }

  const insertLink = () => {
    const url = linkUrl.trim()
    if (!LINK_URL.test(url)) return
    wrap(`<a href="${escapeMarkup(url)}">`, '</a>')
    setLinkOpen(false)
    setLinkUrl('https://')
  }

  const over = length > limit
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-0.5 rounded-lg border bg-muted/40 p-1" role="toolbar" aria-label="Форматирование">
        <ToolButton label="Жирный" onClick={() => wrap(...FORMATS.bold)}>
          <Bold />
        </ToolButton>
        <ToolButton label="Курсив" onClick={() => wrap(...FORMATS.italic)}>
          <Italic />
        </ToolButton>
        <ToolButton label="Подчёркнутый" onClick={() => wrap(...FORMATS.underline)}>
          <Underline />
        </ToolButton>
        <ToolButton label="Зачёркнутый" onClick={() => wrap(...FORMATS.strikethrough)}>
          <Strikethrough />
        </ToolButton>
        <ToolButton label="Скрытый текст" onClick={() => wrap(...FORMATS.spoiler)}>
          <EyeOff />
        </ToolButton>
        <Popover open={linkOpen} onOpenChange={setLinkOpen}>
          <Tooltip>
            <TooltipTrigger asChild>
              <PopoverTrigger asChild>
                <Button type="button" variant="ghost" size="icon" className="size-8" aria-label="Ссылка">
                  <Link2 />
                </Button>
              </PopoverTrigger>
            </TooltipTrigger>
            <TooltipContent>Ссылка</TooltipContent>
          </Tooltip>
          <PopoverContent className="glass-float" align="start">
            <form
              className="flex flex-col gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                insertLink()
              }}
            >
              <Field id="broadcast-link" label="Адрес ссылки" hint="Выделенный текст станет ссылкой.">
                <Input id="broadcast-link" value={linkUrl} onChange={(event) => setLinkUrl(event.target.value)} autoFocus />
              </Field>
              <Button type="submit" size="sm" disabled={!LINK_URL.test(linkUrl.trim())}>
                Сделать ссылкой
              </Button>
            </form>
          </PopoverContent>
        </Popover>
        <ToolButton label="Цитата" onClick={() => wrap(...FORMATS.quote)}>
          <TextQuote />
        </ToolButton>
        <ToolButton label="Моноширинный" onClick={() => wrap(...FORMATS.code)}>
          <Code />
        </ToolButton>
      </div>
      <Textarea
        ref={area}
        id="broadcast-text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="max-h-[28rem] min-h-40 font-mono text-sm"
        placeholder="Текст рассылки. Выделите слово и нажмите кнопку над полем — например, «Жирный»."
        aria-invalid={over || problem !== null}
      />
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 text-xs">
        <p className={cn('text-muted-foreground', problem && 'text-danger-text')}>
          {problem ?? 'Разметка Telegram: теги ставят кнопки над полем, их можно писать и вручную.'}
        </p>
        <p className={cn('shrink-0 tabular-nums text-muted-foreground', over && 'font-medium text-danger-text')}>
          {formatCount(length)} / {formatCount(limit)}
        </p>
      </div>
    </div>
  )
}

/** Изображение рассылки: одно, JPEG или PNG до 10 МБ (11.1). */
function ImageField({
  broadcast,
  version,
  onChanged,
}: {
  broadcast: Broadcast
  version: number
  onChanged: (broadcast: Broadcast) => void
}) {
  const input = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)
  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setError(null)
    const { data, error: failure } = await fetchClient.PUT('/api/admin/broadcasts/{broadcast_id}/image', {
      params: { path: { broadcast_id: broadcast.id } },
      body: file as unknown as never,
      bodySerializer: (body: unknown) => body as Blob,
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
    })
    if (data) onChanged(data)
    else setError(errorMessage(failure, 'Не удалось загрузить изображение'))
  }
  const remove = async () => {
    const { data } = await fetchClient.DELETE('/api/admin/broadcasts/{broadcast_id}/image', {
      params: { path: { broadcast_id: broadcast.id } },
    })
    if (data) onChanged(data)
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3">
        {broadcast.has_image && (
          <img src={imageUrl(broadcast.id, version)} alt="" className="size-16 shrink-0 rounded-xl object-cover" />
        )}
        <input ref={input} type="file" accept="image/jpeg,image/png" className="hidden" onChange={(event) => void upload(event)} />
        <Button type="button" variant="outline" onClick={() => input.current?.click()}>
          <ImagePlus />
          {broadcast.has_image ? 'Заменить' : 'Добавить изображение'}
        </Button>
        {broadcast.has_image && (
          <Button type="button" variant="ghost" size="icon" onClick={() => void remove()} aria-label="Убрать изображение">
            <Trash2 />
          </Button>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        JPEG или PNG до 10 МБ. С изображением текст становится подписью — до {formatCount(MAX_CAPTION_LENGTH)} знаков.
      </p>
      {error && <p className="text-sm text-danger-text">{error}</p>}
    </div>
  )
}

/** Кнопки рассылки: «Купить», «Продлить», до трёх ссылок; «Главное меню» — всегда (11.1). */
function ButtonsField({ content, onChange }: { content: Content; onChange: (content: Content) => void }) {
  const setLink = (index: number, patch: Partial<LinkButton>) =>
    onChange({ ...content, links: content.links.map((link, other) => (other === index ? { ...link, ...patch } : link)) })
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
        <label className="flex items-center gap-2">
          <Checkbox checked={content.buy} onCheckedChange={(checked) => onChange({ ...content, buy: checked === true })} />
          «Купить подписку»
        </label>
        <label className="flex items-center gap-2">
          <Checkbox checked={content.renew} onCheckedChange={(checked) => onChange({ ...content, renew: checked === true })} />
          «Продлить»
        </label>
      </div>
      <p className="text-xs text-muted-foreground">
        Клиент с платной подпиской по обеим кнопкам попадает к продлению, без подписок — к выбору тарифа.
        «Главное меню» добавляется всегда.
      </p>
      {content.links.map((link, index) => {
        const problem = link.text || link.url !== 'https://' ? linkProblem(link) : null
        return (
          <div key={index} className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2 sm:flex-nowrap">
              <Input
                aria-label="Подпись кнопки"
                placeholder="Подпись кнопки"
                maxLength={MAX_BUTTON_TEXT}
                value={link.text}
                onChange={(event) => setLink(index, { text: event.target.value })}
                className="sm:w-48"
              />
              <Input
                aria-label="Адрес ссылки"
                placeholder="https://"
                value={link.url}
                onChange={(event) => setLink(index, { url: event.target.value })}
                className="min-w-0 flex-1"
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="Убрать кнопку"
                onClick={() => onChange({ ...content, links: content.links.filter((_, other) => other !== index) })}
              >
                <X />
              </Button>
            </div>
            {problem && <p className="text-xs text-danger-text">{problem}</p>}
          </div>
        )
      })}
      {content.links.length < MAX_LINKS && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="self-start"
          onClick={() => onChange({ ...content, links: [...content.links, { text: '', url: 'https://' }] })}
        >
          <Plus />
          Кнопка-ссылка
        </Button>
      )}
    </div>
  )
}

function recipientsText(count: number): string {
  return `${formatCount(count)} ${plural(count, 'получатель', 'получателя', 'получателей')}`
}

/** Черновик: содержимое, сегмент, «Отправить себе», подтверждение (11.1–11.4). */
function DraftEditor({ broadcast }: { broadcast: Broadcast }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [form, setForm] = useState<Form>(() => formOf(broadcast))
  const [saved, setSaved] = useState<Form>(() => formOf(broadcast))
  const [current, setCurrent] = useState(broadcast)
  const [imageVersion, setImageVersion] = useState(0)
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null)
  const [confirming, setConfirming] = useState(false)
  const segments = $api.useQuery('get', '/api/admin/broadcasts/segments')
  const save = $api.useMutation('put', '/api/admin/broadcasts/{broadcast_id}')
  const test = $api.useMutation('post', '/api/admin/broadcasts/{broadcast_id}/test')
  const confirm = $api.useMutation('post', '/api/admin/broadcasts/{broadcast_id}/confirm')
  const remove = $api.useMutation('delete', '/api/admin/broadcasts/{broadcast_id}')
  const path = { params: { path: { broadcast_id: broadcast.id } } }

  const parsed = useMemo(() => parseMarkup(form.content.text), [form.content.text])
  const limit = current.has_image ? MAX_CAPTION_LENGTH : MAX_TEXT_LENGTH
  const linksValid = form.content.links.every((link) => linkProblem(link) === null)
  const empty = parsed.length === 0 && !current.has_image
  const ready = parsed.problem === null && parsed.length <= limit && linksValid && !empty
  const dirty = !sameForm(form, saved)
  const { mutateAsync: saveAsync } = save

  const persist = async (next: Form): Promise<boolean> => {
    if (!next.content.links.every((link) => linkProblem(link) === null)) return false
    try {
      const updated = await saveAsync({ ...path, body: { content: next.content, segment: { kind: next.segment } } })
      setSaved(formOf(updated))
      setCurrent(updated)
      return true
    } catch (failure) {
      setNotice({ text: errorMessage(failure, 'Не удалось сохранить черновик'), error: true })
      return false
    }
  }

  useEffect(() => {
    if (!dirty || !linksValid) return
    const timer = setTimeout(() => void persist(form), SAVE_DELAY_MS)
    return () => clearTimeout(timer)
    // persist меняется на каждом рендере; сохранение зависит только от формы
  }, [form, dirty, linksValid])

  const change = (patch: Partial<Form>) => setForm({ ...form, ...patch })
  const counts = segments.data?.segments ?? []
  const chosen = counts.find((segment) => segment.kind === form.segment)

  const sendTest = async () => {
    setNotice(null)
    if (dirty && !(await persist(form))) return
    test.mutate(path, {
      onSuccess: () => setNotice({ text: 'Тестовое сообщение отправлено вам в бот.', error: false }),
      onError: (failure) => setNotice({ text: errorMessage(failure, 'Не удалось отправить тестовое сообщение'), error: true }),
    })
  }

  const openConfirm = async () => {
    setNotice(null)
    if (dirty && !(await persist(form))) return
    void segments.refetch()
    setConfirming(true)
  }

  const start = () =>
    confirm.mutate(path, {
      onSuccess: () => {
        setConfirming(false)
        void queryClient.invalidateQueries()
      },
      onError: (failure) => {
        setConfirming(false)
        setNotice({ text: errorMessage(failure, 'Не удалось начать рассылку'), error: true })
      },
    })

  const discard = () =>
    remove.mutate(path, {
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: ['get', '/api/admin/broadcasts'] })
        void navigate('/broadcasts')
      },
    })

  const status = save.isPending ? 'Сохраняем…' : dirty ? 'Есть несохранённые изменения' : 'Черновик сохранён'

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="flex min-w-0 flex-col gap-6">
        <Module title="Сообщение" action={<span className="text-xs text-muted-foreground">{status}</span>}>
          <div className="flex flex-col gap-5">
            <TextEditor
              value={form.content.text}
              onChange={(text) => change({ content: { ...form.content, text } })}
              length={parsed.length}
              limit={limit}
              problem={parsed.problem}
            />
            <ImageField
              broadcast={current}
              version={imageVersion}
              onChanged={(updated) => {
                setCurrent(updated)
                setImageVersion(imageVersion + 1)
              }}
            />
          </div>
        </Module>
        <Module title="Кнопки">
          <ButtonsField content={form.content} onChange={(content) => change({ content })} />
        </Module>
        <Module title="Кому">
          <div className="flex flex-col gap-2">
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={form.segment}
              onValueChange={(value) => value && change({ segment: value as SegmentKind })}
              className="flex-wrap"
            >
              {(Object.keys(SEGMENTS) as SegmentKind[]).map((kind) => {
                const count = counts.find((segment) => segment.kind === kind)?.recipients
                return (
                  <ToggleGroupItem key={kind} value={kind} className={cn(CHOSEN, 'h-auto py-1.5')}>
                    {SEGMENTS[kind]}
                    {count !== undefined && <span className="tabular-nums text-muted-foreground">{formatCount(count)}</span>}
                  </ToggleGroupItem>
                )
              })}
            </ToggleGroup>
            <p className="text-xs text-muted-foreground">
              С активной — хотя бы одна подписка действует или исчерпала трафик, включая пробные. С истёкшей —
              подписки есть, но ни одна не действует. Клиенты с пометкой «бот заблокирован» входят: бот могли
              разблокировать.
            </p>
          </div>
        </Module>
      </div>

      <div className="flex min-w-0 flex-col gap-4 lg:sticky lg:top-6 lg:self-start">
        <Module title="Как увидит клиент">
          <MessagePreview
            nodes={parsed.problem ? [{ kind: 'text', text: form.content.text.trim() }] : parsed.nodes}
            content={form.content}
            imageUrl={current.has_image ? imageUrl(current.id, imageVersion) : null}
          />
        </Module>
        {notice && (
          <p className={cn('text-sm', notice.error ? 'text-danger-text' : 'text-success-text')} role="status">
            {notice.text}
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => void sendTest()} disabled={!ready || test.isPending}>
            Отправить себе
          </Button>
          <Button onClick={() => void openConfirm()} disabled={!ready || save.isPending}>
            <Send />
            Отправить…
          </Button>
          <Button variant="ghost" onClick={discard} disabled={remove.isPending} className="ml-auto text-muted-foreground">
            <Trash2 />
            Удалить черновик
          </Button>
        </div>
      </div>

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent className="glass-float sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Отправить рассылку?</DialogTitle>
            <DialogDescription>
              {chosen ? (
                <>
                  {SEGMENTS[form.segment]}: <span className="font-medium text-foreground">{recipientsText(chosen.recipients)}</span>.
                </>
              ) : (
                'Считаем получателей…'
              )}{' '}
              Список получателей фиксируется сейчас. Каждый получит сообщение один раз; остановить рассылку можно в
              любой момент.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="ghost">
                Отмена
              </Button>
            </DialogClose>
            <Button onClick={start} disabled={confirm.isPending || !chosen || chosen.recipients === 0}>
              <Send />
              Отправить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

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

/** Отправленная рассылка: прогресс и «Остановить» (11.5, 11.7), итог (11.8). */
function SentBroadcast({ broadcast, timeZone }: { broadcast: Broadcast; timeZone: string | undefined }) {
  const queryClient = useQueryClient()
  const [stopping, setStopping] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const halt = $api.useMutation('post', '/api/admin/broadcasts/{broadcast_id}/stop')
  const { totals } = broadcast
  const done = totals.recipients - totals.pending - totals.skipped
  const share = totals.recipients > 0 ? (done / totals.recipients) * 100 : 0
  const parsed = useMemo(() => parseMarkup(broadcast.content.text), [broadcast.content.text])

  const stop = () =>
    halt.mutate(
      { params: { path: { broadcast_id: broadcast.id } } },
      {
        onSuccess: () => {
          setStopping(false)
          void queryClient.invalidateQueries()
        },
        onError: (failure) => {
          setStopping(false)
          setError(errorMessage(failure, 'Не удалось остановить рассылку'))
        },
      },
    )

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="flex min-w-0 flex-col gap-6">
        <Module
          title={broadcast.state === 'sending' ? 'Идёт рассылка' : 'Итог'}
          action={
            broadcast.state === 'sending' && (
              <Button variant="outline" onClick={() => setStopping(true)}>
                Остановить
              </Button>
            )
          }
        >
          <div className="flex flex-col gap-5">
            <div className="flex flex-col gap-2">
              <div className="h-2 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuenow={Math.round(share)} aria-valuemin={0} aria-valuemax={100}>
                <div className="h-full rounded-full bg-brand transition-[width] duration-500" style={{ width: `${share}%` }} />
              </div>
              <p className="text-sm text-muted-foreground">
                Обработано {formatCount(done)} из {recipientsText(totals.recipients)}
                {broadcast.state === 'sending' && ` · осталось ${formatCount(totals.pending)}`}
              </p>
            </div>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Доставлено" value={totals.delivered} />
              <Stat label="Бот заблокирован" value={totals.bot_blocked} tone="warning" />
              <Stat label="Ошибки" value={totals.failed} tone="danger" />
              <Stat label="Не отправлено" value={totals.skipped} />
            </div>
            {error && <p className="text-sm text-danger-text">{error}</p>}
          </div>
        </Module>
        <Module title="Подробности">
          <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_1fr]">
            <dt className="text-muted-foreground">Кому</dt>
            <dd>{SEGMENTS[broadcast.segment.kind]}</dd>
            {broadcast.confirmed_at && timeZone && (
              <>
                <dt className="text-muted-foreground">Отправлена</dt>
                <dd>{formatDateTime(broadcast.confirmed_at, timeZone)}</dd>
              </>
            )}
            {broadcast.finished_at && timeZone && (
              <>
                <dt className="text-muted-foreground">{broadcast.state === 'stopped' ? 'Остановлена' : 'Завершена'}</dt>
                <dd>{formatDateTime(broadcast.finished_at, timeZone)}</dd>
              </>
            )}
          </dl>
        </Module>
      </div>
      <Module title="Сообщение" className="lg:self-start">
        <MessagePreview
          nodes={parsed.nodes}
          content={broadcast.content}
          imageUrl={broadcast.has_image ? imageUrl(broadcast.id, 'sent') : null}
        />
      </Module>

      <Dialog open={stopping} onOpenChange={setStopping}>
        <DialogContent className="glass-float sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Остановить рассылку?</DialogTitle>
            <DialogDescription>
              Оставшимся {formatCount(totals.pending)} получателям сообщение не уйдёт. Продолжить остановленную
              рассылку нельзя.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="ghost">
                Не останавливать
              </Button>
            </DialogClose>
            <Button variant="destructive" onClick={stop} disabled={halt.isPending}>
              Остановить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

/** А8. Рассылка: черновик — редактор; отправленная — прогресс и итог. */
export function BroadcastPage() {
  const { broadcastId } = useParams()
  const id = Number(broadcastId)
  const history = $api.useQuery('get', '/api/admin/broadcasts')
  const { data, error } = $api.useQuery(
    'get',
    '/api/admin/broadcasts/{broadcast_id}',
    { params: { path: { broadcast_id: id } } },
    { refetchInterval: (query) => (query.state.data?.state === 'sending' ? PROGRESS_REFRESH_MS : false) },
  )
  return (
    <div className="flex flex-col gap-6">
      <Link to="/broadcasts" className="flex items-center gap-1.5 self-start text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" />
        Рассылки
      </Link>
      {error ? (
        <p className="text-sm text-danger-text">{errorMessage(error, 'Рассылка не найдена')}</p>
      ) : !data ? (
        <p className="text-sm text-muted-foreground">Загрузка…</p>
      ) : (
        <>
          <PageTitle action={<StateBadge state={data.state} />}>
            {data.state === 'draft' ? 'Новая рассылка' : 'Рассылка'}
          </PageTitle>
          {data.state === 'draft' ? (
            <DraftEditor key={data.id} broadcast={data} />
          ) : (
            <SentBroadcast broadcast={data} timeZone={history.data?.time_zone} />
          )}
        </>
      )}
    </div>
  )
}
