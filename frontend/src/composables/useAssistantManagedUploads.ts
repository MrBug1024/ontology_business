import { computed, onBeforeUnmount, ref, type ComputedRef, type Ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type { AssistantAttachment, ManagedUploadRun } from '@/types'
import { migrateManagedUploadRunEntry } from '@/utils/managedUploadIdentity'

export interface AssistantManagedUploadOptions {
  attachments: Ref<AssistantAttachment[]>
}

export interface AssistantManagedUploads {
  uploadingFiles: Ref<number>
  activeUploadCount: ComputedRef<number>
  attachmentIsSendable: (item: AssistantAttachment) => boolean
  attachmentStatusLabel: (item: AssistantAttachment) => string
  attachmentStatusType: (item: AssistantAttachment) => 'success' | 'warning' | 'danger' | 'info'
  canRetryAttachment: (item: AssistantAttachment) => boolean
  retryAttachment: (item: AssistantAttachment) => Promise<void>
  onFilesPicked: (event: Event) => Promise<void>
  removeAttachment: (item: AssistantAttachment) => Promise<void>
  resetManagedUploads: () => void
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback
}

function isAbortError(error: unknown) {
  return error instanceof DOMException
    ? error.name === 'AbortError'
    : Boolean(error && typeof error === 'object' && 'name' in error && error.name === 'AbortError')
}

function abortableDelay(milliseconds: number, signal: AbortSignal) {
  if (signal.aborted) return Promise.resolve(false)
  return new Promise<boolean>((resolve) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', cancel)
      resolve(true)
    }, milliseconds)
    const cancel = () => {
      window.clearTimeout(timer)
      resolve(false)
    }
    signal.addEventListener('abort', cancel, { once: true })
  })
}

function applyManagedUpload(item: AssistantAttachment, run: ManagedUploadRun) {
  const previousRunId = item.id
  item.id = run.id
  item.upload_run_id = run.id
  item.filename = run.filename
  item.size = run.byte_size || run.declared_byte_size
  item.status = run.status
  item.revision = run.revision
  item.error = run.error?.message || ''
  return previousRunId
}

export function attachmentIsSendable(item: AssistantAttachment) {
  if (item.status === 'parsed') return true
  if (!item.upload_run_id || ['failed', 'cancelled', 'error'].includes(item.status)) return false
  return item.status !== 'awaiting_upload' || !item.error
}

