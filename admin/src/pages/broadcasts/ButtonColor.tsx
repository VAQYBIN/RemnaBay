import type { Schemas } from '@/api/client'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

export type ButtonStyle = Schemas['ButtonStyle']

// Значение «без цвета»: у переключателя нет значения null
const NONE = 'none'

/** Цвета кнопок Telegram (решение 0069). Синий — цвет Telegram, а не бренда оператора. */
export const BUTTON_COLORS: { value: ButtonStyle | null; label: string; swatch: string; preview: string }[] = [
  { value: null, label: 'Без цвета', swatch: 'border border-border bg-muted', preview: 'bg-muted' },
  { value: 'primary', label: 'Синий', swatch: 'bg-sky-500', preview: 'bg-sky-500/15 font-medium text-sky-700 dark:text-sky-300' },
  { value: 'success', label: 'Зелёный', swatch: 'bg-success', preview: 'bg-success-soft font-medium text-success-text' },
  { value: 'danger', label: 'Красный', swatch: 'bg-danger', preview: 'bg-danger-soft font-medium text-danger-text' },
]

export function previewClass(style: ButtonStyle | null | undefined): string {
  return BUTTON_COLORS.find((color) => color.value === (style ?? null))?.preview ?? 'bg-muted'
}

/** Цвет кнопки рассылки: выбирает владелец (решение 0069). */
export function ButtonColor({
  value,
  onChange,
  label,
}: {
  value: ButtonStyle | null | undefined
  onChange: (value: ButtonStyle | null) => void
  label: string
}) {
  return (
    <ToggleGroup
      type="single"
      variant="outline"
      size="sm"
      spacing={0}
      value={value ?? NONE}
      onValueChange={(next) => next && onChange(next === NONE ? null : (next as ButtonStyle))}
      aria-label={label}
      className="shrink-0"
    >
      {BUTTON_COLORS.map((color) => (
        <Tooltip key={color.label}>
          <TooltipTrigger asChild>
            <ToggleGroupItem
              value={color.value ?? NONE}
              aria-label={color.label}
              className="group size-8 p-0 data-[state=on]:bg-brand-soft"
            >
              <span
                className={cn(
                  'size-3.5 rounded-full ring-offset-2 ring-offset-background group-data-[state=on]:ring-2 group-data-[state=on]:ring-foreground/60',
                  color.swatch,
                )}
                aria-hidden
              />
            </ToggleGroupItem>
          </TooltipTrigger>
          <TooltipContent>{color.label}</TooltipContent>
        </Tooltip>
      ))}
    </ToggleGroup>
  )
}
