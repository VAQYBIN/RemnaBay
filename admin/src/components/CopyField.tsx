import { Check, Copy } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'

/** Значение для копирования в панель: адрес, секрет. */
export function CopyField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <div className="flex min-w-0 items-center gap-2 rounded-lg bg-muted px-3 py-2">
        <code className="min-w-0 flex-1 truncate font-mono text-xs">{value}</code>
        <Button variant="ghost" size="icon-sm" onClick={copy} aria-label={`Скопировать: ${label}`}>
          {copied ? <Check /> : <Copy />}
        </Button>
      </div>
    </div>
  )
}
