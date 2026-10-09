import { onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { pluginPublicationsApi } from '@/api/pluginPublications'
import type { PluginArtifact } from '@/types/pluginArtifact'
import type { PluginPublication } from '@/types/pluginPublication'

export function usePluginPublication(artifact: Ref<PluginArtifact | null>) {
  const publication = ref<PluginPublication | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref('')
  let generation = 0
  let controller: AbortController | undefined
  async function load() {
    controller?.abort()
    const current = ++generation
    const item = artifact.value
    publication.value = null
    error.value = ''
    saving.value = false
    if (!item) { loading.value = false; return }
    controller = new AbortController()
    loading.value = true
    try {
      const result = await pluginPublicationsApi.get(item.id, controller.signal)
      if (current !== generation) return
      if (result.artifact_id !== item.id) throw new Error('发布状态与当前插件不一致，请重新读取')
      publication.value = result
    } catch (caught: unknown) { if (current === generation) error.value = caught instanceof Error ? caught.message : '发布状态读取失败' }
    finally { if (current === generation) loading.value = false }
  }
  async function update(action: 'publish' | 'withdraw', confirmed: boolean) {
    const item = artifact.value
    const state = publication.value
    if (!item || !state || saving.value || loading.value || (action === 'publish' && !confirmed)) return false
    controller?.abort()
    controller = new AbortController()
    const current = ++generation
    saving.value = true
    error.value = ''
    try {
      const result = await pluginPublicationsApi.update(item.id, { artifact_hash: item.artifact_hash, expected_revision: state.revision, action, confirmed_publication: confirmed }, controller.signal)
      if (current !== generation) return false
      if (result.artifact_id !== item.id) throw new Error('发布结果与当前插件不一致，请重新读取')
      publication.value = result
      return true
    } catch (caught: unknown) { if (current === generation) error.value = caught instanceof Error ? caught.message : '发布操作失败，保留当前状态，请重新读取后重试'; return false }
    finally { if (current === generation) saving.value = false }
  }
  watch(artifact, load, { immediate: true })
  onBeforeUnmount(() => { generation++; controller?.abort() })
  return { publication, loading, saving, error, load, update }
}
