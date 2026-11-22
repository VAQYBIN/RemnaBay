import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Админку отдаёт сервер магазина под /admin/ (решения 0025, 0051).
// При разработке запросы к API и логотипам идут на бэкенд из compose.dev.yaml.
const backend = process.env.REMNABAY_BACKEND ?? 'http://localhost:8000'

export default defineConfig({
  base: '/admin/',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': backend,
      '/brand': backend,
    },
  },
})
