import { Monitor, Moon, Sun } from 'lucide-react'

import { DropdownMenuRadioGroup, DropdownMenuRadioItem } from '@/components/ui/dropdown-menu'
import { type ThemePreference, useThemePreference } from '@/lib/theme'

const OPTIONS: { value: ThemePreference; label: string; icon: typeof Sun }[] = [
  { value: 'system', label: 'Системная', icon: Monitor },
  { value: 'light', label: 'Светлая', icon: Sun },
  { value: 'dark', label: 'Тёмная', icon: Moon },
]

/** «Системная / Светлая / Тёмная» (1.25) — пункты меню участника с отметкой выбранного. */
export function ThemeRadioItems() {
  const [preference, setPreference] = useThemePreference()
  return (
    <DropdownMenuRadioGroup
      value={preference}
      onValueChange={(value) => setPreference(value as ThemePreference)}
    >
      {OPTIONS.map(({ value, label, icon: Icon }) => (
        <DropdownMenuRadioItem key={value} value={value} onSelect={(event) => event.preventDefault()}>
          <Icon />
          {label}
        </DropdownMenuRadioItem>
      ))}
    </DropdownMenuRadioGroup>
  )
}
