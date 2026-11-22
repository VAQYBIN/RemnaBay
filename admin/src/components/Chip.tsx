import { cn } from '@/lib/utils'

/** Фильтр-«пилюля» в ряду фильтров: состояние, период, вид (А4, А10). */
export function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        'shrink-0 rounded-full px-3 py-1 text-sm transition-colors',
        active ? 'bg-brand-soft font-medium text-brand-text' : 'text-muted-foreground hover:bg-muted',
      )}
    >
      {children}
    </button>
  )
}
