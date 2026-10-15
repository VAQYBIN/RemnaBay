/** Разметка Telegram (HTML) для предпросмотра и счётчика рассылки (11.1, А8).
 *
 *  Разбор повторяет серверный (src/remnabay/broadcasts/_format.py): те же теги, те же
 *  мнемоники, та же длина — в единицах UTF-16, как `string.length`. Окончательную
 *  проверку делает сервер; здесь — чтобы владелец видел ошибку и счётчик сразу. */

export type EntityType =
  | 'bold'
  | 'italic'
  | 'underline'
  | 'strikethrough'
  | 'spoiler'
  | 'text_link'
  | 'code'
  | 'pre'
  | 'blockquote'
  | 'expandable_blockquote'

export type MarkupNode =
  | { kind: 'text'; text: string }
  | { kind: 'entity'; type: EntityType; url?: string; children: MarkupNode[] }

export type ParsedMarkup = {
  nodes: MarkupNode[]
  /** Длина текста без разметки. */
  length: number
  /** Ошибка разметки — тем же текстом, что покажет сервер. */
  problem: string | null
}

// Ограничения Telegram: текст сообщения и подпись к изображению (11.1)
export const MAX_TEXT_LENGTH = 4096
export const MAX_CAPTION_LENGTH = 1024

const SIMPLE_TAGS: Record<string, EntityType> = {
  b: 'bold',
  strong: 'bold',
  i: 'italic',
  em: 'italic',
  u: 'underline',
  ins: 'underline',
  s: 'strikethrough',
  strike: 'strikethrough',
  del: 'strikethrough',
  'tg-spoiler': 'spoiler',
  code: 'code',
  pre: 'pre',
  blockquote: 'blockquote',
}
const ENTITY_REFS: Record<string, string> = { lt: '<', gt: '>', amp: '&', quot: '"' }
const LINK_SCHEMES = ['https://', 'http://', 'tg://']

// Тег, комментарий или мнемоника; остальное — текст
const TOKEN = /<!--[\s\S]*?(?:-->|$)|<![^>]*>?|<\?[^>]*>?|<(\/?)([a-zA-Z][^\t\n\r\f />]*)([^>]*?)(\/?)>|&(#[xX][0-9a-fA-F]+|#\d+|[a-zA-Z][a-zA-Z0-9]*);/g
const ATTRIBUTE = /([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g

type Open = { tag: string; node: Extract<MarkupNode, { kind: 'entity' }> | null; merged: boolean }

function attributes(source: string): Record<string, string | null> {
  const values: Record<string, string | null> = {}
  for (const match of source.matchAll(ATTRIBUTE)) {
    values[match[1].toLowerCase()] = match[2] ?? match[3] ?? match[4] ?? null
  }
  return values
}

class MarkupError extends Error {}

/** Разбирает разметку; пробелы и переносы по краям отбрасываются, как на сервере. */
export function parseMarkup(markup: string): ParsedMarkup {
  const root: MarkupNode[] = []
  const stack: Open[] = []
  let length = 0
  const target = (): MarkupNode[] => {
    for (let index = stack.length - 1; index >= 0; index -= 1) {
      const node = stack[index].node
      if (node) return node.children
    }
    return root
  }
  const addText = (text: string) => {
    if (text === '') return
    target().push({ kind: 'text', text })
    length += text.length
  }
  const source = markup.trim()
  try {
    let position = 0
    for (const match of source.matchAll(TOKEN)) {
      addText(source.slice(position, match.index))
      position = match.index + match[0].length
      const [whole, closing, rawTag, rawAttributes, selfClosing, reference] = match
      if (reference !== undefined) {
        if (reference.startsWith('#')) {
          const hex = reference[1] === 'x' || reference[1] === 'X'
          const code = parseInt(reference.slice(hex ? 2 : 1), hex ? 16 : 10)
          if (!Number.isFinite(code) || code > 0x10ffff) throw new MarkupError(`Неверный код символа «${whole}»`)
          addText(String.fromCodePoint(code))
        } else {
          addText(ENTITY_REFS[reference] ?? whole)
        }
        continue
      }
      if (rawTag === undefined) {
        if (whole.startsWith('<!--')) throw new MarkupError('Комментарии <!-- --> в тексте не поддерживаются')
        throw new MarkupError(`Конструкция ${whole} не поддерживается`)
      }
      const tag = rawTag.toLowerCase()
      if (selfClosing) throw new MarkupError(`Тег <${tag}/> не поддерживается: перенос строки — обычный Enter`)
      if (closing) {
        const top = stack[stack.length - 1]
        if (!top || top.tag !== tag) {
          const expected = top ? `</${top.tag}>` : 'открывающего тега'
          throw new MarkupError(`Лишний или не на месте </${tag}>: ожидался ${expected}`)
        }
        stack.pop()
        continue
      }
      const values = attributes(rawAttributes)
      let type: EntityType
      let url: string | undefined
      if (tag === 'a') {
        url = (values.href ?? '').trim()
        if (!LINK_SCHEMES.some((scheme) => url?.startsWith(scheme))) {
          throw new MarkupError(`Ссылка должна начинаться с https://, http:// или tg://: «${url}»`)
        }
        type = 'text_link'
      } else if (tag === 'span') {
        if (values.class !== 'tg-spoiler') throw new MarkupError('Тег <span> поддерживается только как <span class="tg-spoiler">')
        type = 'spoiler'
      } else {
        const simple = SIMPLE_TAGS[tag]
        if (!simple) throw new MarkupError(`Тег <${tag}> не поддерживается`)
        type = tag === 'blockquote' && 'expandable' in values ? 'expandable_blockquote' : simple
      }
      const parent = stack[stack.length - 1]
      const merged =
        tag === 'code' && parent?.tag === 'pre' && parent.node?.children.length === 0 && (values.class ?? '').startsWith('language-')
      if (merged) {
        stack.push({ tag, node: null, merged })
        continue
      }
      const node: Extract<MarkupNode, { kind: 'entity' }> = { kind: 'entity', type, url, children: [] }
      target().push(node)
      stack.push({ tag, node, merged })
    }
    addText(source.slice(position))
    if (stack.length > 0) throw new MarkupError(`Не закрыт тег <${stack[stack.length - 1].tag}>`)
    return { nodes: root, length, problem: null }
  } catch (error) {
    if (error instanceof MarkupError) return { nodes: [{ kind: 'text', text: source }], length, problem: error.message }
    throw error
  }
}

/** Что ставят кнопки над полем: тег вокруг выделенного. */
export const FORMATS = {
  bold: ['<b>', '</b>'],
  italic: ['<i>', '</i>'],
  underline: ['<u>', '</u>'],
  strikethrough: ['<s>', '</s>'],
  spoiler: ['<tg-spoiler>', '</tg-spoiler>'],
  quote: ['<blockquote>', '</blockquote>'],
  code: ['<code>', '</code>'],
} as const satisfies Record<string, readonly [string, string]>

/** Экранирует текст для вставки в разметку (подпись ссылки, адрес). */
export function escapeMarkup(text: string): string {
  return text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;')
}
