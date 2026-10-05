import { House, Info, LogOut, Settings, UserRound } from 'lucide-react'
import { NavLink, Navigate, Outlet } from 'react-router'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useBrand } from '@/lib/brand'
import { isOwner, type Member, useLogout, useMe } from '@/lib/session'
import { cn } from '@/lib/utils'

import { BrandLockup, BrandMark } from './BrandMark'
import { ThemeSwitch } from './ThemeSwitch'

type NavItem = { to: string; label: string; icon: typeof House; ownerOnly?: boolean }

/** Разделы, которые уже есть. Остальные появятся вместе со своими блоками;
 *  недоступное роли не показывается (03-screens, «Принципы админки»). */
const NAV: NavItem[] = [
  { to: '/', label: 'Главная', icon: House },
  { to: '/settings', label: 'Настройки', icon: Settings, ownerOnly: true },
  { to: '/about', label: 'О программе', icon: Info },
]

function visibleNav(member: Member): NavItem[] {
  return NAV.filter((item) => !item.ownerOnly || isOwner(member))
}

function MemberMenu({ member, compact = false }: { member: Member; compact?: boolean }) {
  const logout = useLogout()
  const role = isOwner(member) ? 'Владелец' : 'Помощник'
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          className={cn('justify-start gap-2', compact ? 'size-10 p-0 justify-center' : 'h-11 w-full px-3')}
          aria-label="Меню участника"
        >
          <UserRound />
          {!compact && (
            <span className="flex min-w-0 flex-col items-start leading-tight">
              <span className="truncate text-sm">{member.name ?? `ID ${member.telegram_id}`}</span>
              <span className="text-xs text-muted-foreground">{role}</span>
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="glass-float w-64">
        <DropdownMenuLabel className="text-xs text-muted-foreground">Тема</DropdownMenuLabel>
        <div className="px-1 pb-1">
          <ThemeSwitch />
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={logout}>
          <LogOut />
          Выйти
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function navClass({ isActive }: { isActive: boolean }) {
  return cn(
    'flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors duration-150',
    isActive ? 'bg-brand-soft font-medium text-brand-text' : 'text-muted-foreground hover:bg-muted hover:text-foreground',
  )
}

/** Рама админки: навигация — отдельная стеклянная колонка слева; на телефоне —
 *  нижняя панель (DESIGN.md, «Layout»). */
export function AppShell() {
  const me = useMe()
  const { data: brand } = useBrand()
  if (me.isPending) return null
  if (me.isError || !me.data) return <Navigate to="/login" replace />
  const member = me.data
  const items = visibleNav(member)

  return (
    <div className="flex min-h-dvh">
      <aside className="glass-base sticky top-0 hidden h-dvh w-64 shrink-0 flex-col gap-6 p-4 md:flex">
        <BrandLockup brand={brand} wide className="px-2 pt-1" />
        <nav className="flex flex-1 flex-col gap-1" aria-label="Разделы">
          {items.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === '/'} className={navClass}>
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
        <MemberMenu member={member} />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="glass-base sticky top-0 z-20 flex items-center justify-between gap-3 px-4 py-2 md:hidden">
          <span className="flex min-w-0 items-center gap-2">
            <BrandMark brand={brand} className="size-8" />
            <span className="truncate font-semibold">{brand?.name}</span>
          </span>
          <MemberMenu member={member} compact />
        </header>

        <main className="mx-auto flex w-full max-w-6xl min-w-0 flex-1 flex-col gap-4 px-4 pt-4 pb-24 md:px-8 md:py-8">
          <Outlet context={member} />
        </main>

        <nav
          className="glass-float fixed inset-x-3 bottom-3 z-20 flex justify-around rounded-2xl p-1 md:hidden"
          aria-label="Разделы"
        >
          {items.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                cn(
                  'flex min-w-0 flex-1 flex-col items-center gap-0.5 rounded-xl px-2 py-1.5 text-[0.7rem]',
                  isActive ? 'bg-brand-soft text-brand-text' : 'text-muted-foreground',
                )
              }
            >
              <Icon className="size-5" />
              <span className="truncate">{label}</span>
            </NavLink>
          ))}
        </nav>
      </div>
    </div>
  )
}
