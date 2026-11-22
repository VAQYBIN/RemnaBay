import { cn } from '@/lib/utils'

/**
 * ВРЕМЕННЫЙ знак RemnaBay (решение 0051): настоящего знака пока нет, придумывать его
 * нельзя (docs/design/PRODUCT.md). Когда знак появится, заменяется только этот файл.
 * Цвет — основной цвет RemnaBay #1FA4A0 (0038), не цвет оператора.
 */
export function RemnaBayMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 64 64"
      role="img"
      aria-label="RemnaBay"
      className={cn('size-9 shrink-0', className)}
    >
      <rect width="64" height="64" rx="16" fill="#1FA4A0" />
      <text
        x="32"
        y="44"
        textAnchor="middle"
        fontFamily="Golos Text Variable, system-ui, sans-serif"
        fontSize="34"
        fontWeight="600"
        fill="#06201f"
      >
        R
      </text>
    </svg>
  )
}
