import { useState, type ReactNode } from 'react'

import type { Schemas } from '@/api/client'
import type { MarkupNode } from '@/lib/telegramMarkup'
import { cn } from '@/lib/utils'

type Content = Schemas['BroadcastContent']

/** Скрытый текст: закрыт, пока по нему не нажали, — как в Telegram. */
function Spoiler({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <span
      role="button"
      tabIndex={0}
      onClick={() => setOpen(true)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') setOpen(true)
      }}
      className={cn(
        'rounded-sm transition-colors duration-150',
        open ? 'bg-muted' : 'cursor-pointer bg-foreground/70 text-transparent select-none',
      )}
      title={open ? undefined : 'Скрытый текст — нажмите, чтобы увидеть'}
    >
      {children}
    </span>
  )
}

function renderNodes(nodes: MarkupNode[]): ReactNode[] {
  return nodes.map((node, index) => {
    if (node.kind === 'text') return node.text
    const children = renderNodes(node.children)
    switch (node.type) {
      case 'bold':
        return <strong key={index} className="font-semibold">{children}</strong>
      case 'italic':
        return <em key={index}>{children}</em>
      case 'underline':
        return <u key={index}>{children}</u>
      case 'strikethrough':
        return <s key={index}>{children}</s>
      case 'spoiler':
        return <Spoiler key={index}>{children}</Spoiler>
      case 'text_link':
        return (
          <span key={index} className="text-brand-text underline underline-offset-2" title={node.url}>
            {children}
          </span>
        )
      case 'code':
        return <code key={index} className="rounded bg-muted px-1 font-mono text-[0.92em]">{children}</code>
      // Цитата и блок кода — во всю ширину, но в строке: перенос после них не даёт пустой строки, как в Telegram
      case 'pre':
        return <span key={index} className="inline-block w-full rounded-lg bg-muted px-2.5 py-2 align-top font-mono text-[0.92em]">{children}</span>
      case 'blockquote':
      case 'expandable_blockquote':
        return <span key={index} className="inline-block w-full border-l-2 border-brand pl-2.5 align-top">{children}</span>
    }
  })
}

function PreviewButton({ children, target }: { children: ReactNode; target?: boolean }) {
  return (
    <span
      className={cn(
        'flex min-h-9 items-center justify-center rounded-lg px-3 text-center text-sm',
        target ? 'bg-success-soft font-medium text-success-text' : 'bg-muted',
      )}
    >
      {children}
    </span>
  )
}

/** Сообщение рассылки так, как его увидит клиент (С13): изображение, текст, кнопки;
 *  «Главное меню» — всегда (11.1). Одно целевое действие — зелёное, два — без цвета. */
export function MessagePreview({
  nodes,
  content,
  imageUrl,
}: {
  nodes: MarkupNode[]
  content: Content
  imageUrl: string | null
}) {
  const target = content.buy !== content.renew
  const empty = nodes.length === 0
  return (
    <div className="flex flex-col gap-1.5">
      <div className="overflow-hidden rounded-2xl rounded-bl-md border bg-background shadow-sm">
        {imageUrl && <img src={imageUrl} alt="" className="max-h-80 w-full object-cover" />}
        {(!empty || !imageUrl) && (
          <p className={cn('px-3.5 py-2.5 text-sm leading-relaxed break-words whitespace-pre-wrap', empty && 'text-muted-foreground')}>
            {empty ? 'Текст сообщения' : renderNodes(nodes)}
          </p>
        )}
      </div>
      {content.buy && <PreviewButton target={target}>Купить подписку</PreviewButton>}
      {content.renew && <PreviewButton target={target}>Продлить</PreviewButton>}
      {content.links.map((link, index) => (
        <PreviewButton key={index}>
          {link.text || 'Подпись кнопки'}
          <span className="ml-1 text-muted-foreground" aria-hidden>
            ↗
          </span>
        </PreviewButton>
      ))}
      <PreviewButton>Главное меню</PreviewButton>
    </div>
  )
}
