import { Monitor, Moon, Sun } from 'lucide-react'

import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { type ThemePreference, useThemePreference } from '@/lib/theme'

const OPTIONS: { value: ThemePreference; label: string; icon: typeof Sun }[] = [
  { value: 'system', label: 'Системная', icon: Monitor },
  { value: 'light', label: 'Светлая', icon: Sun },
  { value: 'dark', label: 'Тёмная', icon: Moon },
]

/** «Системная / Светлая / Тёмная» (1.25). */
export function ThemeSwitch() {
  const [preference, setPreference] = useThemePreference()
  return (
    <ToggleGroup
      type="single"
      value={preference}
      onValueChange={(value) => value && setPreference(value as ThemePreference)}
      aria-label="Тема"
      className="w-full"
    >
      {OPTIONS.map(({ value, label, icon: Icon }) => (
        <ToggleGroupItem key={value} value={value} aria-label={label} className="flex-1 gap-1.5">
          <Icon />
          <span className="text-xs">{label}</span>
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}
