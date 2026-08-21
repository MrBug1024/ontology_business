<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>技能（Skills）</h2>
        <div class="sub">受治理的能力单元，供已配置的 Action / 工作流使用（如 OCR 解析、数据分析）</div>
      </div>
      <el-button v-if="canManage" @click="rescan"><el-icon><Refresh /></el-icon> 重新扫描</el-button>
    </div>

    <el-alert
      v-if="!canManage"
      class="readonly-notice"
      type="info"
      title="当前账户为只读：可查看技能信息，不能扫描、启停或测试执行。"
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
              <el-tag size="small" :type="s.source === 'builtin' ? 'success' : 'info'" effect="light">{{ s.source }}</el-tag>
            </div>
            <el-switch v-if="canManage" v-model="s.enabled" @change="(v:any)=>toggle(s, v)" style="margin-left:auto" />
            <el-tag v-else size="small" :type="s.enabled ? 'success' : 'info'" effect="light">{{ s.enabled ? '已启用' : '已停用' }}</el-tag>
          </div>
          <div class="sk-desc">{{ s.description || '暂无描述' }}</div>
          <div class="muted mono sk-path">{{ s.path }}</div>
          <div v-if="canManage" class="sk-actions">
            <el-button size="small" type="primary" plain :disabled="!s.enabled" @click="openExec(s)">
              <el-icon><VideoPlay /></el-icon> 测试执行
            </el-button>
          </div>
        </div>
      </el-col>
    </el-row>
    <div v-if="!loading && !skills.length" class="empty-wrap">
      <div class="empty-icon"><el-icon :size="28"><MagicStick /></el-icon></div>
      <div>{{ canManage ? '暂无技能，将技能目录放入 backend/skills/ 后点击重新扫描' : '暂无可查看的技能' }}</div>
      <el-button v-if="canManage" type="primary" size="small" @click="rescan"><el-icon><Refresh /></el-icon> 重新扫描</el-button>
    </div>

    <!-- 执行对话框 -->
    <el-dialog v-if="canManage" v-model="execDlg" :title="'执行技能：' + (curSkill?.name || '')" width="680px" top="6vh">
      <el-form label-width="80px">
        <el-form-item label="参数">
          <el-input v-model="execArgs" type="textarea" :rows="2" class="mono"
            placeholder="命令行参数，空格分隔，如：--path data.csv --group-by 门店" />
        </el-form-item>
      </el-form>
      <el-button type="primary" :loading="executing" @click="doExec" style="margin-bottom:12px">
        <el-icon><VideoPlay /></el-icon> 执行
      </el-button>
      <div v-if="execOut !== null">
        <div class="muted" style="margin-bottom:6px">输出（exit={{ execExit }}）</div>
        <pre class="code">{{ execOut }}</pre>
      </div>
    </el-dialog>
  </div>
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
const execDlg = ref(false)
const curSkill = ref<Skill | null>(null)
const execArgs = ref('')
const executing = ref(false)
const execOut = ref<string | null>(null)
const execExit = ref(0)
const loading = ref(false)

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
    await api.toggleSkill(s.id, v)
  } catch (e: any) {
    s.enabled = !v
    ElMessage.error(e.message)
  }
}
function openExec(s: Skill) {
  if (!canManage.value) return
  curSkill.value = s
  execArgs.value = ''
  execOut.value = null
  execDlg.value = true
}
async function doExec() {
  if (!canManage.value || !curSkill.value) return
  executing.value = true
  execOut.value = null
  try {
    const args = execArgs.value.trim() ? execArgs.value.trim().split(/\s+/) : []
    const r: any = await api.executeSkill(curSkill.value.id, args)
    execExit.value = r.exit_code ?? 0
    execOut.value = (r.stdout || '') + (r.stderr ? '\n\n[stderr]\n' + r.stderr : '') || '（无输出）'
  } catch (e: any) {
    execOut.value = '执行失败：' + e.message
  } finally {
    executing.value = false
  }
}
onMounted(load)
</script>

<style scoped>
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
.sk-path {
  margin: 8px 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sk-actions { border-top: 1px solid var(--border); padding-top: 8px; }
.readonly-notice { margin-bottom: 16px; }
</style>
