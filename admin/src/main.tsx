import './index.css'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from 'react-router/dom'

import { fetchClient } from '@/api/client'
import { TooltipProvider } from '@/components/ui/tooltip'
import { BrandStyle } from '@/lib/brand'
import { applyTheme } from '@/lib/theme'

import { router } from './router'

// Тема — до первой отрисовки, чтобы экран не мигал (1.25)
applyTheme()

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true } },
})

// Сессия закончилась или доступ отозван — на страницу входа. Кэш сбрасывается:
// иначе страница входа увидела бы прежнего участника и вернула на главную
fetchClient.use({
  onResponse({ request, response }) {
    const auth = new URL(request.url, window.location.origin).pathname.startsWith('/api/admin/auth/')
    if (response.status === 401 && !auth) {
      queryClient.clear()
      void router.navigate('/login', { replace: true })
    }
    return response
  },
})

const root = document.getElementById('root')
if (root === null) {
  throw new Error('Не найден элемент #root')
}

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <BrandStyle />
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
)
