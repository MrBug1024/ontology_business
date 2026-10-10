import { computed, ref, watch, type Ref } from 'vue'
import { createClientRequestId } from '@/utils/clientRequestId'
import type { CodingUpdate, CodingWorkspace } from '@/types/pluginCoding'

export function usePluginCodingEditor(workspace: Ref<CodingWorkspace | null>, revise: (payload: CodingUpdate) => Promise<boolean>) {
  const selectedPath = ref('skills/run-scenario/SKILL.md')
  const draft = ref('')
  const original = ref('')
  const baseHash = ref('')
  const feedback = ref('')
  const message = ref('')
  const selectedFile = computed(() => workspace.value?.files.find(file => file.path === selectedPath.value))
  const dirty = computed(() => Boolean(selectedFile.value?.editable && draft.value !== original.value))
  const basisChanged = computed(() => Boolean(workspace.value && baseHash.value !== workspace.value.files_hash))
  function restoreInstruction(instruction: string) {
    if (feedback.value.trim()) { message.value = '输入框中还有未提交内容，请先发送或清空草稿，再恢复本轮需求'; return false }
    feedback.value = instruction
    message.value = ''
    return true
  }
  function selectFile(path: string) {
    if (dirty.value && path !== selectedPath.value) { message.value = '当前文件有未保存修改，请先保存或提交修正'; return }
    selectedPath.value = path
    draft.value = workspace.value?.files.find(file => file.path === path)?.content || ''
    original.value = draft.value
    baseHash.value = workspace.value?.files_hash || ''
    message.value = ''
  }
  function discardDraft() { original.value = selectedFile.value?.content || ''; draft.value = original.value; baseHash.value = workspace.value?.files_hash || ''; message.value = '' }
  watch(() => workspace.value?.files_hash, () => { if (!dirty.value) selectFile(selectedPath.value) }, { immediate: true })
  async function submit(action: 'save' | 'generate' | 'discuss') {
    const value = workspace.value
    if (!value || (dirty.value && basisChanged.value)) return false
    if (action === 'discuss' && dirty.value) { message.value = '讨论基于已保存文件，请先保存当前代码；本地修改已保留'; return false }
    const edited = dirty.value && selectedFile.value ? [{ path: selectedFile.value.path, content: draft.value }] : []
    const accepted = await revise({ expected_revision: value.revision, request_id: createClientRequestId(), session_id: value.session_id, action,
      base_files_hash: edited.length ? baseHash.value : value.files_hash, instruction: action !== 'save' ? feedback.value : '', files: edited })
    if (accepted) { original.value = draft.value; if (action !== 'save') feedback.value = ''; selectFile(selectedPath.value) }
    return accepted
  }
  async function stopCoding() {
    const value = workspace.value
    if (!value) return
    await revise({ expected_revision: value.revision, request_id: createClientRequestId(), session_id: value.session_id, action: 'stop', base_files_hash: value.files_hash, instruction: '', files: [] })
  }
  return { selectedPath, selectedFile, draft, feedback, dirty, basisChanged, baseHash, message, selectFile, discardDraft, submit, stopCoding, restoreInstruction }
}
