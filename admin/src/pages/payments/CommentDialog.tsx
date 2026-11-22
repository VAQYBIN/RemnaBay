import { useState } from 'react'

import { Field } from '@/components/Field'
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
import { Textarea } from '@/components/ui/textarea'

/** Действие команды с обязательным комментарием: он попадает в журнал (4.22, 4.31). */
export function CommentDialog({
  open,
  onOpenChange,
  title,
  description,
  confirm,
  pending,
  error,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirm: string
  pending: boolean
  error?: string | null
  onConfirm: (comment: string) => void
}) {
  const [comment, setComment] = useState('')
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setComment('')
        onOpenChange(next)
      }}
    >
      <DialogContent className="glass-float">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <Field id="team-comment" label="Комментарий" hint="Виден только команде — в журнале и карточке.">
          <Textarea id="team-comment" value={comment} onChange={(event) => setComment(event.target.value)} />
        </Field>
        {error && <p className="text-sm text-danger-text">{error}</p>}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Отмена</Button>
          </DialogClose>
          <Button onClick={() => onConfirm(comment.trim())} disabled={!comment.trim() || pending}>
            {confirm}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
