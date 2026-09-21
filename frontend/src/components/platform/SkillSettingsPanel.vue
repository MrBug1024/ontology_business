<template>
  <section class="settings-panel">
    <div class="page-header">
      <div>
        <h2>技能</h2>
        <div class="sub">管理受信方法包。技能提供调查步骤、验收标准和执行边界；实际读取、写入或外部调用仍必须经过平台工具的统一权限与审计。</div>
      </div>
      <el-button v-if="canManage" @click="rescan"><el-icon><Refresh /></el-icon> 刷新技能列表</el-button>
    </div>

    <el-alert
      v-if="!canManage"
      class="readonly-notice"
      type="info"
      title="当前账户为只读：可查看技能信息，不能刷新或启停。"
      show-icon
      :closable="false"
    />

    <el-row :gutter="16" v-loading="loading">
      <el-col :xs="24" :sm="12" :lg="8" v-for="s in skills" :key="s.id">
        <div class="card skill-card">
          <div class="sk-head">
            <div class="sk-icon"><el-icon :size="20"><MagicStick /></el-icon></div>
            <div class="sk-title">
              <div class="sk-name">{{ s.name }}</div>
              <el-tag size="small" :type="s.source === 'builtin' ? 'success' : 'info'" effect="light">{{ skillSourceLabel(s.source) }}</el-tag>
            </div>
            <el-switch v-if="canManage" :model-value="s.enabled" :aria-label="`启用技能 ${s.name}`" @change="(value: string | number | boolean) => toggle(s, value === true)" style="margin-left:auto" />
            <el-tag v-else size="small" :type="s.enabled ? 'success' : 'info'" effect="light">{{ s.enabled ? '已启用' : '已停用' }}</el-tag>
          </div>
          <div class="sk-desc">{{ s.description || '暂无描述' }}</div>
        </div>
      </el-col>
    </el-row>
    <div v-if="!loading && !skills.length" class="empty-wrap">
      <div class="empty-icon"><el-icon :size="28"><MagicStick /></el-icon></div>
      <div>{{ canManage ? '暂无技能，请由管理员安装后刷新技能列表' : '暂无可查看的技能' }}</div>
      <el-button v-if="canManage" type="primary" size="small" @click="rescan"><el-icon><Refresh /></el-icon> 刷新技能列表</el-button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { Skill } from '@/types'

const auth = useAuthStore()
const canManage = computed(() => auth.user?.can_manage === true)
const skills = ref<Skill[]>([])
const loading = ref(false)
function skillSourceLabel(source?: string) {
  return ({ builtin: '平台内置', local: '本地安装', custom: '自定义' } as Record<string, string>)[source || ''] || '本地安装'
}

async function load() {
  loading.value = true
  try {
    skills.value = await api.listSkills()
  } catch (e: any) {
    ElMessage.error('加载失败：' + e.message)
  } finally {
    loading.value = false
  }
}
async function rescan() {
  if (!canManage.value) return
  await api.rescanSkills()
  ElMessage.success('已重新扫描')
  load()
}
async function toggle(s: Skill, v: boolean) {
  if (!canManage.value) return
  try {
    const updated = await api.toggleSkill(s.id, v)
    s.enabled = updated.enabled
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}
onMounted(load)
</script>

<style scoped>
.settings-panel { padding: 24px; }
.page-header h2 { margin: 0 0 8px; font-size: 20px; color: var(--text); }
@media (max-width: 720px) {
  .settings-panel { padding: 18px 14px 24px; }
  .page-header { align-items: stretch; flex-direction: column; gap: 12px; }
  .page-actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .page-actions :deep(.el-button + .el-button) { margin-left: 0; }
}

.skill-card {
  margin-bottom: 16px;
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.skill-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}
.sk-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.sk-icon {
  width: 40px; height: 40px;
  border-radius: 11px;
  background: var(--accent-soft);
  color: var(--accent);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.sk-title { flex: 1; min-width: 0; }
.sk-name {
  font-weight: 700; font-size: 15px; margin-bottom: 3px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sk-desc {
  color: var(--text-2); font-size: 13px;
  min-height: 38px; margin-bottom: 6px;
  line-height: 1.5;
}
.readonly-notice { margin-bottom: 16px; }
</style>
