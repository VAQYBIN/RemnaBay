import { createBrowserRouter, Navigate } from 'react-router'

import { AppShell } from '@/components/frame/AppShell'
import { AboutPage } from '@/pages/AboutPage'
import { ExpiringPage } from '@/pages/ExpiringPage'
import { HomePage } from '@/pages/HomePage'
import { LoginPage } from '@/pages/LoginPage'
import { PaymentCardPage } from '@/pages/payments/PaymentCardPage'
import { PaymentsPage } from '@/pages/payments/PaymentsPage'
import { BrandSettingsPage } from '@/pages/settings/BrandSettingsPage'
import { LoginSettingsPage } from '@/pages/settings/LoginSettingsPage'
import { PanelSettingsPage } from '@/pages/settings/PanelSettingsPage'
import { PaymentSettingsPage } from '@/pages/settings/PaymentSettingsPage'
import { SettingsLayout } from '@/pages/settings/SettingsLayout'
import { ShopSettingsPage } from '@/pages/settings/ShopSettingsPage'
import { TariffsPage } from '@/pages/TariffsPage'

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
        { path: 'payments', Component: PaymentsPage },
        { path: 'payments/:paymentId', Component: PaymentCardPage },
        { path: 'tariffs', Component: TariffsPage },
        {
          path: 'settings',
          Component: SettingsLayout,
          children: [
            { index: true, element: <Navigate to="brand" replace /> },
            { path: 'brand', Component: BrandSettingsPage },
            { path: 'payment', Component: PaymentSettingsPage },
            { path: 'shop', Component: ShopSettingsPage },
            { path: 'panel', Component: PanelSettingsPage },
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
