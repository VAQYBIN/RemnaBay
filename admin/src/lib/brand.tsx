import { useEffect } from 'react'

import { $api, type Schemas } from '@/api/client'

export type Brand = Schemas['BrandOut']
export type ThemeTokens = Record<string, string>

function block(selector: string, tokens: ThemeTokens): string {
  const lines = Object.entries(tokens).map(([name, value]) => `  --rb-${name}: ${value};`)
  return `${selector} {\n${lines.join('\n')}\n}`
}

/** Токены бренда оператора поверх токенов RemnaBay по умолчанию (1.23, 1.24). */
export function brandCss(tokens: { light: ThemeTokens; dark: ThemeTokens }): string {
  return [
    block(':root,\n:root[data-theme="dark"]', tokens.dark),
    block(':root[data-theme="light"]', tokens.light),
  ].join('\n')
}

/** Бренд оператора: экран входа и все экраны админки (1.23). Без входа. */
export function useBrand() {
  return $api.useQuery('get', '/api/admin/brand', {}, { staleTime: 60_000 })
}

/** Подставляет цвета бренда и название во вкладку браузера. Изменение видно сразу:
 *  после сохранения в настройках запрос бренда обновляется. */
export function BrandStyle() {
  const { data } = useBrand()
  useEffect(() => {
    if (data) document.title = data.name
  }, [data])
  if (!data) return null
  return <style>{brandCss(data.tokens)}</style>
}
