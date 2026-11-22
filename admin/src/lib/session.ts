import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router'

import { $api, type Schemas } from '@/api/client'

export type Member = Schemas['MemberOut']

/** Участник текущей сессии; 401 — сессии нет (вход, 1.4). */
export function useMe() {
  return $api.useQuery('get', '/api/admin/auth/me', {}, { retry: false, staleTime: 60_000 })
}

export function isOwner(member: Member | undefined): boolean {
  return member?.role === 'owner'
}

export function useLogout() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const logout = $api.useMutation('post', '/api/admin/auth/logout')
  return () =>
    logout.mutate(
      {},
      {
        onSettled: () => {
          queryClient.clear()
          void navigate('/login', { replace: true })
        },
      },
    )
}
