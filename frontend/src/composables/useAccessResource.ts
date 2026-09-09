import { onScopeDispose, ref, shallowRef } from 'vue'

export function useAccessResource<T>(loader: (signal: AbortSignal) => Promise<T>) {
  const data = shallowRef<T>()
  const loading = ref(false)
  const error = ref('')
  let controller: AbortController | undefined
  let generation = 0
  let disposed = false

  async function reload() {
    if (disposed) return
    controller?.abort()
    const active = new AbortController()
    controller = active
    const current = ++generation
    loading.value = true
    error.value = ''
    try {
      const result = await loader(active.signal)
      if (!disposed && current === generation) data.value = result
    } catch (reason: unknown) {
      if (!disposed && current === generation && !active.signal.aborted) {
        error.value = reason instanceof Error ? reason.message : '加载失败，请重试'
      }
    } finally {
      if (!disposed && current === generation) loading.value = false
    }
  }
  onScopeDispose(() => { disposed = true; generation += 1; controller?.abort() })
  return { data, loading, error, reload }
}
