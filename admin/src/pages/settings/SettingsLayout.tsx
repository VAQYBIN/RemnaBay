import { NavLink, Navigate, Outlet, useOutletContext } from 'react-router'

import { PageTitle } from '@/components/frame/Module'
import { isOwner, type Member } from '@/lib/session'
import { cn } from '@/lib/utils'

/** Разделы настроек этого этапа; остальные появятся со своими блоками. */
const SECTIONS = [
  { to: 'brand', label: 'Бренд' },
  { to: 'shop', label: 'Магазин' },
  { to: 'login', label: 'Вход в админку' },
]

/** А11. Настройки — только владелец (01-domain, таблица ролей). */
export function SettingsLayout() {
  const member = useOutletContext<Member>()
  if (!isOwner(member)) return <Navigate to="/" replace />
  return (
    <>
      <PageTitle>Настройки</PageTitle>
      <nav className="flex gap-1 overflow-x-auto" aria-label="Разделы настроек">
        {SECTIONS.map(({ to, label }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cn(
                'shrink-0 rounded-full px-4 py-1.5 text-sm transition-colors',
                isActive ? 'bg-brand-soft font-medium text-brand-text' : 'text-muted-foreground hover:bg-muted',
              )
            }
          >
            {label}
          </NavLink>
        ))}
      </nav>
      <Outlet context={member} />
    </>
  )
}
