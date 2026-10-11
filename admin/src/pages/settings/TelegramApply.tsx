import { useEffect, useRef, useState } from 'react'

import { errorMessage, fetchClient, type Schemas } from '@/api/client'
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
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { formatDateTime } from '@/lib/format'

type Settings = Schemas['BrandSettingsOut']
type Background = Schemas['AvatarBackground']

// Сторона аватара: Telegram принимает квадратный JPG
const SIZE = 640
// Знак вписан в круг аватара с отступом: сторона квадрата в круге — 0,71 диаметра
const MARK_SHARE = 0.58
const BACKGROUNDS: { value: Background; label: string }[] = [
  { value: 'brand', label: 'Фирменный цвет' },
  { value: 'white', label: 'Белый' },
  { value: 'black', label: 'Чёрный' },
]

function fill(background: Background, brand: string): string {
  if (background === 'white') return '#ffffff'
  if (background === 'black') return '#000000'
  return brand
}

/** Знак как картинка для холста. У SVG без width и height браузер не знает размера —
 *  задаём его, сохраняя viewBox. */
async function loadMark(url: string): Promise<HTMLImageElement> {
  const response = await fetch(url, { credentials: 'same-origin' })
  let blob = await response.blob()
  if (blob.type.startsWith('image/svg')) {
    const document = new DOMParser().parseFromString(await blob.text(), 'image/svg+xml')
    const svg = document.documentElement
    svg.setAttribute('width', String(SIZE))
    svg.setAttribute('height', String(SIZE))
    blob = new Blob([new XMLSerializer().serializeToString(svg)], { type: 'image/svg+xml' })
  }
  const source = URL.createObjectURL(blob)
  try {
    const image = new Image()
    image.src = source
    await image.decode()
    return image
  } finally {
    URL.revokeObjectURL(source)
  }
}

function draw(canvas: HTMLCanvasElement, mark: HTMLImageElement, background: string): void {
  const context = canvas.getContext('2d')
  if (!context) return
  context.fillStyle = background
  context.fillRect(0, 0, SIZE, SIZE)
  const side = SIZE * MARK_SHARE
  const offset = (SIZE - side) / 2
  context.drawImage(mark, offset, offset, side, side)
}

function toJpeg(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('jpeg'))), 'image/jpeg', 0.92)
  })
}

/** Итог последнего применения: когда и с какой ошибкой Telegram (1.27). */
export function TelegramResult({ settings, timeZone }: { settings: Settings; timeZone: string }) {
  const result = settings.telegram
  if (!result) return null
  if (result.ok) {
    return (
      <p className="text-sm text-muted-foreground">Применено в Telegram {formatDateTime(result.at, timeZone)}.</p>
    )
  }
  return (
    <p className="text-sm text-danger-text">
      {formatDateTime(result.at, timeZone)} Telegram ответил ошибкой: {result.error}
    </p>
  )
}

/** «Применить в Telegram» (1.27, решения 0058, 0059): имя, описания и аватар бота.
 *  Аватар — квадратный знак в круге на фоне, выбранном по круглому предпросмотру;
 *  JPG собирается здесь, из того же холста, что и предпросмотр. */
export function TelegramApplyDialog({
  open,
  onOpenChange,
  settings,
  onApplied,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  settings: Settings
  onApplied: (settings: Settings) => void
}) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [background, setBackground] = useState<Background>('brand')
  const [mark, setMark] = useState<HTMLImageElement | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const markUrl = settings.mark?.url

  useEffect(() => {
    if (!open || !markUrl) return
    let cancelled = false
    loadMark(markUrl).then(
      (image) => !cancelled && setMark(image),
      () => !cancelled && setError('Не удалось прочитать квадратный знак'),
    )
    return () => {
      cancelled = true
    }
  }, [open, markUrl])

  useEffect(() => {
    if (canvas.current && mark) draw(canvas.current, mark, fill(background, settings.primary_color))
  }, [mark, background, settings.primary_color])

  const apply = async () => {
    if (!canvas.current || !mark) return
    setPending(true)
    setError(null)
    try {
      const avatar = await toJpeg(canvas.current)
      const { data, error: failure } = await fetchClient.PUT('/api/admin/settings/brand/telegram', {
        params: { query: { background } },
        body: avatar as unknown as never,
        bodySerializer: (body: unknown) => body as Blob,
        headers: { 'Content-Type': 'image/jpeg' },
      })
      if (data) {
        onApplied(data)
        if (data.telegram?.ok) onOpenChange(false)
        else setError(`Telegram ответил ошибкой: ${data.telegram?.error ?? 'неизвестно'}`)
      } else setError(errorMessage(failure, 'Не удалось применить'))
    } catch {
      setError('Не удалось собрать аватар')
    } finally {
      setPending(false)
    }
  }

  const description = settings.bot_description.trim()
  const short = settings.bot_short_description.trim()
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="glass-float">
        <DialogHeader>
          <DialogTitle>Применить в Telegram</DialogTitle>
          <DialogDescription>
            Бот получит имя «{settings.name}», аватар из квадратного знака
            {description ? ', описание' : ''}
            {short ? ' и «Коротко о боте»' : ''}. Пустые поля в Telegram не меняются.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col items-center gap-4">
          <canvas
            ref={canvas}
            width={SIZE}
            height={SIZE}
            className="size-40 rounded-full shadow-sm"
            role="img"
            aria-label="Предпросмотр аватара"
          />
          <ToggleGroup
            type="single"
            value={background}
            onValueChange={(value) => value && setBackground(value as Background)}
            aria-label="Фон аватара"
            variant="outline"
            size="sm"
          >
            {BACKGROUNDS.map(({ value, label }) => (
              <ToggleGroupItem key={value} value={value}>
                {label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <p className="text-center text-xs text-muted-foreground">
            Выберите фон, на котором знак хорошо виден: Telegram показывает аватар кругом.
          </p>
        </div>
        {error && <p className="text-sm text-danger-text">{error}</p>}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Отмена</Button>
          </DialogClose>
          <Button onClick={() => void apply()} disabled={!mark || pending}>
            Применить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
