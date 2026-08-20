<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>仪表盘</h2>
        <div class="sub">业务场景本体智能平台 · 总览</div>
      </div>
      <el-button @click="load" :loading="loading">
        <el-icon v-if="!loading"><Refresh /></el-icon> 刷新
      </el-button>
    </div>

    <!-- 统计卡片 -->
    <el-row :gutter="16">
      <el-col :xs="12" :sm="12" :md="6" v-for="s in stats" :key="s.label">
        <div class="stat-card" role="link" tabindex="0" :aria-label="`查看${s.label}`" @click="s.to && $router.push(s.to)" @keydown.enter.prevent="s.to && $router.push(s.to)" @keydown.space.prevent="s.to && $router.push(s.to)">
          <div class="stat-icon" :style="{ background: s.bg, color: s.fg }" aria-hidden="true">
            <el-icon :size="22"><component :is="s.icon" /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-num">
              <span v-if="loading" class="skeleton-num"></span>
              <template v-else>{{ s.value }}</template>
            </div>
            <div class="stat-label">{{ s.label }}</div>
          </div>
          <el-icon class="stat-arrow"><ArrowRight /></el-icon>
        </div>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="mt-4">
      <el-col :xs="24" :md="14">
        <div class="card">
          <div class="card-title">
            <el-icon><OfficeBuilding /></el-icon> 业务场景
            <el-button class="card-more" size="small" text type="primary" @click="$router.push('/scenarios')">全部</el-button>
          </div>
          <el-table v-loading="loading" :data="scenarios" size="small" @row-click="(r:any)=>$router.push('/scenarios/'+r.id)" style="cursor:pointer">
            <el-table-column prop="name" label="名称" min-width="140">
              <template #default="{ row }">
                <div class="cell-main">{{ row.name }}</div>
                <div class="cell-sub">{{ row.industry || '通用' }}</div>
              </template>
            </el-table-column>
            <el-table-column prop="entity_count" label="实体" width="70" align="center" />
            <el-table-column prop="relation_count" label="关系" width="70" align="center" />
            <el-table-column prop="data_source_count" label="数据源" width="80" align="center" />
            <el-table-column label="" width="50" align="center">
              <template #default>
                <el-icon class="row-arrow"><ArrowRight /></el-icon>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="!loading && !scenarios.length" class="empty-wrap">
            <div class="empty-icon"><el-icon :size="28"><OfficeBuilding /></el-icon></div>
            <div>暂无业务场景</div>
            <el-button type="primary" size="small" @click="$router.push('/scenarios')">去创建</el-button>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :md="10">
        <div class="card">
          <div class="card-title">
            <el-icon><Cpu /></el-icon> Agent
            <el-button class="card-more" size="small" text type="primary" @click="$router.push('/agents')">全部</el-button>
          </div>
          <el-table v-loading="loading" :data="agents" size="small" @row-click="(r:any)=>$router.push('/agents/'+r.id+'/chat')" style="cursor:pointer">
            <el-table-column prop="name" label="名称" min-width="120">
              <template #default="{ row }">
                <div class="cell-main">{{ row.name }}</div>
                <div class="cell-sub">{{ row.scenario_name || '未绑定场景' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="能力" min-width="150">
              <template #default="{ row }">
                <div class="cap-tags">
                  <el-tag size="small" type="success" effect="light" v-for="n in (row.skill_names||[]).slice(0,2)" :key="n">{{ n }}</el-tag>
                  <el-tag size="small" type="warning" effect="light" v-for="n in (row.mcp_names||[]).slice(0,2)" :key="n">{{ n }}</el-tag>
                  <span class="muted" v-if="!(row.skill_names?.length || row.mcp_names?.length)">未配置</span>
                </div>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="!loading && !agents.length" class="empty-wrap">
            <div class="empty-icon"><el-icon :size="28"><Cpu /></el-icon></div>
            <div>暂无 Agent</div>
            <el-button type="primary" size="small" @click="$router.push('/agents')">去创建</el-button>
          </div>
        </div>
      </el-col>
    </el-row>

    <div class="card mt-4">
      <div class="card-title"><el-icon><Guide /></el-icon> 平台使用流程</div>
      <el-steps :active="4" align-center finish-status="success" class="flow-steps">
        <el-step title="定义业务场景" description="创建场景，描述业务领域" />
        <el-step title="本体建模" description="定义实体、属性、关系" />
        <el-step title="接入数据源" description="数据库 / 文件桶（Excel、PDF、图片…）" />
        <el-step title="创建 Agent" description="绑定场景 + LLM + 技能 + MCP" />
        <el-step title="AI 对话" description="Agent 自主查询数据、调用技能完成任务" />
      </el-steps>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type { Scenario, Agent } from '@/types'

const scenarios = ref<Scenario[]>([])
const agents = ref<Agent[]>([])
const totalDS = ref(0)
const skills = ref<any[]>([])
const loading = ref(false)

const stats = computed(() => [
  { label: '业务场景', value: scenarios.value.length, icon: 'OfficeBuilding', bg: 'var(--primary-soft)', fg: 'var(--primary-600)', to: '/scenarios' },
  { label: 'Agent', value: agents.value.length, icon: 'Cpu', bg: 'var(--success-soft)', fg: 'var(--success)', to: '/agents' },
  { label: '数据源', value: totalDS.value, icon: 'Coin', bg: 'var(--warning-soft)', fg: 'var(--warning)', to: '/data-sources' },
  { label: '技能', value: skills.value.length, icon: 'MagicStick', bg: 'var(--info-soft)', fg: 'var(--info)', to: '/skills' },
])

async function load() {
  loading.value = true
  try {
    const [sc, ag, ds, sk] = await Promise.all([
      api.listScenarios(),
      api.listAgents(),
      api.listDataSources(),
      api.listSkills(),
    ])
    scenarios.value = sc
    agents.value = ag
    totalDS.value = ds.length
    skills.value = sk
  } catch (e: any) {
    ElMessage.error('加载失败：' + e.message)
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>

<style scoped>
.mt-4 { margin-top: 16px; }

.stat-card {
  display: flex;
  align-items: center;
  gap: 14px;
  cursor: pointer;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  padding: 18px 20px;
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease);
  position: relative;
  overflow: hidden;
}
.stat-card::before {
  content: '';
  position: absolute;
  inset: 0;
  background: var(--grad-soft);
  opacity: 0;
  transition: opacity var(--dur) var(--ease);
}
.stat-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}
.stat-card:hover::before { opacity: 1; }
.stat-card > * { position: relative; }

.stat-icon {
  width: 50px; height: 50px;
  border-radius: 13px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.stat-body { flex: 1; min-width: 0; }
.stat-num {
  font-size: 28px;
  font-weight: 800;
  line-height: 1.1;
  letter-spacing: -0.5px;
  color: var(--text);
}
.skeleton-num {
  display: inline-block;
  width: 44px; height: 26px;
  border-radius: 6px;
  background: linear-gradient(90deg, var(--surface-3) 25%, var(--border) 50%, var(--surface-3) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.4s infinite;
}
@keyframes shimmer {
  from { background-position: 200% 0; }
  to { background-position: -200% 0; }
}
.stat-label { color: var(--text-2); font-size: 13px; font-weight: 600; margin-top: 2px; white-space: nowrap; }
.stat-arrow {
  color: var(--text-3);
  transition: transform var(--dur) var(--ease), color var(--dur);
}
.stat-card:hover .stat-arrow {
  transform: translateX(3px);
  color: var(--primary);
}
.stat-card:focus-visible { outline: 3px solid color-mix(in srgb, var(--primary) 42%, transparent); outline-offset: 3px; }

@media (max-width: 768px) {
  .stat-card { padding: 14px 14px; gap: 10px; }
  .stat-icon { width: 40px; height: 40px; border-radius: 11px; }
  .stat-num { font-size: 22px; }
  .stat-label { font-size: 12px; }
  .stat-arrow { display: none; }
}

.card-more {
  margin-left: auto;
}
.cell-main { font-weight: 700; color: var(--text); }
.cell-sub { font-size: 12px; color: var(--text-3); margin-top: 1px; }
.row-arrow { color: var(--text-3); }
.cap-tags { display: flex; flex-wrap: wrap; gap: 4px; }

.flow-steps {
  padding: 12px 0 4px;
}
</style>
