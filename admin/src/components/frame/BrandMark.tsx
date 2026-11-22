import type { Brand } from '@/lib/brand'
import { cn } from '@/lib/utils'

import { RemnaBayMark } from './RemnaBayMark'

/** Квадратный знак оператора в плитке со скруглением рамы; пока не задан — знак
 *  RemnaBay (1.23). */
export function BrandMark({ brand, className }: { brand?: Brand; className?: string }) {
  if (!brand?.mark_url) return <RemnaBayMark className={className} />
  return (
    <img
      src={brand.mark_url}
      alt=""
      className={cn('size-9 shrink-0 rounded-[28%] object-contain', className)}
    />
  )
}

/** Знак и название; на широком месте — горизонтальный логотип, если он есть. */
export function BrandLockup({
  brand,
  wide = false,
  className,
}: {
  brand?: Brand
  wide?: boolean
  className?: string
}) {
  if (wide && brand?.logo_url) {
    return (
      <img src={brand.logo_url} alt={brand.name} className={cn('h-9 max-w-48 object-contain', className)} />
    )
  }
  return (
    <span className={cn('flex min-w-0 items-center gap-3', className)}>
      <BrandMark brand={brand} />
      <span className="truncate text-base font-semibold">{brand?.name ?? ''}</span>
    </span>
  )
}
