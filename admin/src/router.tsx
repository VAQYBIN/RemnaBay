import { createBrowserRouter, Navigate } from 'react-router'

import { AppShell } from '@/components/frame/AppShell'
import { AboutPage } from '@/pages/AboutPage'
import { ExpiringPage } from '@/pages/ExpiringPage'
import { HomePage } from '@/pages/HomePage'
import { LoginPage } from '@/pages/LoginPage'
import { BrandSettingsPage } from '@/pages/settings/BrandSettingsPage'
import { LoginSettingsPage } from '@/pages/settings/LoginSettingsPage'
import { SettingsLayout } from '@/pages/settings/SettingsLayout'
import { ShopSettingsPage } from '@/pages/settings/ShopSettingsPage'

/** Навигация админки (03-screens, «Навигация») — разделы этого этапа. */
export const router = createBrowserRouter(
  [
    { path: '/login', Component: LoginPage },
    {
      path: '/',
      Component: AppShell,
      children: [
        { index: true, Component: HomePage },
        { path: 'expiring', Component: ExpiringPage },
        {
          path: 'settings',
          Component: SettingsLayout,
          children: [
            { index: true, element: <Navigate to="brand" replace /> },
            { path: 'brand', Component: BrandSettingsPage },
            { path: 'shop', Component: ShopSettingsPage },
            { path: 'login', Component: LoginSettingsPage },
          ],
        },
        { path: 'about', Component: AboutPage },
        { path: '*', element: <Navigate to="/" replace /> },
      ],
    },
  ],
  { basename: '/admin' },
)
