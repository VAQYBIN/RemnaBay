import { useCallback, useEffect, useSyncExternalStore } from 'react'

/** Тема — выбор участника на этом устройстве, а не настройка магазина (1.25, 0038). */
export type ThemePreference = 'system' | 'light' | 'dark'
export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'remnabay.theme'
const LIGHT_QUERY = '(prefers-color-scheme: light)'
const listeners = new Set<() => void>()

function readPreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'light' || stored === 'dark' || stored === 'system') return stored
  } catch {
    // Хранилище недоступно (приватный режим) — тема устройства
  }
  return 'system'
}

/** Тема устройства, а если она неизвестна — тёмная (1.25). */
function systemTheme(): Theme {
  return window.matchMedia(LIGHT_QUERY).matches ? 'light' : 'dark'
}

export function resolveTheme(preference: ThemePreference): Theme {
  return preference === 'system' ? systemTheme() : preference
}

export function applyTheme(preference: ThemePreference = readPreference()): void {
  document.documentElement.dataset.theme = resolveTheme(preference)
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  const media = window.matchMedia(LIGHT_QUERY)
  media.addEventListener('change', listener)
  return () => {
    listeners.delete(listener)
    media.removeEventListener('change', listener)
  }
}

export function useThemePreference(): [ThemePreference, (value: ThemePreference) => void] {
  const preference = useSyncExternalStore(subscribe, readPreference)
  useEffect(() => {
    applyTheme(preference)
    const media = window.matchMedia(LIGHT_QUERY)
    const follow = () => applyTheme(preference)
    media.addEventListener('change', follow)
    return () => media.removeEventListener('change', follow)
  }, [preference])
  const setPreference = useCallback((value: ThemePreference) => {
    try {
      localStorage.setItem(STORAGE_KEY, value)
    } catch {
      // Не сохранилось — действует до перезагрузки
    }
    applyTheme(value)
    listeners.forEach((listener) => listener())
  }, [])
  return [preference, setPreference]
}
