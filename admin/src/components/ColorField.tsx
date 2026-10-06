import { HexColorPicker } from 'react-colorful'

import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'

const HEX = /^#[0-9a-f]{6}$/i

/** Цвет: образец открывает палитру на парящем стекле, рядом — поле #RRGGBB.
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
  return (
    <div className="flex items-center gap-2">
      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label="Открыть палитру"
            className={cn(
              'size-9 shrink-0 rounded-lg border shadow-[inset_0_0_0_2px_var(--rb-glass-module-solid)] transition-transform duration-150 hover:scale-105',
              !valid && 'bg-[repeating-linear-gradient(45deg,var(--rb-sunken)_0_6px,transparent_6px_12px)]',
            )}
            style={valid ? { backgroundColor: value } : undefined}
          />
        </PopoverTrigger>
        <PopoverContent className="glass-float w-auto rounded-2xl p-3" align="start">
          <div className="rb-color-picker">
            <HexColorPicker color={valid ? value : '#1fa4a0'} onChange={onChange} />
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
    </div>
  )
}
