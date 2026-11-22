import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

/** Модуль: шапка (заголовок слева, действие справа) → содержимое → подвал.
 *  Одинаковое устройство у всех модулей (DESIGN.md, «Правила модульности»). */
export function Module({
  title,
  action,
  footer,
  className,
  children,
}: {
  title: ReactNode
  action?: ReactNode
  footer?: ReactNode
  className?: string
  children: ReactNode
}) {
  return (
    <section className={cn('glass-module flex min-w-0 flex-col gap-4 p-5', className)}>
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-base font-medium">{title}</h2>
        {action}
      </header>
      <div className="min-w-0">{children}</div>
      {footer && <footer className="border-t pt-4">{footer}</footer>}
    </section>
  )
}

/** Заголовок экрана. */
export function PageTitle({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h1 className="text-2xl font-semibold">{children}</h1>
      {action}
    </div>
  )
}
