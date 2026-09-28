import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('platform settings separates MCP connections from fixed platform tools', () => {
  const dialog = readFileSync(new URL('../src/components/platform/PlatformSettingsDialog.vue', import.meta.url), 'utf8')
  const tabs = readFileSync(new URL('../src/utils/platformSettings.ts', import.meta.url), 'utf8')

  assert.match(dialog, /<el-tab-pane label="MCP" name="mcp"/)
  assert.doesNotMatch(dialog, /ToolSettingsPanel|label="工具"|name="tools"/)
  assert.doesNotMatch(tabs, /'tools'/)
})

test('only system superadmins can expose model and MCP sharing controls', () => {
  const modelPanel = readFileSync(new URL('../src/components/platform/ModelSettingsPanel.vue', import.meta.url), 'utf8')
  const mcpPanel = readFileSync(new URL('../src/components/platform/McpSettingsPanel.vue', import.meta.url), 'utf8')

  assert.match(modelPanel, /auth\.user\?\.system_role === 'superadmin'/)
  assert.match(modelPanel, /v-if="canShareAcrossWorkspaces" label="允许其他工作区使用"/)
  assert.match(modelPanel, /v-if="canManage && config\.is_owned"[^\n]*> 编辑/)
  assert.match(mcpPanel, /auth\.user\?\.system_role === 'superadmin'/)
  assert.match(mcpPanel, /v-if="canShareAcrossWorkspaces" label="允许其他工作区使用"/)
  assert.match(mcpPanel, /v-if="canManage && m\.is_owned"[^\n]*> 编辑/)
  assert.match(mcpPanel, /查看远端工具/)
})

test('only unbound integration keys receive a delete action', () => {
  const view = readFileSync(new URL('../src/views/CapabilityAccess.vue', import.meta.url), 'utf8')
  const api = readFileSync(new URL('../src/api/capabilityAccess.ts', import.meta.url), 'utf8')

  assert.match(view, /v-if="row\.scenario_id && row\.status === 'active'"[\s\S]*?@click="revokeKey\(row\)"/)
  assert.match(view, /v-else-if="!row\.scenario_id"[\s\S]*?@click="deleteUnboundKey\(row\)"/)
  assert.match(view, /审计记录会保留/)
  assert.match(api, /deleteUnboundKey: \(keyId: string\) => http\.delete/)
})
