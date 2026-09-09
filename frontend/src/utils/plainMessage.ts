import { marked } from 'marked'

interface TextToken {
  type: string
  text?: string
  raw?: string
  tokens?: TextToken[]
  href?: string
  ordered?: boolean
  start?: number
  items?: TextToken[]
  header?: TextToken[]
  rows?: TextToken[][]
}

function inline(tokens: TextToken[]): string {
  return tokens.map(token => {
    if (token.type === 'br') return '\n'
    if (token.type === 'link') {
      const label = inline(token.tokens || [])
      return /^https?:\/\//i.test(token.href || '') ? `${label} (${token.href})` : label
    }
    return token.tokens?.length ? inline(token.tokens) : token.text || token.raw || ''
  }).join('')
}

function blocks(tokens: TextToken[]): string {
  return tokens.map(token => {
    if (token.type === 'space' || token.type === 'def') return ''
    if (token.type === 'list') return (token.items || []).map((item, index) =>
      `${token.ordered ? `${(token.start || 1) + index}.` : '-'} ${blocks(item.tokens || []).trim()}`,
    ).join('\n')
    if (token.type === 'blockquote') return blocks(token.tokens || [])
    if (token.type === 'table') return [token.header || [], ...(token.rows || [])].map(row =>
      row.map(cell => inline(cell.tokens || [{ type: 'text', text: cell.text || '' }])).join(' | '),
    ).join('\n')
    return token.tokens?.length ? inline(token.tokens) : token.text || token.raw || ''
  }).filter(Boolean).join('\n\n')
}

export function plainMessage(content: string): string {
  return blocks(marked.lexer(content.slice(0, 200000)) as TextToken[]).trim()
}
