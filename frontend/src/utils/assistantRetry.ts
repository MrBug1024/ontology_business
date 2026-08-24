import type { AssistantAttachment, AssistantMessage } from '@/types'

function isParsedAttachment(value: unknown): value is AssistantAttachment {
  if (!value || typeof value !== 'object') return false
  const attachment = value as Partial<AssistantAttachment>
  return typeof attachment.id === 'string'
    && attachment.id.trim().length > 0
    && typeof attachment.filename === 'string'
    && attachment.filename.trim().length > 0
    && typeof attachment.size === 'number'
    && Number.isFinite(attachment.size)
    && attachment.status === 'parsed'
}

/**
 * Resolve only the parsed attachments that belonged to the user request paired
 * with the clicked assistant response. This prevents an old retry control from
 * silently borrowing attachments uploaded by a newer request in the thread.
 */
export function retryAttachmentsForMessage(
  messages: AssistantMessage[],
  sourceMessage: AssistantMessage | undefined,
  currentThreadId: string,
): AssistantAttachment[] {
  if (!sourceMessage) return []
  if (currentThreadId && sourceMessage.thread_id && sourceMessage.thread_id !== currentThreadId) return []

  let sourceIndex = messages.indexOf(sourceMessage)
  if (sourceIndex < 0 && sourceMessage.id) {
    sourceIndex = messages.findIndex((message) => message.id === sourceMessage.id)
  }
  if (sourceIndex < 0) return []

  for (let index = sourceIndex - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message.role !== 'user') continue
    if (currentThreadId && message.thread_id && message.thread_id !== currentThreadId) return []

    const seen = new Set<string>()
    const candidates: unknown[] = Array.isArray(message.attachments) ? message.attachments : []
    return candidates.filter((attachment): attachment is AssistantAttachment => {
      if (!isParsedAttachment(attachment) || seen.has(attachment.id)) return false
      seen.add(attachment.id)
      return true
    })
  }
  return []
}

/**
 * Build an editable correction draft for a failed attachment compilation.
 * Returning a draft (instead of submitting it) prevents a recovery click from
 * silently replaying an old chat request.
 */
export function compilationRetryDraft(optionValue: unknown, optionPrompt = '') {
  const value = String(optionValue || '').trim()
  if (!['retry', 'revise_and_retry'].includes(value)) return ''
  const sourceInstruction = '以下是对附件模型的补充/修正，请连同原附件重新编译并保留每项模型的来源段落。'
  const provided = String(optionPrompt || '').trim()
  if (provided) return `${sourceInstruction}\n\n${provided}`
  return value === 'revise_and_retry'
    ? `${sourceInstruction}\n\n需要补充或修正的内容：`
    : `${sourceInstruction}\n\n请重新检查未识别、歧义、冲突和未解析引用，并生成新的复合变更清单。`
}
