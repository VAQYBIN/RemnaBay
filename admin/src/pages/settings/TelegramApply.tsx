import { FlipHorizontal2, FlipVertical2, RotateCcw, RotateCw } from 'lucide-react'
import { type KeyboardEvent, type PointerEvent, useEffect, useRef, useState } from 'react'

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
import { Toggle } from '@/components/ui/toggle'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { formatDateTime } from '@/lib/format'

type Settings = Schemas['BrandSettingsOut']
type Background = Schemas['AvatarBackground']

// Сторона аватара: Telegram принимает квадратный JPG
const SIZE = 640
// Масштаб знака — доля стороны аватара, %. По умолчанию знак целиком внутри круга
// с отступом: сторона квадрата, вписанного в круг, — 71 % диаметра. Больше 100 % —
// чтобы круглый знак закрыл круг аватара целиком (решение 0060)
const DEFAULT_SCALE = 58
const MIN_SCALE = 20
const MAX_SCALE = 150
// Знак растрируется с запасом под наибольший масштаб — без размытия
const MARK_PIXELS = (SIZE * MAX_SCALE) / 100
// Сдвиг знака — не дальше, чем на сторону аватара от центра: знак не теряется
const MAX_SHIFT = SIZE
// Шаг сдвига стрелками, пикселей аватара; с Shift — крупнее
const KEY_STEP = 4
const KEY_STEP_LARGE = 20

/** Положение знака на аватаре: масштаб, сдвиг от центра, поворот, отражение. */
type Placement = { scale: number; x: number; y: number; rotation: number; flipX: boolean; flipY: boolean }

const INITIAL: Placement = { scale: DEFAULT_SCALE, x: 0, y: 0, rotation: 0, flipX: false, flipY: false }

/** Положение, сохранённое при прошлом применении (0060), — окно начинает с него. */
function savedPlacement(settings: Settings): Placement {
  const saved = settings.telegram?.placement
  if (!saved) return INITIAL
  return {
    scale: saved.scale ?? DEFAULT_SCALE,
    x: saved.x ?? 0,
    y: saved.y ?? 0,
    rotation: saved.rotation ?? 0,
    flipX: saved.flip_x ?? false,
    flipY: saved.flip_y ?? false,
  }
}

function isInitial(placement: Placement): boolean {
  return (Object.keys(INITIAL) as (keyof Placement)[]).every((key) => placement[key] === INITIAL[key])
}

function clampShift(value: number): number {
  return Math.max(-MAX_SHIFT, Math.min(MAX_SHIFT, value))
}

/** Поворот в пределах −180…180°. */
function normalizeAngle(degrees: number): number {
  const turned = ((degrees % 360) + 360) % 360
  return turned > 180 ? turned - 360 : turned
}
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
    svg.setAttribute('width', String(MARK_PIXELS))
    svg.setAttribute('height', String(MARK_PIXELS))
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

