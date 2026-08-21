<script lang="ts">
import { defineComponent, h, type PropType, type VNodeChild } from 'vue'
import { marked } from 'marked'

/**
 * Minimal, structural Markdown renderer.
 *
 * `marked.parse()` produces an HTML string, which is unsafe for model and
 * document content when fed to a raw HTML injection point. Rendering the token stream as Vue
 * VNodes lets Vue escape all text by default and gives us one narrow URL
 * allow-list for links.
 */
type MarkdownToken = {
  type: string
  raw?: string
  text?: string
  tokens?: MarkdownToken[]
  depth?: number
  href?: string
  title?: string | null
  ordered?: boolean
  start?: number
  items?: MarkdownToken[]
  header?: MarkdownToken[]
  rows?: MarkdownToken[][]
  align?: Array<'left' | 'center' | 'right' | null>
  lang?: string
}

function safeHref(value?: string) {
  const href = (value || '').trim()
  if (!href) return ''
  // Keep same-origin routes and fragment links, but reject protocol-relative
  // URLs so a model cannot turn a benign relative-looking link into an
  // external destination.
  if (href.startsWith('/') && !href.startsWith('//')) return href
  if (href.startsWith('#') || href.startsWith('?')) return href
  try {
    const url = new URL(href)
    return ['https:', 'http:', 'mailto:'].includes(url.protocol) ? href : ''
  } catch {
    return ''
  }
}

function inline(tokens: MarkdownToken[] = []): VNodeChild[] {
  return tokens.map((token, index) => {
    const key = `${token.type}-${index}`
    switch (token.type) {
      case 'strong':
        return h('strong', { key }, inline(token.tokens))
      case 'em':
        return h('em', { key }, inline(token.tokens))
      case 'del':
        return h('del', { key }, inline(token.tokens))
      case 'codespan':
        return h('code', { key }, token.text || '')
      case 'br':
        return h('br', { key })
      case 'link': {
        const href = safeHref(token.href)
        // Do not create a clickable element for an unsafe URL. Its readable
        // label stays visible so the assistant response is not silently lost.
        if (!href) return h('span', { key }, inline(token.tokens))
        const external = /^https?:\/\//i.test(href)
        return h('a', {
          key,
          href,
          title: token.title || undefined,
          target: external ? '_blank' : undefined,
          rel: external ? 'noopener noreferrer' : undefined,
        }, inline(token.tokens))
      }
      case 'image':
        // Images from untrusted Markdown can trigger remote requests and have
        // historically been a common XSS/vector. Preserve the alt text only.
        return h('span', { key }, token.text || '（图片）')
      case 'html':
        // Raw HTML is intentionally rendered as text. Vue escapes strings,
        // so tags such as <script> and event handlers remain inert.
        return h('span', { key, class: 'md-raw-html' }, token.raw || token.text || '')
      case 'text':
        return token.tokens?.length
          ? h('span', { key }, inline(token.tokens))
          : token.text || token.raw || ''
      default:
        return token.tokens?.length
          ? h('span', { key }, inline(token.tokens))
          : token.text || token.raw || ''
    }
  })
}

function blocks(tokens: MarkdownToken[] = []): VNodeChild[] {
  return tokens.flatMap((token, index) => {
    const key = `${token.type}-${index}`
    switch (token.type) {
      case 'space':
      case 'def':
        return []
      case 'heading':
        return [h(`h${Math.min(Math.max(token.depth || 2, 1), 6)}`, { key }, inline(token.tokens))]
      case 'paragraph':
        return [h('p', { key }, inline(token.tokens))]
      case 'text':
        return [h('p', { key }, inline(token.tokens?.length ? token.tokens : [token]))]
      case 'blockquote':
        return [h('blockquote', { key }, blocks(token.tokens))]
      case 'code':
        return [h('pre', { key }, [h('code', token.text || '')])]
      case 'hr':
        return [h('hr', { key })]
      case 'list': {
        const tag = token.ordered ? 'ol' : 'ul'
        return [h(tag, { key, start: token.ordered && token.start ? token.start : undefined },
          (token.items || []).map((item, itemIndex) => h('li', { key: `${key}-item-${itemIndex}` }, blocks(item.tokens))),
        )]
      }
      case 'table': {
        const cell = (cellToken: MarkdownToken, cellIndex: number, header = false) => h(
          header ? 'th' : 'td',
          {
            key: `${key}-cell-${cellIndex}`,
            style: token.align?.[cellIndex] ? { textAlign: token.align[cellIndex] } : undefined,
          },
          inline(cellToken.tokens?.length ? cellToken.tokens : [cellToken]),
        )
        return [h('table', { key }, [
          h('thead', [h('tr', (token.header || []).map((item, itemIndex) => cell(item, itemIndex, true)))]),
          h('tbody', (token.rows || []).map((row, rowIndex) => h('tr', { key: `${key}-row-${rowIndex}` }, row.map((item, itemIndex) => cell(item, itemIndex))))),
        ])]
      }
      case 'html':
        return [h('p', { key, class: 'md-raw-html' }, token.raw || token.text || '')]
      default:
        return [h('p', { key }, inline(token.tokens?.length ? token.tokens : [token]))]
    }
  })
}

export default defineComponent({
  name: 'SafeMarkdown',
  props: {
    content: { type: String as PropType<string>, default: '' },
  },
  setup(props) {
    return () => {
      let tokens: MarkdownToken[] = []
      try {
        tokens = marked.lexer(props.content || '', { gfm: true, breaks: true }) as unknown as MarkdownToken[]
      } catch {
        tokens = [{ type: 'text', text: props.content || '' }]
      }
      return h('div', { class: 'md-body' }, blocks(tokens))
    }
  },
})
</script>