export function useAssistantManagedUploads(
  options: AssistantManagedUploadOptions,
): AssistantManagedUploads {
  const uploadingFiles = ref(0)
  const uploadControllers = new Map<string, AbortController>()
  const uploadSourceFiles = new Map<string, File>()
  let disposed = false
  let uploadScope = 0

  const activeUploadCount = computed(() => options.attachments.value.filter((item) => (
    Boolean(item.upload_run_id)
    && ['awaiting_upload', 'uploading', 'stored', 'processing'].includes(item.status)
  )).length)

  function scopeIsCurrent(scope: number) {
    return !disposed && scope === uploadScope
  }

  function controllerIsCurrent(runId: string, controller: AbortController, scope: number) {
    return scopeIsCurrent(scope) && uploadControllers.get(runId) === controller
  }

  function applyTrackedManagedUpload(item: AssistantAttachment, run: ManagedUploadRun) {
    const previousRunId = applyManagedUpload(item, run)
    migrateManagedUploadRunEntry(uploadSourceFiles, previousRunId, run.id)
    migrateManagedUploadRunEntry(uploadControllers, previousRunId, run.id)
  }

  function attachmentStatusLabel(item: AssistantAttachment) {
    if (item.status === 'parsed' || item.status === 'ready') return '已准备'
    if (item.status === 'failed' || item.status === 'error') return '失败'
    if (item.status === 'cancelled') return '已取消'
    if (item.status === 'awaiting_upload') return item.error ? '等待重试' : '等待上传'
    if (item.status === 'uploading') return `${item.progress || 0}%`
    if (item.status === 'stored') return '等待解析'
    if (item.status === 'processing') return '正在解析'
    return '处理中'
  }

  function attachmentStatusType(item: AssistantAttachment): 'success' | 'warning' | 'danger' | 'info' {
    if (item.status === 'parsed' || item.status === 'ready') return 'success'
    if (item.status === 'failed' || item.status === 'error') return 'danger'
    if (item.status === 'cancelled') return 'info'
    return 'warning'
  }

  function canRetryAttachment(item: AssistantAttachment) {
    return Boolean(item.upload_run_id && uploadSourceFiles.has(item.id) && (
      item.status === 'failed'
      || item.status === 'error'
      || (item.status === 'awaiting_upload' && item.error)
    ))
  }

  async function trackManagedUpload(
    item: AssistantAttachment,
    controller: AbortController,
    scope: number,
  ) {
    let consecutiveFailures = 0
    while (controllerIsCurrent(item.id, controller, scope)) {
      if (['ready', 'failed', 'cancelled'].includes(item.status)) break
      if (!await abortableDelay(750, controller.signal)) break
      if (!controllerIsCurrent(item.id, controller, scope)) break
      try {
        const run = await api.getManagedUploadRun(item.id, controller.signal)
        if (!controllerIsCurrent(item.id, controller, scope)) break
        applyTrackedManagedUpload(item, run)
        consecutiveFailures = 0
      } catch (error: unknown) {
        if (isAbortError(error) || !controllerIsCurrent(item.id, controller, scope)) break
        consecutiveFailures += 1
        item.error = errorMessage(error, '读取附件状态失败')
        if (consecutiveFailures >= 4) break
        if (!await abortableDelay(
          Math.min(750 * (2 ** consecutiveFailures), 5000),
          controller.signal,
        )) break
      }
    }
    if (controllerIsCurrent(item.id, controller, scope) && item.status === 'ready') {
      uploadControllers.delete(item.id)
      uploadSourceFiles.delete(item.id)
    }
  }

  async function uploadManagedContent(item: AssistantAttachment, file: File, scope = uploadScope) {
    if (!scopeIsCurrent(scope)) return
    const controller = new AbortController()
    uploadControllers.get(item.id)?.abort()
    uploadControllers.set(item.id, controller)
    item.error = ''
    try {
      const run = await api.uploadManagedRunContent({
        runId: item.id,
        expectedRevision: item.revision || 1,
        file,
        signal: controller.signal,
        onProgress: (percent) => {
          if (!controllerIsCurrent(item.id, controller, scope)) return
          item.status = 'uploading'
          item.progress = percent
        },
      })
      if (!controllerIsCurrent(item.id, controller, scope)) return
      applyTrackedManagedUpload(item, run)
      await trackManagedUpload(item, controller, scope)
    } catch (error: unknown) {
      if (!controllerIsCurrent(item.id, controller, scope)) return
      if (controller.signal.aborted) {
        item.status = 'cancelled'
        item.error = '已取消本地上传'
        return
      }
      try {
        const run = await api.getManagedUploadRun(item.id, controller.signal)
        if (!controllerIsCurrent(item.id, controller, scope)) return
        applyTrackedManagedUpload(item, run)
        if (['stored', 'processing', 'uploading'].includes(run.status)) {
          await trackManagedUpload(item, controller, scope)
        } else if (run.status === 'ready') {
          uploadControllers.delete(item.id)
          uploadSourceFiles.delete(item.id)
        } else if (run.status === 'awaiting_upload') {
          item.error = errorMessage(error, '上传未到达服务端，请重试')
        }
      } catch (statusError: unknown) {
        if (!controllerIsCurrent(item.id, controller, scope) || isAbortError(statusError)) return
        item.status = 'error'
        item.error = errorMessage(error, '上传失败')
      }
    }
  }

  async function retryAttachment(item: AssistantAttachment) {
    const file = uploadSourceFiles.get(item.id)
    if (!file || !item.upload_run_id) return
    const scope = uploadScope
    try {
      if (item.status === 'failed') {
        const run = await api.retryManagedUploadRun(item.id, item.revision || 1)
        if (!scopeIsCurrent(scope)) return
        applyTrackedManagedUpload(item, run)
        if (run.status === 'stored' || run.status === 'processing') {
          const controller = new AbortController()
          uploadControllers.get(item.id)?.abort()
          uploadControllers.set(item.id, controller)
          void trackManagedUpload(item, controller, scope)
          return
        }
      }
      void uploadManagedContent(item, file, scope)
    } catch (error: unknown) {
      if (scopeIsCurrent(scope)) item.error = errorMessage(error, '重试失败')
    }
  }

  async function uploadTemporaryFiles(files: File[]) {
    const scope = uploadScope
    for (const file of files) {
      if (!scopeIsCurrent(scope)) break
      uploadingFiles.value += 1
      try {
        const run = await api.createManagedUploadRun({
          filename: file.name,
          byte_size: file.size,
          media_type: file.type,
          purpose: 'invocation_attachment',
          idempotency_key: typeof crypto?.randomUUID === 'function'
            ? crypto.randomUUID()
            : `assistant-upload-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          expires_in_seconds: 6 * 60 * 60,
        })
        if (!scopeIsCurrent(scope)) return
        const item: AssistantAttachment = {
          id: run.id,
          upload_run_id: run.id,
          filename: run.filename,
          mime: file.type,
          size: run.declared_byte_size,
          status: run.status,
          revision: run.revision,
          progress: 0,
          error: run.error?.message || '',
          created_at: run.created_at,
        }
        options.attachments.value.push(item)
        uploadSourceFiles.set(run.id, file)
        void uploadManagedContent(item, file, scope)
      } catch (error: unknown) {
        if (scopeIsCurrent(scope)) {
          ElMessage.error(`${file.name} 登记失败：${errorMessage(error, '请求失败')}`)
        }
      } finally {
        if (scopeIsCurrent(scope)) {
          uploadingFiles.value = Math.max(uploadingFiles.value - 1, 0)
        }
      }
    }
  }

  async function onFilesPicked(event: Event) {
    const target = event.target as HTMLInputElement
    const files = Array.from(target.files || [])
    await uploadTemporaryFiles(files)
    target.value = ''
  }

  async function removeAttachment(item: AssistantAttachment) {
    const scope = uploadScope
    options.attachments.value = options.attachments.value.filter((candidate) => candidate.id !== item.id)
    if (item.upload_run_id) {
      uploadControllers.get(item.id)?.abort()
      uploadControllers.delete(item.id)
      uploadSourceFiles.delete(item.id)
      try {
        const current = await api.getManagedUploadRun(item.id)
        if (current.status !== 'ready' && current.status !== 'cancelled') {
          await api.cancelManagedUploadRun(item.id, current.revision)
        }
        if (scopeIsCurrent(scope)) {
          ElMessage.info(current.status === 'ready' ? '已从本次需求移除附件' : '附件上传已取消')
        }
      } catch (error: unknown) {
        if (scopeIsCurrent(scope)) {
          ElMessage.warning(errorMessage(error, '附件已从本次需求移除，后台状态稍后清理'))
        }
      }
      return
    }
    try {
      await api.deleteAssistantAttachment(item.id)
    } catch {
      // Legacy attachments are scoped to the current request; local removal is sufficient.
    }
  }

  function resetManagedUploads() {
    uploadScope += 1
    uploadControllers.forEach((controller) => controller.abort())
    uploadControllers.clear()
    uploadSourceFiles.clear()
    uploadingFiles.value = 0
  }

  onBeforeUnmount(() => {
    disposed = true
    resetManagedUploads()
  })

  return {
    uploadingFiles,
    activeUploadCount,
    attachmentIsSendable,
    attachmentStatusLabel,
    attachmentStatusType,
    canRetryAttachment,
    retryAttachment,
    onFilesPicked,
    removeAttachment,
    resetManagedUploads,
  }
}
