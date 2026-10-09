export interface SourceSelection { value: string; start: number; end: number }
export interface SourceToken { text: string; kind: 'plain' | 'keyword' | 'string' | 'number' | 'comment' | 'heading' }
export interface PluginFileNode { path: string; name: string; directory: boolean; children: PluginFileNode[] }

export function indentSource(value: string, start: number, end: number, outdent = false, width = 2): SourceSelection {
  const first = Math.max(0, Math.min(start, value.length))
  const last = Math.max(first, Math.min(end, value.length))
  const indent = ' '.repeat(width)
  if (!outdent && first === last) return { value: value.slice(0, first) + indent + value.slice(last), start: first + width, end: first + width }
  const lineStart = first === 0 ? 0 : value.lastIndexOf('\n', first - 1) + 1
  const lineEnd = last > first && value[last - 1] === '\n' ? last - 1 : last
  const block = value.slice(lineStart, lineEnd)
  let firstDelta = 0
  let totalDelta = 0
  const updated = block.split('\n').map((line, index) => {
    const removed = outdent ? (line.startsWith('\t') ? 1 : Math.min(width, line.match(/^ */)?.[0].length || 0)) : 0
    const delta = outdent ? -removed : width
    if (index === 0) firstDelta = delta
    totalDelta += delta
    return outdent ? line.slice(removed) : indent + line
  }).join('\n')
  return { value: value.slice(0, lineStart) + updated + value.slice(lineEnd), start: Math.max(lineStart, first + firstDelta), end: Math.max(lineStart, last + totalDelta) }
}

const PYTHON_KEYWORDS = new Set('async await def return import from as if elif else True False None and or not in is for pass raise'.split(' '))

export function sourceTokens(value: string, path: string): SourceToken[][] {
  const python = path.endsWith('.py')
  const json = path.endsWith('.json')
  return value.split('\n').map(line => {
    if (!python && !json) return [{ text: line, kind: /^\s*#{1,6}\s|^---$|^```/.test(line) ? 'heading' : 'plain' }]
    const tokens: SourceToken[] = []
    const matches = line.matchAll(/#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b\d+(?:\.\d+)?\b|\b[a-zA-Z_][a-zA-Z_0-9]*\b/g)
    let cursor = 0
    for (const match of matches) {
      const index = match.index
      if (index > cursor) tokens.push({ text: line.slice(cursor, index), kind: 'plain' })
      const word = match[0]
      const kind = python && word.startsWith('#') ? 'comment' : /^['"]/.test(word) ? 'string' : /^\d/.test(word) ? 'number' : (python ? PYTHON_KEYWORDS.has(word) : ['true', 'false', 'null'].includes(word)) ? 'keyword' : 'plain'
      tokens.push({ text: word, kind })
      cursor = index + word.length
      if (kind === 'comment') break
    }
    if (cursor < line.length) tokens.push({ text: line.slice(cursor), kind: 'plain' })
    return tokens.length ? tokens : [{ text: '', kind: 'plain' }]
  })
}

export function pluginFileTree(paths: string[]): PluginFileNode[] {
  const root: PluginFileNode[] = []
  for (const path of paths) {
    const parts = path.split('/')
    let siblings = root
    for (let i = 0; i < parts.length; i++) {
      const name = parts[i] || ''
      const nodePath = parts.slice(0, i + 1).join('/')
      let node = siblings.find(item => item.path === nodePath)
      if (!node) { node = { path: nodePath, name, directory: i < parts.length - 1, children: [] }; siblings.push(node) }
      siblings = node.children
    }
  }
  function sort(nodes: PluginFileNode[]) { nodes.sort((a, b) => Number(b.directory) - Number(a.directory) || a.name.localeCompare(b.name)); for (const node of nodes) sort(node.children) }
  sort(root)
  return root
}
