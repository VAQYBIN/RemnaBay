import { Check, ChevronsUpDown } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'

/** Выбор из длинного списка с поиском — на парящем стекле рамы, а не системный
 *  список браузера (DESIGN.md, «Три уровня стекла»). */
export function SearchSelect({
  id,
  value,
  options,
  onChange,
  searchPlaceholder,
  emptyText = 'Ничего не найдено',
}: {
  id: string
  value: string
  options: readonly string[]
  onChange: (value: string) => void
  searchPlaceholder: string
  emptyText?: string
}) {
  const [open, setOpen] = useState(false)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id={id}
          variant="outline"
          role="combobox"
          aria-expanded={open}
          className="h-9 w-full justify-between font-normal"
        >
          <span className="truncate">{value}</span>
          <ChevronsUpDown className="opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="glass-float w-(--radix-popover-trigger-width) min-w-64 p-0" align="start">
        <Command className="bg-transparent">
          <CommandInput placeholder={searchPlaceholder} />
          <CommandList className="rb-scroll max-h-72 px-1 pb-1">
            <CommandEmpty>{emptyText}</CommandEmpty>
            {options.map((option) => (
              <CommandItem
                key={option}
                value={option}
                onSelect={() => {
                  onChange(option)
                  setOpen(false)
                }}
                className={cn(option === value && 'font-medium text-brand-text')}
              >
                <Check className={cn('size-4 text-brand-text', option === value ? 'opacity-100' : 'opacity-0')} />
                {option}
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}
