import { useQueryClient } from '@tanstack/react-query'
import { ChevronDown, RotateCcw, Search, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { $api, errorMessage, type Schemas } from '@/api/client'
import { Module } from '@/components/frame/Module'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from '@/components/ui/input-group'
import { Textarea } from '@/components/ui/textarea'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'
import { MessageBubble, PreviewButton } from '@/pages/broadcasts/MessagePreview'

type BotText = Schemas['BotTextOut']
type Group = Schemas['TextGroupOut']
type Filter = 'all' | 'changed'

const TEXTS_PATH = '/api/admin/settings/texts'
const PREVIEW_DELAY_MS = 300
// Язык в подписи переключателя — когда языков больше одного (v1, решение 0018)
const LANGUAGE_NAMES: Record<string, string> = { ru: 'Русский', en: 'English' }

/** Группы подряд из одного раздела 03-bot-texts.md («События») — в одном модуле. */
function modules(groups: Group[]): { title: string; groups: Group[] }[] {
  const result: { title: string; groups: Group[] }[] = []
  for (const group of groups) {
    const title = group.section ?? group.title
    const last = result.at(-1)
    if (last && last.title === title) last.groups.push(group)
    else result.push({ title, groups: [group] })
  }
  return result
}

function matches(text: BotText, group: Group, query: string): boolean {
  if (!query) return true
  const haystack = [text.key, text.text, text.default, group.title, group.section ?? ''].join('\n').toLowerCase()
  return haystack.includes(query)
}

function pluralHint(counts: number[]): string {
  return `Форма для чисел ${counts.slice(0, 6).join(', ')}…`
}

/** Текст так, как его увидит клиент: сообщение, кнопка, пункт меню команд или
 *  страница-переходник. */
function TextPreview({ item, text }: { item: BotText; text: string }) {
  switch (item.kind) {
    case 'button':
      return <PreviewButton>{text}</PreviewButton>
    case 'command':
      return (
        <div className="flex flex-col gap-0.5 rounded-xl border bg-background px-3.5 py-2.5 text-sm shadow-sm">
          <span className="font-medium">/{item.key.split('.').at(-1)}</span>
          <span className="break-words text-muted-foreground">{text}</span>
        </div>
      )
    case 'page':
      return (
        <div className="rounded-xl border bg-background px-4 py-5 text-center text-sm break-words whitespace-pre-wrap shadow-sm">{text}</div>
      )
    default:
      return <MessageBubble>{text}</MessageBubble>
  }
}

// Пояснения к текстам, смысл которых по ключу не ясен (03-bot-texts.md)
const NOTES: Record<string, string> = {
  'date.fallback':
    'Запасной текст даты: его видят клиенты, у которых Telegram не показывает дату сам. Дата и время — в часовом поясе магазина; если пояс не московский, поменяйте подпись.',
}

const PREVIEW_LABEL: Record<BotText['kind'], string> = {
  message: 'Так клиент увидит сообщение',
  button: 'Так клиент увидит кнопку',
  command: 'Так команда видна в меню бота',
  page: 'Так текст выглядит на странице-переходнике',
}

/** Редактор одного текста: поле, переменные, предпросмотр, сохранение и сброс (1.29). */
function TextEditor({
  item,
  language,
  onDone,
}: {
  item: BotText
  language: string
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(item.text)
  const area = useRef<HTMLTextAreaElement>(null)
  const preview = $api.useMutation('post', '/api/admin/settings/texts/{language}/{key}/preview')
  const save = $api.useMutation('put', '/api/admin/settings/texts/{language}/{key}')
  const reset = $api.useMutation('delete', '/api/admin/settings/texts/{language}/{key}')
  const { mutate: requestPreview } = preview
  const path = { language, key: item.key }

  useEffect(() => {
    const timer = window.setTimeout(() => {
      requestPreview({ params: { path: { language, key: item.key } }, body: { text: draft } })
    }, PREVIEW_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [draft, language, item.key, requestPreview])

  const refresh = () => {
    // Приветствие — тот же текст, что в «Бренд» (1.11): его форма перечитывается тоже
    for (const key of [TEXTS_PATH, '/api/admin/settings/brand']) {
      void queryClient.invalidateQueries({ queryKey: ['get', key] })
    }
  }
  const insert = (name: string) => {
    const field = area.current
    const token = `{${name}}`
    const start = field?.selectionStart ?? draft.length
    const end = field?.selectionEnd ?? draft.length
    setDraft(draft.slice(0, start) + token + draft.slice(end))
    window.requestAnimationFrame(() => {
      field?.focus()
      field?.setSelectionRange(start + token.length, start + token.length)
    })
  }

  const problem = preview.data?.problem ?? null
  const shown = preview.data?.text ?? null
  const unchanged = draft.trim() === item.text
  const busy = save.isPending || reset.isPending
  const failure = save.error ?? reset.error

  return (
    <div className="mt-2 flex flex-col gap-4 rounded-2xl bg-muted/50 p-4">
      <div className="grid gap-4 md:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-2">
          <Textarea
            ref={area}
            aria-label={`Текст ${item.key}`}
            aria-invalid={problem !== null}
            rows={item.kind === 'message' ? 4 : 2}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            // Поле растёт по тексту само: системный уголок для растягивания не нужен
            className="resize-none bg-background dark:bg-background"
          />
          {item.variables.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Переменные:</span>
              {item.variables.map((name) => (
                <button
                  key={name}
                  type="button"
                  onClick={() => insert(name)}
                  className="rounded-full bg-background px-2 py-0.5 font-mono text-xs text-brand-text transition-colors hover:bg-brand-soft"
                  title="Вставить в текст"
                >
                  {`{${name}}`}
                </button>
              ))}
            </div>
          )}
          {problem && <p className="text-sm text-danger-text">{problem}</p>}
          {NOTES[item.key] && <p className="text-xs text-muted-foreground">{NOTES[item.key]}</p>}
          <p className="text-xs break-words text-muted-foreground">
            {item.plural_counts && <>{pluralHint(item.plural_counts)}. </>}
            По умолчанию: <span className="whitespace-pre-wrap">{item.default}</span>
          </p>
        </div>
        <div className="flex min-w-0 flex-col gap-2">
          <span className="text-xs text-muted-foreground">{PREVIEW_LABEL[item.kind]}</span>
          {shown !== null ? (
            <TextPreview item={item} text={shown} />
          ) : (
            <div className="rounded-xl border border-dashed px-3.5 py-2.5 text-sm text-muted-foreground">
              {problem ? 'Предпросмотра нет, пока в тексте ошибка.' : 'Собираем предпросмотр…'}
            </div>
          )}
          {shown !== null && item.variables.length > 0 && (
            <span className="text-xs text-muted-foreground">Вместо переменных — примеры значений; даты клиент видит в своём часовом поясе.</span>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          disabled={busy || unchanged || problem !== null || shown === null}
          onClick={() =>
            save.mutate({ params: { path }, body: { text: draft } }, { onSuccess: () => { refresh(); onDone() } })
          }
        >
          Сохранить
        </Button>
        <Button variant="ghost" disabled={busy} onClick={onDone}>
          Отменить
        </Button>
        {item.changed && (
          <Button
            variant="outline"
            className="sm:ml-auto"
            disabled={busy}
            onClick={() => reset.mutate({ params: { path } }, { onSuccess: () => { refresh(); onDone() } })}
          >
            <RotateCcw />
            Вернуть текст по умолчанию
          </Button>
        )}
        {failure && <p className="w-full text-sm text-danger-text">{errorMessage(failure, 'Не удалось сохранить')}</p>}
      </div>
    </div>
  )
}

function TextRow({
  item,
  open,
  language,
  onToggle,
}: {
  item: BotText
  open: boolean
  language: string
  onToggle: () => void
}) {
  return (
    <li className="py-2">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full min-w-0 items-start gap-3 rounded-xl px-2 py-1.5 text-left transition-colors hover:bg-muted/60"
      >
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs break-all text-muted-foreground">{item.key}</span>
            {item.changed && <Badge className="bg-brand-soft text-brand-text">изменён</Badge>}
          </span>
          <span className="line-clamp-2 text-sm break-words whitespace-pre-line">{item.text}</span>
        </span>
        <ChevronDown className={cn('mt-1 size-4 shrink-0 text-muted-foreground transition-transform duration-150', open && 'rotate-180')} aria-hidden />
      </button>
      {open && <TextEditor key={`${language}:${item.key}:${item.text}`} item={item} language={language} onDone={onToggle} />}
    </li>
  )
}

/** «Тексты бота» (1.29): все тексты по экранам, поиск, правка с предпросмотром, сброс. */
export function TextsSettingsPage() {
  const [language, setLanguage] = useState<string | null>(null)
  const { data } = $api.useQuery('get', TEXTS_PATH, { params: { query: language ? { language } : {} } })
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [openKey, setOpenKey] = useState<string | null>(null)

  const query = search.trim().toLowerCase()
  const visible = useMemo(() => {
    if (!data) return []
    return modules(
      data.groups
        .map((group) => ({
          ...group,
          texts: group.texts.filter((text) => (filter === 'all' || text.changed) && matches(text, group, query)),
        }))
        .filter((group) => group.texts.length > 0),
    )
  }, [data, filter, query])

  if (!data) return null
  const changedCount = data.groups.reduce((sum, group) => sum + group.texts.filter((text) => text.changed).length, 0)

  return (
    <div className="flex flex-col gap-4">
      <Module
        title="Тексты бота"
        action={
          data.languages.length > 1 && (
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={data.language}
              onValueChange={(value) => value && setLanguage(value)}
              aria-label="Язык"
            >
              {data.languages.map((code) => (
                <ToggleGroupItem key={code} value={code}>
                  {LANGUAGE_NAMES[code] ?? code}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          )
        }
      >
        <div className="flex flex-col gap-3">
          <p className="text-sm text-muted-foreground">
            Всё, что пишет бот, — по экранам. Переменные в фигурных скобках подставляет магазин: их можно переставлять и убирать, но не
            придумывать новые. Изменение действует сразу.
          </p>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <InputGroup className="h-10 sm:flex-1">
              <InputGroupAddon>
                <Search aria-hidden />
              </InputGroupAddon>
              <InputGroupInput
                inputMode="search"
                placeholder="Слово из текста, ключ или экран"
                aria-label="Поиск текста"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
              {search && (
                <InputGroupAddon align="inline-end">
                  <InputGroupButton size="icon-xs" aria-label="Очистить поиск" onClick={() => setSearch('')}>
                    <X />
                  </InputGroupButton>
                </InputGroupAddon>
              )}
            </InputGroup>
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={filter}
              onValueChange={(value) => value && setFilter(value as Filter)}
              aria-label="Какие тексты показать"
              className="w-fit"
            >
              <ToggleGroupItem value="all">Все</ToggleGroupItem>
              <ToggleGroupItem value="changed">Изменённые · {changedCount}</ToggleGroupItem>
            </ToggleGroup>
          </div>
          {visible.length === 0 && (
            <p className="text-sm text-muted-foreground">
              {filter === 'changed' && !query ? 'Все тексты — по умолчанию.' : 'Ничего не нашли. Проверьте, нет ли опечатки.'}
            </p>
          )}
        </div>
      </Module>

      {visible.map((module) => (
        <Module key={module.title} title={module.title}>
          <div className="flex flex-col gap-4">
            {module.groups.map((group) => (
              <div key={group.id} className="flex flex-col">
                {group.title !== module.title && <h3 className="px-2 text-sm font-medium text-muted-foreground">{group.title}</h3>}
                <ul className="divide-y">
                  {group.texts.map((item) => (
                    <TextRow
                      key={item.key}
                      item={item}
                      language={data.language}
                      open={openKey === item.key}
                      onToggle={() => setOpenKey(openKey === item.key ? null : item.key)}
                    />
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Module>
      ))}
    </div>
  )
}
