import type { AssistantSource } from '@/types'

export function assistantSourceDisplay(source: AssistantSource) {
  const reference = source.kind === 'skill_method' || source.kind === 'mcp_tool_catalog'
  const skill = source.kind === 'skill_method'
  const handoff = source.kind === 'distillation'
  const libraryPath = handoff && source.data_source_id ? `/data-sources?source_id=${encodeURIComponent(source.data_source_id)}` : null
  const mark = handoff ? '交接' : reference ? skill ? '方法' : '契约' : source.citation_id || (source.kind === 'rag' ? '引用' : '附件')
  const origin = reference ? skill ? '技能方法 · 建模参考' : 'MCP 工具契约 · 建模参考'
    : handoff ? '业务蒸馏 · 资料库交接版本' : source.data_source_name || (source.file_id ? '正式资料库' : '临时上下文')
  const canPreview = reference || !!source.file_id || !!libraryPath
  const notice = reference
    ? '这是本次建模实际读取的方法或工具目录摘要，仅作为建模参考；不代表已执行技能脚本或 MCP 业务工具。'
    : '以下内容按当前账号权限重新读取；历史引用失效或权限收回后将无法显示。'
  return {
    reference, mark, origin, canPreview, notice, libraryPath,
    title: handoff ? `在资料库查看交接版本：${source.filename}` : reference ? `查看建模参考：${source.filename}` : source.file_id ? `查看引用原文：${source.filename}` : '本次对话的临时附件',
    inlineText: reference ? (source.snippet || (skill ? '已读取技能方法，未执行脚本。' : '已读取 MCP 工具契约目录，未调用工具。')).slice(0, 5000) : null,
  }
}
