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
import { Slider } from '@/components/ui/slider'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { formatDateTime } from '@/lib/format'

type Settings = Schemas['BrandSettingsOut']
type Background = Schemas['AvatarBackground']

// Сторона аватара: Telegram принимает квадратный JPG
const SIZE = 640
// Масштаб знака — доля стороны аватара, %. По умолчанию знак целиком внутри круга
// с отступом: сторона квадрата, вписанного в круг, — 71 % диаметра
const DEFAULT_SCALE = 58
const MIN_SCALE = 20
const MAX_SCALE = 100
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
 *  задаём его. Без viewBox новые width и height лишь увеличили бы область, а не
 *  рисунок, поэтому viewBox сначала берётся из прежних размеров. */
async function loadMark(url: string): Promise<HTMLImageElement> {
  const response = await fetch(url, { credentials: 'same-origin' })
  let blob = await response.blob()
  if (blob.type.startsWith('image/svg')) {
    const document = new DOMParser().parseFromString(await blob.text(), 'image/svg+xml')
    const svg = document.documentElement
    if (!svg.hasAttribute('viewBox')) {
      const width = Number.parseFloat(svg.getAttribute('width') ?? '')
      const height = Number.parseFloat(svg.getAttribute('height') ?? '')
      if (width > 0 && height > 0) svg.setAttribute('viewBox', `0 0 ${width} ${height}`)
    }
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

function draw(canvas: HTMLCanvasElement, mark: HTMLImageElement, background: string, scale: number): void {
  const context = canvas.getContext('2d')
  if (!context) return
  context.fillStyle = background
  context.fillRect(0, 0, SIZE, SIZE)
  const side = (SIZE * scale) / 100
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

/** «Применить в Telegram» (1.27, решения 0058–0060): имя, описания, меню команд и аватар
 *  бота. Аватар — квадратный знак в выбранном масштабе на выбранном фоне; предпросмотр —
 *  квадратная рамка с круглым ориентиром. JPG собирается из того же холста. */
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
  const [scale, setScale] = useState(DEFAULT_SCALE)
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
    if (canvas.current && mark) draw(canvas.current, mark, fill(background, settings.primary_color), scale)
  }, [mark, background, settings.primary_color, scale])

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
            Бот получит имя «{settings.name}», меню команд, аватар из квадратного знака
            {description ? ', описание' : ''}
            {short ? ' и «Коротко о боте»' : ''}. Пустые поля в Telegram не меняются.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col items-center gap-4">
          {/* Квадрат — картинка, которая уйдёт в Telegram; круг — то, что клиенты
              увидят как аватар: углы за кругом затемнены (решение 0060) */}
          <div className="relative size-48 overflow-hidden rounded-md shadow-sm ring-1 ring-border">
            <canvas
              ref={canvas}
              width={SIZE}
              height={SIZE}
              className="size-full"
              role="img"
              aria-label="Предпросмотр аватара"
            />
            <svg viewBox="0 0 100 100" className="pointer-events-none absolute inset-0 size-full" aria-hidden>
              <path d="M0 0H100V100H0Z M50 0A50 50 0 1 0 50 100A50 50 0 1 0 50 0Z" fillRule="evenodd" fill="rgb(0 0 0 / 0.45)" />
              <circle cx="50" cy="50" r="49.5" fill="none" stroke="white" strokeOpacity="0.8" strokeWidth="0.6" strokeDasharray="2 1.5" />
            </svg>
          </div>
          <div className="flex w-full max-w-64 flex-col gap-2">
            <div className="flex items-center justify-between text-sm">
              <span id="avatar-scale">Размер знака</span>
              <span className="tabular text-muted-foreground">{scale}%</span>
            </div>
            <Slider
              aria-labelledby="avatar-scale"
              min={MIN_SCALE}
              max={MAX_SCALE}
              step={1}
              value={[scale]}
              onValueChange={([value]) => value !== undefined && setScale(value)}
            />
          </div>
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
            Квадрат — картинка для Telegram, круг — то, что клиенты увидят как аватар бота. Подберите размер
            знака и фон, на котором он хорошо виден.
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
