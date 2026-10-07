import { useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Circle, CircleDashed } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'

import { $api, type Schemas } from '@/api/client'
import { CopyField } from '@/components/CopyField'
import { Module } from '@/components/frame/Module'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

type Item = Schemas['ItemOut']
type Checklist = Schemas['ChecklistOut']

const TITLES: Record<Item['key'], string> = {
  panel: 'Подключение к панели',
  webhook: 'Вебхук панели',
  device_limit: 'Лимит устройств в панели',
  brand: 'Бренд',
  tariffs: 'Тарифы',
  payment: 'Способ оплаты',
  support: 'Контакт поддержки',
  trial: 'Триал',
  migration: 'Миграция',
}

function requiredVersion(supported: string[]): string {
  return supported
    .map((version) => {
      const [major, minor] = version.split('.')
      return `${major}.${minor}.x не ниже ${version}`
    })
    .join(' или ')
}

function panelText(item: Item): string {
  const details = item.panel
  if (!details) return ''
  switch (details.error) {
    case null:
      return `Панель отвечает, версия ${details.version ?? '—'}.`
    case 'unavailable':
      return 'Панель не отвечает. Проверьте PANEL_URL в .env и что панель запущена.'
    case 'unauthorized':
      return 'Панель отклонила токен. Проверьте PANEL_TOKEN в .env.'
    case 'incompatible_version':
      return `Версия панели ${details.version ?? '—'} не поддерживается. Нужна ${requiredVersion(details.supported_versions)}.`
    default:
      return 'Панель ответила ошибкой. Подробности — в логе магазина.'
  }
}

function Body({ item, isOwner }: { item: Item; isOwner: boolean }) {
  switch (item.key) {
    case 'panel':
      return <p>{panelText(item)}</p>
    case 'webhook':
      if (item.status === 'done') return <p>События от панели приходят.</p>
      return (
        <div className="flex flex-col gap-3">
          <p>Укажите в панели адрес и секрет вебхука. Пункт выполнится, когда придёт первое событие.</p>
          {item.webhook && <CopyField label="Адрес" value={item.webhook.url} />}
          {item.webhook?.secret && <CopyField label="Секрет" value={item.webhook.secret} />}
          {isOwner ? (
            <p>
              В панели уже задан свой секрет?{' '}
              <Link to="/settings/panel" className="text-brand-text underline-offset-4 hover:underline">
                Укажите его в настройках
              </Link>
            </p>
          ) : (
            <p className="text-xs">Секрет видит владелец.</p>
          )}
        </div>
      )
    case 'device_limit':
      if (item.status === 'done') return <p>Лимит устройств включён.</p>
      if (item.status === 'warning')
        return <p>В панели выключен лимит устройств: защита триала по устройству не будет работать. Открыть магазин можно и так.</p>
      return <p>Не удалось проверить: панель не отвечает.</p>
    case 'brand':
      if (item.status === 'done') return <p>Название и знак заданы.</p>
      return (
        <p>
          Задайте {[!item.brand?.name && 'название', !item.brand?.mark && 'квадратный знак'].filter(Boolean).join(' и ')}.{' '}
          {isOwner && (
            <Link to="/settings/brand" className="text-brand-text underline-offset-4 hover:underline">
              Настройки бренда
            </Link>
          )}
        </p>
      )
    case 'tariffs':
      if (item.status === 'done') return <p>Есть тариф в продаже.</p>
      return (
        <p>
          Нужен хотя бы один тариф в продаже.{' '}
          {isOwner && (
            <Link to="/tariffs" className="text-brand-text underline-offset-4 hover:underline">
              Тарифы
            </Link>
          )}
        </p>
      )
    case 'payment':
      return <p>{item.status === 'done' ? 'Способ оплаты подключён.' : 'Нужен хотя бы один способ оплаты.'}</p>
    case 'support':
      if (item.status === 'done') return <p>Контакт поддержки указан.</p>
      return (
        <p>
          Пока контакт не указан, кнопки «Поддержка» в боте нет.{' '}
          {isOwner && (
            <Link to="/settings/shop" className="text-brand-text underline-offset-4 hover:underline">
              Настройки магазина
            </Link>
          )}
        </p>
      )
    case 'trial':
      return <p>{item.trial_enabled ? 'Триал включён.' : 'Триал выключен — это нормально.'}</p>
    case 'migration':
      return <p>Необязательно: перенос клиентов из прежнего бота.</p>
  }
}

const STATUS_ICON = {
  done: { icon: CheckCircle2, className: 'text-success-text', label: 'Выполнено' },
  todo: { icon: Circle, className: 'text-muted-foreground', label: 'Не выполнено' },
  warning: { icon: AlertTriangle, className: 'text-warning-text', label: 'Предупреждение' },
  optional: { icon: CircleDashed, className: 'text-muted-foreground', label: 'Необязательно' },
} as const

function ItemRow({ item, isOwner }: { item: Item; isOwner: boolean }) {
  const status = STATUS_ICON[item.status]
  const Icon = status.icon
  return (
    <li className="flex gap-3 py-3">
      <Icon className={cn('mt-0.5 size-5 shrink-0', status.className)} aria-label={status.label} />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <p className="font-medium">
          {TITLES[item.key]}
          {item.required && item.status !== 'done' && (
            <span className="ml-2 text-xs font-normal text-muted-foreground">обязательно</span>
          )}
        </p>
        <div className="text-sm text-muted-foreground">
          <Body item={item} isOwner={isOwner} />
        </div>
      </div>
    </li>
  )
}

function OpenShopButton({ checklist }: { checklist: Checklist }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const openShop = $api.useMutation('post', '/api/admin/shop/open', {
    onSuccess: () => {
      setOpen(false)
      void queryClient.invalidateQueries()
    },
  })
  const label = checklist.state === 'paused' ? 'Открыть магазин снова' : 'Открыть магазин'
  return (
    <>
      <Button onClick={() => setOpen(true)} disabled={!checklist.can_open}>
        {label}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="glass-float">
          <DialogHeader>
            <DialogTitle>{label}?</DialogTitle>
            <DialogDescription>
              Клиенты начнут попадать в магазин: бот перестанет отвечать «Магазин скоро откроется», станут
              доступны покупки, продления и триал. Если вы переезжаете на этот бот, клиенты прежнего
              магазина с этого момента попадают сюда.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="ghost">Отмена</Button>
            </DialogClose>
            <Button onClick={() => openShop.mutate({})} disabled={openShop.isPending}>
              Открыть
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

/** Чек-лист первичной настройки (1.7–1.15): что ещё не настроено. */
export function ChecklistModule({ checklist, isOwner }: { checklist: Checklist; isOwner: boolean }) {
  const title = checklist.state === 'not_opened' ? 'Что ещё не настроено' : 'Невыполненные пункты'
  const canOpenHint = !checklist.can_open && checklist.state !== 'open'
  return (
    <Module
      title={title}
      action={isOwner && checklist.state !== 'open' ? <OpenShopButton checklist={checklist} /> : undefined}
      footer={
        canOpenHint ? (
          <p className="text-xs text-muted-foreground">
            Открыть магазин можно, когда панель подключена, есть тариф в продаже и способ оплаты.
          </p>
        ) : undefined
      }
    >
      <ul className="divide-y">
        {checklist.items.map((item) => (
          <ItemRow key={item.key} item={item} isOwner={isOwner} />
        ))}
      </ul>
    </Module>
  )
}
