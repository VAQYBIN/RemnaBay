import { useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Trash2, Upload } from 'lucide-react'
import { type ChangeEvent, useEffect, useRef, useState } from 'react'

import { $api, errorMessage, fetchClient, type Schemas } from '@/api/client'
import { ColorField } from '@/components/ColorField'
import { Field } from '@/components/Field'
import { Module } from '@/components/frame/Module'
import { RemnaBayMark } from '@/components/frame/RemnaBayMark'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { brandCss } from '@/lib/brand'

type Settings = Schemas['BrandSettingsOut']
type Palette = Schemas['PaletteOut']
type AssetKind = 'mark' | 'logo'

const HEX = /^#[0-9a-f]{6}$/i
const SEMANTIC_NAMES: Record<string, string> = {
  success: '«успех»',
  warning: '«внимание»',
  danger: '«ошибка»',
}

function warningText(palette: Palette): string | null {
  if (palette.warnings.length === 0) return null
  const names = palette.warnings.map((name) => SEMANTIC_NAMES[name] ?? name).join(' и ')
  return `Основной цвет по тону близок к смысловому цвету ${names}. Сохранить можно, но клиенты и команда могут спутать бренд со статусом.`
}

/** Предпросмотр рамы в обеих темах с цветом, каким он станет (1.24). */
function Preview({ palette }: { palette: Palette }) {
  const css = brandCss(palette.tokens).replaceAll(':root,\n:root[data-theme="dark"]', '.rb-preview-dark').replaceAll(':root[data-theme="light"]', '.rb-preview-light')
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <style>{css}</style>
      {(['light', 'dark'] as const).map((theme) => (
        <div
          key={theme}
          data-theme={theme}
          className={`rb-preview-${theme} rounded-xl p-4`}
          style={{
            backgroundColor: 'var(--rb-bg)',
            backgroundImage: 'radial-gradient(14rem 10rem at 0% 0%, var(--rb-glow-1), transparent 70%)',
          }}
        >
          <div className="flex flex-col gap-3 rounded-xl p-4" style={{ background: 'var(--rb-glass-module-solid)', color: 'var(--rb-text)' }}>
            <p className="text-sm font-medium">{theme === 'light' ? 'Светлая тема' : 'Тёмная тема'}</p>
            <p className="text-sm" style={{ color: 'var(--rb-text-muted)' }}>
              Подпись и <span style={{ color: 'var(--rb-primary-text)' }}>ссылка</span>
            </p>
            <span
              className="inline-flex h-9 w-fit items-center rounded-lg px-4 text-sm font-medium"
              style={{ background: 'var(--rb-primary)', color: 'var(--rb-on-primary)' }}
            >
              Главная кнопка
            </span>
            <span className="tabular text-xs" style={{ color: 'var(--rb-text-muted)' }}>
              Кнопка: {theme === 'light' ? palette.light_primary : palette.dark_primary}
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}

function AssetUpload({
  kind,
  settings,
  onChanged,
}: {
  kind: AssetKind
  settings: Settings
  onChanged: (settings: Settings) => void
}) {
  const input = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)
  const asset = kind === 'mark' ? settings.mark : settings.logo
  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setError(null)
    const { data, error: failure } = await fetchClient.PUT('/api/admin/settings/brand/assets/{kind}', {
      params: { path: { kind } },
      body: file as unknown as never,
      bodySerializer: (body: unknown) => body as Blob,
      headers: { 'Content-Type': file.type },
    })
    if (data) onChanged(data)
    else setError(errorMessage(failure, 'Не удалось загрузить файл'))
  }
  const remove = async () => {
    const { data } = await fetchClient.DELETE('/api/admin/settings/brand/assets/{kind}', {
      params: { path: { kind } },
    })
    if (data) onChanged(data)
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3">
        <div className="flex size-16 shrink-0 items-center justify-center rounded-xl bg-muted p-2">
          {asset ? (
            <img src={asset.url} alt="" className="max-h-full max-w-full object-contain" />
          ) : kind === 'mark' ? (
            <RemnaBayMark className="size-12 opacity-60" />
          ) : (
            <span className="text-xs text-muted-foreground">нет</span>
          )}
        </div>
        <input ref={input} type="file" accept="image/svg+xml,image/png" className="hidden" onChange={(event) => void upload(event)} />
        <Button variant="outline" onClick={() => input.current?.click()}>
          <Upload />
          {asset ? 'Заменить' : 'Загрузить'}
        </Button>
        {asset && (
          <Button variant="ghost" size="icon" onClick={() => void remove()} aria-label="Удалить">
            <Trash2 />
          </Button>
        )}
      </div>
      {error && <p className="text-sm text-danger-text">{error}</p>}
    </div>
  )
}

/** «Бренд» (1.11, 1.23, 1.24): название, логотипы, цвета, приветственный текст. */
export function BrandSettingsPage() {
  const queryClient = useQueryClient()
  const { data } = $api.useQuery('get', '/api/admin/settings/brand')
  const [form, setForm] = useState<{ name: string; primary: string; secondary: string; welcome: string } | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const preview = $api.useMutation('post', '/api/admin/settings/brand/preview')
  const save = $api.useMutation('put', '/api/admin/settings/brand')
  const { mutate: requestPreview } = preview

  useEffect(() => {
    if (data && form === null) {
      setForm({
        name: data.name,
        primary: data.primary_color,
        secondary: data.secondary_color ?? '',
        welcome: data.welcome_text,
      })
    }
  }, [data, form])

  const primary = form?.primary ?? ''
  const secondary = form?.secondary ?? ''
  useEffect(() => {
    if (!HEX.test(primary) || (secondary !== '' && !HEX.test(secondary))) return
    const timer = window.setTimeout(() => {
      requestPreview({ body: { primary_color: primary, secondary_color: secondary || null } })
    }, 250)
    return () => window.clearTimeout(timer)
  }, [primary, secondary, requestPreview])

  if (!data || !form) return null
  const palette = preview.data ?? data.palette
  // Изменение бренда видно сразу, без перезапуска (1.23): рама перечитывает бренд
  const refreshBrand = (_next: Settings) => {
    for (const path of ['/api/admin/settings/brand', '/api/admin/brand', '/api/admin/checklist']) {
      void queryClient.invalidateQueries({ queryKey: ['get', path] })
    }
  }
  const submit = () => {
    setSaved(null)
    save.mutate(
      {
        body: {
          name: form.name,
          primary_color: form.primary,
          secondary_color: form.secondary || null,
          welcome_text: form.welcome,
        },
      },
      {
        onSuccess: (next) => {
          refreshBrand(next)
          setSaved(warningText(next.palette) ?? 'Сохранено')
        },
      },
    )
  }
  const warning = warningText(palette)

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Module title="Название и логотип">
        <div className="flex flex-col gap-5">
          <Field id="brand-name" label="Название" hint="Видно клиентам в боте и команде в админке.">
            <Input id="brand-name" value={form.name} maxLength={64} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field id="brand-mark" label="Квадратный знак" hint="Обязателен. SVG или PNG с прозрачным фоном, не меньше 512×512.">
            <AssetUpload kind="mark" settings={data} onChanged={refreshBrand} />
          </Field>
          <Field id="brand-logo" label="Горизонтальный логотип" hint="По желанию. Без него показываются знак и название.">
            <AssetUpload kind="logo" settings={data} onChanged={refreshBrand} />
          </Field>
        </div>
      </Module>

      <Module title="Цвета">
        <div className="flex flex-col gap-5">
          <Field id="brand-primary" label="Основной фирменный цвет" hint="Главная кнопка, активный пункт меню, ссылки.">
            <ColorField id="brand-primary" value={form.primary} onChange={(value) => setForm({ ...form, primary: value })} />
          </Field>
          <Field
            id="brand-secondary"
            label="Дополнительный цвет"
            hint={
              <>
                По желанию: выделения и бейджи. Не используется для главной кнопки.{' '}
                {form.secondary && (
                  <button type="button" className="text-brand-text underline-offset-4 hover:underline" onClick={() => setForm({ ...form, secondary: '' })}>
                    Убрать
                  </button>
                )}
              </>
            }
          >
            <ColorField id="brand-secondary" emptyLabel="не задан" value={form.secondary} onChange={(value) => setForm({ ...form, secondary: value })} />
          </Field>
          {palette.adjusted && (
            <p className="text-sm text-muted-foreground">
              Ради читаемости цвет подстроен: в светлой теме кнопка — <span className="tabular">{palette.light_primary}</span>, в
              тёмной — <span className="tabular">{palette.dark_primary}</span>. Тон сохранён.
            </p>
          )}
          {warning && (
            <Alert className="border-warning-text/40 bg-warning-soft">
              <AlertTriangle className="text-warning-text" />
              <AlertDescription className="text-foreground">{warning}</AlertDescription>
            </Alert>
          )}
          <Preview palette={palette} />
        </div>
      </Module>

      <Module title="Приветственный текст" className="lg:col-span-2">
        <Field
          id="brand-welcome"
          label="Текст в главном меню бота для нового клиента"
          hint={data.welcome_text_is_default ? 'Сейчас — текст по умолчанию.' : 'Пустое поле вернёт текст по умолчанию.'}
        >
          <Textarea id="brand-welcome" rows={3} maxLength={1000} value={form.welcome} onChange={(e) => setForm({ ...form, welcome: e.target.value })} />
        </Field>
      </Module>

      <div className="flex flex-wrap items-center gap-3 lg:col-span-2">
        <Button onClick={submit} disabled={save.isPending}>
          Сохранить
        </Button>
        {save.error && <p className="text-sm text-danger-text">{errorMessage(save.error, 'Не удалось сохранить')}</p>}
        {saved && !save.error && <p className="text-sm text-muted-foreground">{saved}</p>}
      </div>
    </div>
  )
}