function draw(canvas: HTMLCanvasElement, mark: HTMLImageElement, background: string, placement: Placement): void {
  const context = canvas.getContext('2d')
  if (!context) return
  context.fillStyle = background
  context.fillRect(0, 0, SIZE, SIZE)
  const side = (SIZE * placement.scale) / 100
  context.save()
  context.translate(SIZE / 2 + placement.x, SIZE / 2 + placement.y)
  context.rotate((placement.rotation * Math.PI) / 180)
  context.scale(placement.flipX ? -1 : 1, placement.flipY ? -1 : 1)
  context.drawImage(mark, -side / 2, -side / 2, side, side)
  context.restore()
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
 *  бота. Аватар — квадратный знак на выбранном фоне: оператор масштабирует его до 150 %,
 *  перетаскивает, поворачивает и отражает; предпросмотр — квадратная рамка с круглым
 *  ориентиром. JPG собирается из того же холста. */
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
  const [placement, setPlacement] = useState<Placement>(INITIAL)
  const drag = useRef<{ pointer: number; x: number; y: number } | null>(null)
  const change = (patch: Partial<Placement>) => setPlacement((current) => ({ ...current, ...patch }))
  const [mark, setMark] = useState<HTMLImageElement | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const markUrl = settings.mark?.url

  // Окно открывается с фоном и положением знака прошлого применения
  useEffect(() => {
    if (!open) return
    setPlacement(savedPlacement(settings))
    setBackground(settings.telegram?.background ?? 'brand')
    setError(null)
    // Только при открытии окна: иначе ответ сервера сбивал бы правку на ходу
  }, [open])

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
    if (canvas.current && mark) draw(canvas.current, mark, fill(background, settings.primary_color), placement)
  }, [mark, background, settings.primary_color, placement])

  // Перетаскивание знака мышью или пальцем: сдвиг на экране — в пиксели аватара
  const startDrag = (event: PointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = { pointer: event.pointerId, x: event.clientX, y: event.clientY }
  }
  const moveDrag = (event: PointerEvent<HTMLDivElement>) => {
    const start = drag.current
    if (!start || start.pointer !== event.pointerId) return
    const ratio = SIZE / event.currentTarget.getBoundingClientRect().width
    const dx = (event.clientX - start.x) * ratio
    const dy = (event.clientY - start.y) * ratio
    drag.current = { ...start, x: event.clientX, y: event.clientY }
    setPlacement((current) => ({ ...current, x: clampShift(current.x + dx), y: clampShift(current.y + dy) }))
  }
  const endDrag = () => {
    drag.current = null
  }
  // Стрелки — то же перемещение с клавиатуры
  const moveByKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? KEY_STEP_LARGE : KEY_STEP
    const shifts: Record<string, [number, number]> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    }
    const shift = shifts[event.key]
    if (!shift) return
    event.preventDefault()
    setPlacement((current) => ({
      ...current,
      x: clampShift(current.x + shift[0]),
      y: clampShift(current.y + shift[1]),
    }))
  }

  const apply = async () => {
    if (!canvas.current || !mark) return
    setPending(true)
    setError(null)
    try {
      const avatar = await toJpeg(canvas.current)
      const { data, error: failure } = await fetchClient.PUT('/api/admin/settings/brand/telegram', {
        params: {
          query: {
            background,
            scale: placement.scale,
            x: Math.round(placement.x),
            y: Math.round(placement.y),
            rotation: placement.rotation,
            flip_x: placement.flipX,
            flip_y: placement.flipY,
          },
        },
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
              увидят как аватар: углы за кругом затемнены (решение 0060). Знак
              перетаскивается мышью, пальцем или стрелками */}
          <div
            className="relative size-48 cursor-grab touch-none overflow-hidden rounded-md shadow-sm ring-1 ring-border outline-none select-none focus-visible:ring-3 focus-visible:ring-ring/50 active:cursor-grabbing"
            tabIndex={0}
            role="group"
            aria-label="Положение знака: перетащите или двигайте стрелками"
            onPointerDown={startDrag}
            onPointerMove={moveDrag}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onKeyDown={moveByKey}
          >
            <canvas
              ref={canvas}
              width={SIZE}
              height={SIZE}
              className="pointer-events-none size-full"
              role="img"
              aria-label="Предпросмотр аватара"
            />
            <svg viewBox="0 0 100 100" className="pointer-events-none absolute inset-0 size-full" aria-hidden>
              <path d="M0 0H100V100H0Z M50 0A50 50 0 1 0 50 100A50 50 0 1 0 50 0Z" fillRule="evenodd" fill="rgb(0 0 0 / 0.45)" />
              <circle cx="50" cy="50" r="49.5" fill="none" stroke="white" strokeOpacity="0.8" strokeWidth="0.6" strokeDasharray="2 1.5" />
            </svg>
          </div>
          <div className="flex items-center gap-1" role="toolbar" aria-label="Знак">
            <Toggle
              variant="outline"
              size="sm"
              pressed={placement.flipX}
              onPressedChange={(flipX) => change({ flipX })}
              aria-label="Отразить по горизонтали"
              title="Отразить по горизонтали"
            >
              <FlipHorizontal2 />
            </Toggle>
            <Toggle
              variant="outline"
              size="sm"
              pressed={placement.flipY}
              onPressedChange={(flipY) => change({ flipY })}
              aria-label="Отразить по вертикали"
              title="Отразить по вертикали"
            >
              <FlipVertical2 />
            </Toggle>
            <Button
              variant="outline"
              size="icon-sm"
              onClick={() => change({ rotation: normalizeAngle(placement.rotation + 90) })}
              aria-label="Повернуть на 90°"
              title="Повернуть на 90°"
            >
              <RotateCw />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setPlacement(INITIAL)}
              disabled={isInitial(placement)}
            >
              <RotateCcw />
              Сбросить
            </Button>
          </div>
          <div className="flex w-full max-w-64 flex-col gap-4">
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between text-sm">
                <span id="avatar-scale">Размер знака</span>
                <span className="tabular text-muted-foreground">{placement.scale}%</span>
              </div>
              <Slider
                aria-labelledby="avatar-scale"
                min={MIN_SCALE}
                max={MAX_SCALE}
                step={1}
                value={[placement.scale]}
                onValueChange={([scale]) => scale !== undefined && change({ scale })}
              />
            </div>
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between text-sm">
                <span id="avatar-rotation">Поворот</span>
                <span className="tabular text-muted-foreground">{placement.rotation}°</span>
              </div>
              <Slider
                aria-labelledby="avatar-rotation"
                min={-180}
                max={180}
                step={1}
                value={[placement.rotation]}
                onValueChange={([rotation]) => rotation !== undefined && change({ rotation })}
              />
            </div>
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
            Квадрат — картинка для Telegram, круг — то, что клиенты увидят как аватар бота. Перетащите знак,
            подберите размер, поворот и фон, на котором он хорошо виден.
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
