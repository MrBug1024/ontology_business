export interface DiffLine { kind: 'same' | 'added' | 'removed'; text: string; before: number | null; after: number | null }

// Prefix/suffix keep long files bounded. Within the changed region, an LCS
// pairs unchanged lines; oversized regions remain an explicit delete/add block.
export function codingDiff(previous: string, current: string): DiffLine[] {
  const a = previous ? previous.split('\n') : []
  const b = current ? current.split('\n') : []
  const result: DiffLine[] = []
  let prefix = 0
  while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix]) prefix++
  let suffix = 0
  while (suffix < a.length - prefix && suffix < b.length - prefix && a[a.length - suffix - 1] === b[b.length - suffix - 1]) suffix++
  const old = a.slice(prefix, a.length - suffix)
  const next = b.slice(prefix, b.length - suffix)
  for (let i = 0; i < prefix; i++) result.push({ kind: 'same', text: a[i] || '', before: i + 1, after: i + 1 })
  const add = (i: number) => result.push({ kind: 'added', text: next[i] || '', before: null, after: prefix + i + 1 })
  const remove = (i: number) => result.push({ kind: 'removed', text: old[i] || '', before: prefix + i + 1, after: null })
  if (old.length * next.length > 160000) {
    old.forEach((_, i) => remove(i))
    next.forEach((_, i) => add(i))
  } else {
    const lengths = Array.from({ length: old.length + 1 }, () => new Uint32Array(next.length + 1))
    for (let i = old.length - 1; i >= 0; i--) {
      const row = lengths[i]
      if (!row) continue
      for (let j = next.length - 1; j >= 0; j--)
        row[j] = old[i] === next[j] ? (lengths[i + 1]?.[j + 1] || 0) + 1 : Math.max(lengths[i + 1]?.[j] || 0, row[j + 1] || 0)
    }
    let i = 0
    let j = 0
    while (i < old.length || j < next.length) {
      if (i < old.length && j < next.length && old[i] === next[j]) {
        result.push({ kind: 'same', text: old[i] || '', before: prefix + ++i, after: prefix + ++j })
      } else if (j < next.length && (i === old.length || (lengths[i]?.[j + 1] || 0) > (lengths[i + 1]?.[j] || 0))) add(j++)
      else remove(i++)
    }
  }
  for (let i = suffix; i > 0; i--) result.push({ kind: 'same', text: a[a.length - i] || '', before: a.length - i + 1, after: b.length - i + 1 })
  return result
}
