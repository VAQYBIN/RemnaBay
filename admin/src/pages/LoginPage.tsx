import { useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Send } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router'

import { $api, type Schemas } from '@/api/client'
import { BrandMark } from '@/components/frame/BrandMark'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { useBrand } from '@/lib/brand'
import { useMe } from '@/lib/session'

const POLL_INTERVAL_MS = 2000

type Problem = 'rejected' | 'expired' | 'failed' | 'start_failed'

const PROBLEMS: Record<Problem, string> = {
  rejected: 'Этот аккаунт Telegram не входит в команду магазина.',
  expired: 'Подтверждение устарело. Начните вход заново.',
  failed: 'Telegram не подтвердил вход. Попробуйте ещё раз или войдите через бот.',
  start_failed: 'Не удалось начать вход. Попробуйте ещё раз.',
}

// Вход через Telegram OpenID Connect: сервер уводит на страницу Telegram (1.4, 0053)
const OIDC_START = '/api/admin/auth/telegram/start'

function problemFromQuery(value: string | null): Problem | null {
  return value === 'rejected' || value === 'expired' || value === 'failed' ? value : null
}

/** Ожидание подтверждения в боте: код, ссылка на бот, опрос (1.4, 1.5). */
function Waiting({
  request,
  onDone,
}: {
  request: Schemas['LoginRequestOut']
  onDone: (problem: Problem | null) => void
}) {
  const poll = $api.useMutation('post', '/api/admin/auth/login-requests/poll')
  const { mutate } = poll

  useEffect(() => {
    const timer = window.setInterval(() => {
      mutate(
        {},
        {
          onSuccess: (result) => {
            if (result.status === 'signed_in') onDone(null)
            else if (result.status === 'rejected' || result.status === 'expired') onDone(result.status)
          },
        },
      )
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [mutate, onDone])

  return (
    <div className="flex flex-col items-center gap-5 text-center">
      <p className="text-sm text-muted-foreground">Код подтверждения</p>
      <p className="tabular text-5xl font-semibold tracking-[0.3em]" aria-live="polite">
        {request.code}
      </p>
      <p className="max-w-xs text-sm text-muted-foreground">
        Откройте бот и подтвердите вход, только если в боте тот же код.
      </p>
      <Button asChild size="lg" className="h-11 w-full">
        <a href={request.bot_link} target="_blank" rel="noreferrer">
          <ExternalLink />
          Открыть Telegram
        </a>
      </Button>
      <p className="text-xs text-muted-foreground">Ждём подтверждения в боте…</p>
    </div>
  )
}

/** А0. Вход: оформлен брендом оператора (1.23); пока бренд не задан — RemnaBay. */
export function LoginPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [params] = useSearchParams()
  const { data: brand } = useBrand()
  const me = useMe()
  const start = $api.useMutation('post', '/api/admin/auth/login-requests')
  const [problem, setProblem] = useState<Problem | null>(problemFromQuery(params.get('error')))
  const [request, setRequest] = useState<Schemas['LoginRequestOut'] | null>(null)
  const [viaBot, setViaBot] = useState(false)

  const begin = () => {
    setProblem(null)
    start.mutate(
      {},
      {
        onSuccess: (result) => {
          setRequest(result)
          window.open(result.bot_link, '_blank', 'noreferrer')
        },
        onError: () => setProblem('start_failed'),
      },
    )
  }

  // Стабильная ссылка: опрос в Waiting не перезапускает таймер на каждой отрисовке
  const finish = useCallback(
    (result: Problem | null) => {
      setRequest(null)
      if (result) {
        setProblem(result)
        return
      }
      void queryClient.invalidateQueries().then(() => navigate('/', { replace: true }))
    },
    [navigate, queryClient],
  )

  // Только действующая сессия: после ошибки обновления в кэше может остаться прежний участник
  if (me.isSuccess && !me.isRefetchError) return <Navigate to="/" replace />

  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="glass-module flex w-full max-w-sm flex-col gap-6 p-6">
        <div className="flex flex-col items-center gap-3 text-center">
          {brand?.logo_url ? (
            <img src={brand.logo_url} alt={brand.name} className="h-12 max-w-full object-contain" />
          ) : (
            <>
              <BrandMark brand={brand} className="size-14" />
              <p className="text-xl font-semibold">{brand?.name}</p>
            </>
          )}
          <h1 className="text-sm text-muted-foreground">Вход в админку</h1>
        </div>

        {problem && (
          <Alert className="border-danger-text/40 bg-danger-soft">
            <AlertDescription className="text-foreground">{PROBLEMS[problem]}</AlertDescription>
          </Alert>
        )}

        {request ? (
          <Waiting request={request} onDone={finish} />
        ) : viaBot ? (
          <div className="flex flex-col gap-3">
            <Button size="lg" className="h-11 w-full" onClick={begin} disabled={start.isPending}>
              <Send />
              Получить подтверждение в боте
            </Button>
            <Button variant="ghost" onClick={() => setViaBot(false)}>
              Назад
            </Button>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-3">
            <Button asChild size="lg" className="h-11 w-full">
              <a href={OIDC_START}>
                <Send />
                Войти через Telegram
              </a>
            </Button>
            <button
              type="button"
              onClick={() => {
                setProblem(null)
                setViaBot(true)
              }}
              className="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
            >
              Войти через бот
            </button>
          </div>
        )}

        <p className="text-center text-xs text-muted-foreground">
          {viaBot
            ? 'Бот пришлёт подтверждение с кодом — подтвердите, если код тот же.'
            : 'Вход подтверждается в приложении Telegram. Ссылку на эту страницу бот присылает и по команде /admin.'}
        </p>
      </div>
    </main>
  )
}
