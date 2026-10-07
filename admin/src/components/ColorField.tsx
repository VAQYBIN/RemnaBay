import { Pipette } from 'lucide-react'
import { HexColorPicker } from 'react-colorful'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'

const HEX = /^#[0-9a-f]{6}$/i

/** Пипетка браузера (EyeDropper API): есть в Chrome, Edge, Opera, Brave; нет в Firefox и
 *  Safari — там кнопки нет. */
type EyeDropperResult = { sRGBHex: string }
type EyeDropperConstructor = new () => { open: () => Promise<EyeDropperResult> }

function eyeDropper(): EyeDropperConstructor | null {
  const candidate = (window as { EyeDropper?: EyeDropperConstructor }).EyeDropper
  return candidate ?? null
}

/** Цвет: образец открывает палитру на парящем стекле, рядом — поле #RRGGBB и пипетка.
 *  Системный выбор цвета браузера в раму не вписывается (DESIGN.md). */
export function ColorField({
  id,
  value,
  onChange,
  emptyLabel = 'не задан',
}: {
  id: string
  value: string
  onChange: (value: string) => void
  emptyLabel?: string
}) {
  const valid = HEX.test(value)
  const Dropper = eyeDropper()
  const pick = async () => {
    if (!Dropper) return
    try {
      const { sRGBHex } = await new Dropper().open()
      onChange(sRGBHex.toLowerCase())
    } catch {
      // Пользователь нажал Esc — цвет не меняется
    }
  }
  return (
    <div className="flex items-center gap-2">
      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label="Открыть палитру"
            className={cn(
              // Без увеличения при наведении: палитра привязана к образцу и не должна дёргаться
              'size-9 shrink-0 rounded-lg border shadow-[inset_0_0_0_2px_var(--rb-glass-module-solid)] outline-offset-2 transition-[box-shadow] duration-150 hover:ring-2 hover:ring-ring/40',
              !valid && 'bg-[repeating-linear-gradient(45deg,var(--rb-sunken)_0_6px,transparent_6px_12px)]',
            )}
            style={valid ? { backgroundColor: value } : undefined}
          />
        </PopoverTrigger>
        <PopoverContent className="glass-float w-auto rounded-2xl p-3" align="start" sideOffset={8}>
          <div className="rb-color-picker flex flex-col gap-3">
            <HexColorPicker color={valid ? value : '#1fa4a0'} onChange={onChange} />
            {Dropper && (
              <Button variant="outline" size="sm" onClick={() => void pick()}>
                <Pipette />
                Взять цвет с экрана
              </Button>
            )}
          </div>
        </PopoverContent>
      </Popover>
      <Input
        id={id}
        value={value}
        placeholder={emptyLabel}
        onChange={(event) => onChange(event.target.value)}
        className="tabular font-mono"
        maxLength={7}
        aria-invalid={value !== '' && !valid}
      />
      {Dropper && (
        <Button
          variant="ghost"
          size="icon"
          onClick={() => void pick()}
          aria-label="Пипетка: взять цвет с экрана"
          title="Пипетка: взять цвет с экрана"
        >
          <Pipette />
        </Button>
      )}
    </div>
  )
}
