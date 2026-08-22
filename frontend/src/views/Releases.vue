<template>
  <main class="release-page" aria-labelledby="release-page-title">
    <header class="release-header">
      <div>
        <span class="eyebrow">RELEASE GOVERNANCE</span>
        <h1 id="release-page-title">发布治理</h1>
        <p>在同一条可审计路径中管理分支、变更提案、评审、环境发布与可确认回滚。</p>
      </div>
      <div class="release-header-actions">
        <el-select
          v-model="scenarioId"
          class="scenario-select"
          aria-label="选择业务场景"
          :disabled="loading || !scenarios.length"
          placeholder="选择业务场景"
          @change="selectScenario"
        >
          <el-option v-for="scenario in scenarios" :key="scenario.id" :label="scenario.name" :value="scenario.id" />
        </el-select>
        <el-button :loading="loading" :disabled="!scenarioId" @click="loadReleaseData()">
          <el-icon aria-hidden="true"><Refresh /></el-icon>刷新
        </el-button>
        <el-button :disabled="!scenarioId" @click="openExportDialog">
          <el-icon aria-hidden="true"><Download /></el-icon>安全导出
        </el-button>
        <el-button :disabled="!scenarioId" @click="openImportPicker">
          <el-icon aria-hidden="true"><Upload /></el-icon>导入预检
        </el-button>
        <el-button type="primary" :disabled="!scenarioId" @click="openBranchDialog">
          <el-icon aria-hidden="true"><Plus /></el-icon>新建发布分支
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="error"
      class="release-alert"
      type="error"
      :title="error"
      show-icon
      closable
      role="alert"
      @close="error = ''"
    />
    <p v-if="feedback" class="release-feedback" role="status" aria-live="polite">
      <el-icon aria-hidden="true"><InfoFilled /></el-icon>{{ feedback }}
    </p>

    <section v-if="!loading && !scenarios.length" class="empty-card card" aria-labelledby="scenario-empty-title">
      <el-icon aria-hidden="true" :size="30"><OfficeBuilding /></el-icon>
      <h3 id="scenario-empty-title">暂无可访问的业务场景</h3>
      <p>发布分支属于业务场景。创建或获得一个场景的访问权限后，即可在这里管理发布。</p>
    </section>

    <template v-else-if="scenarioId">
      <section v-loading="loading" class="branch-card card" aria-labelledby="branch-title">
        <template v-if="selectedBranch">
          <div class="branch-heading">
            <div class="branch-icon" aria-hidden="true"><el-icon><Share /></el-icon></div>
            <div>
              <span class="eyebrow">ACTIVE RELEASE BRANCH</span>
              <h3 id="branch-title">{{ selectedBranch.name }}</h3>
              <p>{{ selectedBranch.description || '该发布分支尚未填写说明。' }}</p>
            </div>
          </div>
          <dl class="branch-facts">
            <div><dt>状态</dt><dd>{{ branchStatus(selectedBranch.status).label }}</dd></div>
            <div><dt>基线快照</dt><dd><code>{{ shortId(selectedBranch.base_snapshot_id) || '尚未生成' }}</code></dd></div>
            <div><dt>当前快照</dt><dd><code>{{ shortId(selectedBranch.head_snapshot_id) || '尚未生成' }}</code></dd></div>
            <div><dt>最后更新</dt><dd>{{ formatDate(selectedBranch.updated_at || selectedBranch.created_at) || '—' }}</dd></div>
          </dl>
          <div class="branch-gate" :class="`gate--${branchStatus(selectedBranch.status).tone}`">
            <el-icon aria-hidden="true"><component :is="branchStatus(selectedBranch.status).icon" /></el-icon>
            <span><b>{{ branchStatus(selectedBranch.status).label }}</b>{{ branchGateMessage(selectedBranch) }}</span>
          </div>
        </template>
        <div v-else class="branch-empty">
          <el-icon aria-hidden="true" :size="28"><Share /></el-icon>
          <div>
            <h3 id="branch-title">尚未创建发布分支</h3>
            <p>先创建一个分支，系统才能把提案和可发布快照关联到该场景。</p>
          </div>
          <el-button type="primary" @click="openBranchDialog"><el-icon aria-hidden="true"><Plus /></el-icon>创建分支</el-button>
        </div>
      </section>

      <section class="governance-summary" aria-label="发布治理概览" aria-live="polite">
        <article class="summary-card summary-card--review">
          <span class="summary-icon" aria-hidden="true"><el-icon><UserFilled /></el-icon></span>
          <span><b>{{ reviewPendingCount }}</b><small>待评审提案</small></span>
          <p>已提交、尚未完成合入的变更</p>
        </article>
        <article class="summary-card summary-card--risk">
          <span class="summary-icon" aria-hidden="true"><el-icon><WarningFilled /></el-icon></span>
          <span><b>{{ rejectedCount }}</b><small>被拒绝提案</small></span>
          <p>需要修订后重新提交</p>
        </article>
        <article class="summary-card summary-card--ready">
          <span class="summary-icon" aria-hidden="true"><el-icon><CircleCheckFilled /></el-icon></span>
          <span><b>{{ releasedEnvironmentCount }}/{{ ENVIRONMENTS.length }}</b><small>环境有发布记录</small></span>
          <p>按最近发布记录统计</p>
        </article>
        <article class="summary-card summary-card--rollback">
          <span class="summary-icon" aria-hidden="true"><el-icon><RefreshLeft /></el-icon></span>
          <span><b>{{ rollbackCandidateCount }}</b><small>可选回滚快照</small></span>
          <p>仅展示服务端已经记录的发布版本</p>
        </article>
      </section>

      <section class="release-layout">
        <section class="proposal-panel card" aria-labelledby="proposal-title" v-loading="loading">
          <header class="section-head">
            <div>
              <span class="eyebrow">CHANGE PROPOSALS</span>
              <h3 id="proposal-title">变更提案与评审</h3>
            </div>
            <div class="section-actions">
              <el-select v-model="selectedBranchId" class="branch-select" aria-label="按发布分支筛选提案" :disabled="!branches.length">
                <el-option v-for="branch in branches" :key="branch.id" :label="branch.name" :value="branch.id" />
              </el-select>
              <el-button type="primary" plain :loading="actionLoading === 'snapshot'" :disabled="!selectedBranch || Boolean(actionLoading)" @click="openProposalDialog">
                <el-icon aria-hidden="true"><Plus /></el-icon>新建提案
              </el-button>
            </div>
          </header>

          <div v-if="!visibleProposals.length && !loading" class="inline-empty">
            <el-icon aria-hidden="true"><DocumentAdd /></el-icon>
            <strong>{{ selectedBranch ? '此分支暂无变更提案' : '选择或创建分支后可创建提案' }}</strong>
            <span>{{ selectedBranch ? '提案可保存为草稿，也可提交给评审人。' : '发布治理不会跨分支混合提案。' }}</span>
          </div>
          <ol v-else class="proposal-list" aria-label="发布提案列表">
            <li v-for="proposal in visibleProposals" :key="proposal.id">
              <article class="proposal-card" :class="{ selected: selectedProposalId === proposal.id }">
                <header class="proposal-heading">
                  <div class="proposal-title-wrap">
                    <span class="proposal-id mono">{{ shortId(proposal.id) }}</span>
                    <h4>{{ proposal.title }}</h4>
                  </div>
                  <span class="state-pill" :class="`state-pill--${proposalStatus(proposal.status).tone}`">
                    <el-icon aria-hidden="true"><component :is="proposalStatus(proposal.status).icon" /></el-icon>{{ proposalStatus(proposal.status).label }}
                  </span>
                </header>
                <p class="proposal-description">{{ proposal.description || '该提案未填写变更说明。' }}</p>
                <dl class="proposal-meta">
                  <div><dt>创建人</dt><dd>{{ userLabel(proposal.created_by_user_id) }}</dd></div>
                  <div><dt>基线快照</dt><dd><code>{{ shortId(proposal.base_snapshot_id) }}</code></dd></div>
                  <div><dt>目标快照</dt><dd><code>{{ shortId(proposal.proposed_snapshot_id) }}</code></dd></div>
                  <div><dt>更新时间</dt><dd>{{ formatDate(proposal.updated_at || proposal.created_at) || '—' }}</dd></div>
                </dl>
                <div class="review-row">
                  <div>
                    <b>评审记录</b>
                    <p>{{ reviewSummary(proposal) }}</p>
                  </div>
                  <ul v-if="proposal.reviews?.length" class="reviewer-list" :aria-label="`${proposal.title} 的评审记录`">
                    <li v-for="review in proposal.reviews" :key="review.id" :class="`reviewer--${review.decision === 'approve' ? 'approved' : 'changes_requested'}`">
                      <span class="reviewer-avatar" aria-hidden="true">{{ initials(review.reviewer_user_id) }}</span>
                      <span>{{ userLabel(review.reviewer_user_id) }}</span>
                      <small>{{ reviewDecisionLabel(review.decision) }}</small>
                    </li>
                  </ul>
                  <span v-else class="review-empty">尚无评审记录</span>
                </div>
                <div class="proposal-actions">
                  <el-button text type="primary" :aria-label="`查看 ${proposal.title} 的提交快照`" @click="openCompare(proposal)">
                    <el-icon aria-hidden="true"><DocumentCopy /></el-icon>查看快照
                  </el-button>
                  <el-button text :aria-pressed="selectedProposalId === proposal.id" @click="selectProposal(proposal.id)">
                    <el-icon aria-hidden="true"><View /></el-icon>{{ selectedProposalId === proposal.id ? '当前提案' : '设为当前提案' }}
                  </el-button>
                  <el-button v-if="canSubmitProposal(proposal)" text type="primary" :loading="actionLoading === 'submit-proposal'" @click="submitReleaseProposal(proposal)">
                    <el-icon aria-hidden="true"><Upload /></el-icon>送审
                  </el-button>
                  <el-button v-if="canReview(proposal)" text type="warning" @click="openReviewDialog(proposal)">
                    <el-icon aria-hidden="true"><UserFilled /></el-icon>填写评审结论
                  </el-button>
                  <el-button v-if="proposal.status === 'approved'" text type="success" @click="openMergeDialog(proposal)">
                    <el-icon aria-hidden="true"><CircleCheckFilled /></el-icon>确认合入
                  </el-button>
                </div>
              </article>
            </li>
          </ol>
        </section>

        <aside class="policy-panel card" aria-labelledby="policy-title">
          <header class="section-head">
            <div>
              <span class="eyebrow">RELEASE GATES</span>
              <h3 id="policy-title">发布门禁</h3>
            </div>
            <p>真实策略由服务端执行；界面只说明当前可见记录和需要确认的操作。</p>
          </header>
          <ul class="policy-list">
            <li v-for="gate in releaseGates" :key="gate.id" :class="`policy--${gate.tone}`">
              <span class="policy-icon" aria-hidden="true"><el-icon><component :is="gate.icon" /></el-icon></span>
              <div>
                <h4>{{ gate.title }}</h4>
                <p>{{ gate.description }}</p>
                <small>{{ gate.detail }}</small>
              </div>
            </li>
          </ul>
          <div class="policy-foot">
            <el-icon aria-hidden="true"><Lock /></el-icon>
            <p>提交评审、合入、发布与回滚都要由后端重新检查访问控制和不变量。</p>
          </div>
        </aside>
      </section>

      <section class="environment-panel card" aria-labelledby="environment-title" v-loading="loading">
        <header class="section-head environment-section-head">
          <div>
            <span class="eyebrow">ENVIRONMENT DELIVERY</span>
            <h3 id="environment-title">环境发布路径</h3>
          </div>
          <p>每次发布都会锁定该环境的本体定义快照；已投递任务会继续使用入队时的固定版本。</p>
        </header>
        <ol class="environment-list" aria-label="环境发布阶段">
          <li v-for="(environment, index) in ENVIRONMENTS" :key="environment.id" class="environment-stage" :class="`environment-stage--${environmentView(environment.id).tone}`">
            <div class="stage-index" aria-hidden="true">{{ index + 1 }}</div>
            <article class="environment-card">
              <header>
                <div>
                  <h4>{{ environment.name }}</h4>
                  <p>{{ environment.description }}</p>
                </div>
                <span class="state-pill" :class="`state-pill--${environmentView(environment.id).tone}`">
                  <el-icon aria-hidden="true"><component :is="environmentView(environment.id).icon" /></el-icon>{{ environmentView(environment.id).label }}
                </span>
              </header>
              <p class="environment-description">{{ environmentView(environment.id).description }}</p>
              <dl class="environment-meta">
                <div><dt>最近快照</dt><dd><code>{{ shortId(environmentView(environment.id).record?.snapshot_id) || '暂无发布' }}</code></dd></div>
                <div><dt>最近时间</dt><dd>{{ formatDate(environmentView(environment.id).record?.created_at) || '—' }}</dd></div>
                <div><dt>连接器审计</dt><dd>{{ environmentView(environment.id).record?.connector_audit?.length ? `${environmentView(environment.id).record?.connector_audit?.length} 项已验证` : '—' }}</dd></div>
              </dl>
              <p
                class="environment-connector-gate"
                :class="`environment-connector-gate--${connectorGate(environment.id).tone}`"
                :data-testid="`release-connector-gate-${environment.id}`"
              >
                <el-icon aria-hidden="true"><component :is="connectorGate(environment.id).icon" /></el-icon>
                <span><b>{{ connectorGate(environment.id).label }}</b>{{ connectorGate(environment.id).description }}</span>
              </p>
              <div v-if="rollbackCandidates(environment.id).length" class="rollback-target-picker">
                <label :for="`rollback-target-${environment.id}`">回滚目标版本</label>
                <el-select
                  :id="`rollback-target-${environment.id}`"
                  v-model="rollbackSelections[environment.id]"
                  size="small"
                  :aria-label="`选择 ${environment.name} 的回滚目标版本`"
                >
                  <el-option
                    v-for="record in rollbackCandidates(environment.id)"
                    :key="record.id"
                    :value="record.id"
                    :label="rollbackRecordLabel(record)"
                  />
                </el-select>
              </div>
              <div class="environment-actions">
                <button type="button" class="environment-button" @click="showEnvironmentPreview(environment.id)">
                  <el-icon aria-hidden="true"><View /></el-icon>查看发布条件
                </button>
                <button
                  type="button"
                  class="environment-button environment-button--primary"
                  :disabled="!canPublishEnvironment(environment.id)"
                  :data-testid="`release-publish-${environment.id}`"
                  :aria-label="`打开 ${environment.name} 的发布确认`"
                  @click="openPublishDialog(environment.id)"
                >
                  <el-icon aria-hidden="true"><UploadFilled /></el-icon>发布确认
                </button>
                <button
                  type="button"
                  class="environment-button"
                  :data-testid="`release-connectors-${environment.id}`"
                  :aria-label="`配置 ${environment.name} 的连接器与环境绑定`"
                  @click="openConnectorSettings(environment.id)"
                >
                  <el-icon aria-hidden="true"><Connection /></el-icon>连接器
                </button>
                <button
                  v-if="rollbackCandidate(environment.id)"
                  type="button"
                  class="environment-button environment-button--danger"
                  :aria-label="`打开 ${environment.name} 的所选回滚版本确认`"
                  @click="openRollback(rollbackCandidate(environment.id)!)"
                >
                  <el-icon aria-hidden="true"><RefreshLeft /></el-icon>回滚所选版本
                </button>
              </div>
            </article>
          </li>
        </ol>
      </section>

      <section class="package-panel card" aria-labelledby="package-title">
        <header class="section-head">
          <div>
            <span class="eyebrow">PORTABLE RESOURCE PACKAGE</span>
            <h3 id="package-title">安全资源包交换</h3>
          </div>
          <p>支持已脱敏定义的导出、目标场景导入预检与受治理提案；此页不能直接应用资源包。</p>
        </header>
        <div class="package-body">
          <div>
            <h4><el-icon aria-hidden="true"><Download /></el-icon>导出已脱敏定义</h4>
            <p>导出包不包含凭据、运行记录、外部连接配置或来源标识；服务端仍会核验当前场景访问权限。</p>
          </div>
          <div>
            <h4><el-icon aria-hidden="true"><Upload /></el-icon>导入前只做预检</h4>
            <p>上传 JSON 后只返回格式、差异、冲突与待绑定项。任何变更必须转入发布提案与评审链路。</p>
          </div>
          <div class="starter-kit-card">
            <h4><el-icon aria-hidden="true"><DocumentChecked /></el-icon>从行业 Starter Kit 开始</h4>
            <p>由服务端加载并复算指纹的零售、财税或供应链声明式基础包；同样只能先预览，再创建提案。</p>
            <el-select
              v-model="selectedStarterKitId"
              class="starter-kit-select"
              :loading="starterKitsLoading"
              :disabled="starterKitsLoading || !starterKits.length"
              aria-label="选择行业 Starter Kit"
              placeholder="选择行业 Starter Kit"
            >
              <el-option
                v-for="kit in starterKits"
                :key="kit.id"
                :label="`${kit.name} · ${kit.industry} · v${kit.version}`"
                :value="kit.id"
              />
            </el-select>
            <p v-if="selectedStarterKit" class="starter-kit-summary">
              {{ selectedStarterKit.description || `${selectedStarterKit.industry}行业声明式本体基础包。` }}
            </p>
          </div>
        </div>
        <div class="package-actions">
          <el-button :disabled="!scenarioId" @click="openExportDialog"><el-icon aria-hidden="true"><Download /></el-icon>导出资源包</el-button>
          <el-select v-model="packageImportEnvironment" class="package-environment-select" aria-label="选择资源包导入目标环境">
            <el-option v-for="environment in ENVIRONMENTS" :key="environment.id" :label="`${environment.name}（${environment.id}）`" :value="environment.id" />
          </el-select>
          <el-button type="primary" plain :disabled="!scenarioId" @click="openImportPicker"><el-icon aria-hidden="true"><Upload /></el-icon>选择 JSON 并预检</el-button>
          <el-button
            type="primary"
            :loading="actionLoading === 'starter-kit-preview'"
            :disabled="!scenarioId || !selectedStarterKitId || starterKitsLoading"
            @click="previewStarterKitImport"
          ><el-icon aria-hidden="true"><DocumentChecked /></el-icon>预览 Starter Kit</el-button>
        </div>
      </section>
    </template>

    <input ref="packageFileInput" class="sr-only" type="file" accept="application/json,.json" @change="readPackageFile">

    <el-dialog v-model="branchDialogVisible" title="新建发布分支" width="min(540px, calc(100vw - 32px))" :close-on-click-modal="false" destroy-on-close>
      <el-form label-position="top" @submit.prevent="createBranch">
        <el-form-item label="分支名称" required>
          <el-input v-model.trim="branchForm.name" maxlength="160" show-word-limit placeholder="例如 release/2026.08" aria-describedby="branch-name-help" />
          <p id="branch-name-help" class="field-help">名称在当前场景的发布分支范围内应保持可辨识。</p>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model.trim="branchForm.description" type="textarea" :rows="3" maxlength="1000" show-word-limit placeholder="说明本次发布的范围、窗口或约束" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="actionLoading === 'branch'" @click="branchDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="actionLoading === 'branch'" :disabled="!branchForm.name" @click="createBranch">创建分支</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="proposalDialogVisible" title="新建变更提案" width="min(680px, calc(100vw - 32px))" :close-on-click-modal="false" destroy-on-close>
      <el-form label-position="top" @submit.prevent="createProposal">
        <el-form-item label="提案标题" required><el-input v-model.trim="proposalForm.title" maxlength="200" show-word-limit placeholder="说明待评审的变更" /></el-form-item>
        <el-form-item label="变更说明"><el-input v-model.trim="proposalForm.description" type="textarea" :rows="3" maxlength="2000" show-word-limit placeholder="说明影响范围、验证方式和回滚考虑" /></el-form-item>
        <el-form-item label="脱敏变更内容（JSON）" required>
          <el-input v-model="proposalForm.contentText" class="json-input" type="textarea" :rows="7" spellcheck="false" aria-describedby="proposal-content-help" />
          <p id="proposal-content-help" class="field-help">已从当前分支快照 <code>{{ shortId(proposalBaseSnapshotId) }}</code> 载入。将作为服务端定义快照的一部分保存；请勿输入密钥、口令或未脱敏业务数据。</p>
        </el-form-item>
        <el-checkbox v-model="proposalForm.submit">创建后立即提交评审</el-checkbox>
      </el-form>
      <template #footer>
        <el-button :disabled="actionLoading === 'proposal'" @click="proposalDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="actionLoading === 'proposal'" :disabled="!proposalForm.title || !proposalForm.contentText.trim()" @click="createProposal">保存提案</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="reviewDialogVisible" :title="reviewTarget ? `评审：${reviewTarget.title}` : '提交评审'" width="min(540px, calc(100vw - 32px))" :close-on-click-modal="false" destroy-on-close>
      <section v-if="reviewTarget" class="review-dialog" aria-describedby="review-description">
        <p id="review-description">评审决定会写入该提案的审计记录，后端仍将核验当前用户的角色与权限。</p>
        <el-form label-position="top" @submit.prevent="submitReview">
          <el-form-item label="评审决定" required>
            <el-radio-group v-model="reviewForm.decision" aria-label="选择评审决定">
              <el-radio value="approve">批准</el-radio>
              <el-radio value="reject">拒绝并要求修改</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="评审意见"><el-input v-model.trim="reviewForm.comment" type="textarea" :rows="4" maxlength="2000" show-word-limit placeholder="记录理由、风险或需要补充的证据" /></el-form-item>
        </el-form>
      </section>
      <template #footer>
        <el-button :disabled="actionLoading === 'review'" @click="reviewDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="actionLoading === 'review'" @click="submitReview">提交评审</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="mergeDialogVisible" :title="mergeTarget ? `确认合入：${mergeTarget.title}` : '确认合入'" width="min(590px, calc(100vw - 32px))" :close-on-click-modal="false" destroy-on-close @closed="mergeAcknowledged = false">
      <section v-if="mergeTarget" class="confirmation-dialog" aria-labelledby="merge-title" aria-describedby="merge-description">
        <span class="eyebrow">MERGE CONFIRMATION</span>
        <h3 id="merge-title">合入已批准的提案</h3>
        <p id="merge-description">合入会生成或推进可发布快照。系统会在服务端再次检查提案状态、分支归属和操作者权限。</p>
        <dl class="confirmation-facts"><div><dt>提案</dt><dd>{{ mergeTarget.title }}</dd></div><div><dt>目标快照</dt><dd><code>{{ shortId(mergeTarget.proposed_snapshot_id) }}</code></dd></div></dl>
        <el-input v-model.trim="mergeNote" type="textarea" :rows="3" maxlength="1000" show-word-limit placeholder="可选：记录合入说明" />
        <el-checkbox v-model="mergeAcknowledged" class="confirmation-check">我已确认该提案处于批准状态，并同意请求服务端执行合入。</el-checkbox>
      </section>
      <template #footer><el-button @click="mergeDialogVisible = false">取消</el-button><el-button type="primary" :disabled="!mergeAcknowledged" :loading="actionLoading === 'merge'" @click="mergeProposal">确认合入</el-button></template>
    </el-dialog>

    <el-dialog v-model="compareVisible" :title="compareTarget ? `提交快照：${compareTarget.title}` : '提交快照'" width="min(760px, calc(100vw - 32px))" destroy-on-close>
      <section v-if="compareTarget" class="compare-dialog" aria-labelledby="compare-title" aria-describedby="compare-description">
        <span class="eyebrow">CHANGE SNAPSHOT</span>
        <h3 id="compare-title">{{ compareTarget.title }}</h3>
        <p id="compare-description">这里展示服务端返回的脱敏提案快照摘要。基线与逐项差异可由后续比较接口补充。</p>
        <dl class="compare-facts"><div><dt>基线快照</dt><dd><code>{{ shortId(compareTarget.base_snapshot_id) }}</code></dd></div><div><dt>提议快照</dt><dd><code>{{ shortId(compareTarget.proposed_snapshot_id) }}</code></dd></div><div><dt>状态</dt><dd>{{ proposalStatus(compareTarget.status).label }}</dd></div></dl>
        <div class="diff-table-wrap" tabindex="0" aria-label="脱敏变更内容摘要，可横向滚动">
          <table class="diff-table">
            <caption class="sr-only">{{ compareTarget.title }} 的脱敏变更内容摘要</caption>
            <thead><tr><th scope="col">字段</th><th scope="col">值摘要</th></tr></thead>
            <tbody><tr v-for="item in safeContentEntries(compareTarget.content)" :key="item.key"><th scope="row">{{ item.key }}</th><td>{{ item.value }}</td></tr></tbody>
          </table>
        </div>
      </section>
      <template #footer><el-button @click="compareVisible = false">关闭</el-button></template>
    </el-dialog>

    <el-dialog v-model="publishDialogVisible" :title="`确认发布到 ${environmentName(publishForm.environment)}`" width="min(620px, calc(100vw - 32px))" :close-on-click-modal="false" destroy-on-close @closed="publishAcknowledged = false">
      <section class="confirmation-dialog" aria-labelledby="publish-title" aria-describedby="publish-description">
        <span class="eyebrow">PUBLISH CONFIRMATION</span>
        <h3 id="publish-title">请求环境发布</h3>
        <p id="publish-description">发布会将该环境的运行定义锁定到当前快照；服务端仍将确认分支、快照、发布策略和当前用户权限。</p>
        <dl class="confirmation-facts"><div><dt>环境</dt><dd>{{ environmentName(publishForm.environment) }}</dd></div><div><dt>分支</dt><dd>{{ selectedBranch?.name || '—' }}</dd></div><div><dt>快照</dt><dd><code>{{ shortId(publishSnapshotId) || '不可用' }}</code></dd></div></dl>
        <el-alert
          :type="connectorGate(publishForm.environment).ready ? 'success' : 'warning'"
          :title="connectorGate(publishForm.environment).label"
          :description="connectorGate(publishForm.environment).description"
          :closable="false"
          show-icon
        />
        <el-input v-model.trim="publishForm.notes" type="textarea" :rows="3" maxlength="1000" show-word-limit placeholder="记录发布说明或关联变更单" />
        <el-checkbox v-model="publishAcknowledged" class="confirmation-check">我已核对环境、分支与快照，并同意请求服务端执行发布。</el-checkbox>
      </section>
      <template #footer><el-button @click="publishDialogVisible = false">取消</el-button><el-button type="primary" :disabled="!publishAcknowledged || !canPublishEnvironment(publishForm.environment)" :loading="actionLoading === 'publish'" @click="publishRelease">确认发布</el-button></template>
    </el-dialog>

    <el-dialog v-model="rollbackVisible" :title="rollbackTarget ? `确认回滚：${environmentName(rollbackTarget.environment)}` : '确认回滚'" width="min(620px, calc(100vw - 32px))" :close-on-click-modal="false" destroy-on-close @closed="rollbackAcknowledged = false">
      <section v-if="rollbackTarget" class="confirmation-dialog" aria-labelledby="rollback-title" aria-describedby="rollback-description">
        <span class="eyebrow">ROLLBACK CONFIRMATION</span>
        <h3 id="rollback-title">将环境恢复到已记录快照</h3>
        <p id="rollback-description">目标是历史发布记录中的快照。预发/生产环境回滚只切换该环境的固定版本，不会改动开发中的本体定义；服务端仍会确认权限、环境和快照可用性。</p>
        <el-alert type="warning" :closable="false" show-icon><template #title>回滚可能影响正在使用此环境的业务流程。</template><template #default>请记录原因，并在执行前核对目标环境与快照。</template></el-alert>
        <dl class="confirmation-facts"><div><dt>目标环境</dt><dd>{{ environmentName(rollbackTarget.environment) }}</dd></div><div><dt>目标快照</dt><dd><code>{{ shortId(rollbackTarget.snapshot_id) }}</code></dd></div><div><dt>记录时间</dt><dd>{{ formatDate(rollbackTarget.created_at) || '—' }}</dd></div></dl>
        <el-input v-model.trim="rollbackReason" type="textarea" :rows="3" maxlength="1000" show-word-limit placeholder="说明回滚原因" />
        <el-checkbox v-model="rollbackAcknowledged" class="confirmation-check">我已核对环境、历史快照和影响范围，并同意请求服务端执行回滚。</el-checkbox>
      </section>
      <template #footer><el-button @click="rollbackVisible = false">取消</el-button><el-button type="danger" :disabled="!rollbackAcknowledged" :loading="actionLoading === 'rollback'" @click="rollbackRelease">确认回滚</el-button></template>
    </el-dialog>

    <el-dialog v-model="exportDialogVisible" title="确认安全导出" width="min(580px, calc(100vw - 32px))" :close-on-click-modal="false" destroy-on-close @closed="exportAcknowledged = false">
      <section class="confirmation-dialog" aria-labelledby="export-title" aria-describedby="export-description">
        <span class="eyebrow">SAFE PACKAGE EXPORT</span>
        <h3 id="export-title">导出场景定义资源包</h3>
        <p id="export-description">导出内容由服务端去敏，不包含凭据、运行记录、外部连接配置或来源标识。请仅在授权范围内保存和共享导出的 JSON 文件。</p>
        <dl class="confirmation-facts"><div><dt>业务场景</dt><dd>{{ selectedScenario?.name || scenarioId }}</dd></div><div><dt>导出范围</dt><dd>声明式本体资源与依赖</dd></div></dl>
        <el-checkbox v-model="exportAcknowledged" class="confirmation-check">我确认该资源包只会在获授权的范围内使用和共享。</el-checkbox>
      </section>
      <template #footer><el-button @click="exportDialogVisible = false">取消</el-button><el-button type="primary" :disabled="!exportAcknowledged" :loading="actionLoading === 'export'" @click="exportPackage">导出 JSON</el-button></template>
    </el-dialog>

    <el-dialog v-model="packagePreviewVisible" title="资源包导入预检" width="min(820px, calc(100vw - 32px))" destroy-on-close>
      <section class="package-preview-dialog" aria-labelledby="package-preview-title" aria-describedby="package-preview-description">
        <span class="eyebrow">IMPORT PREVIEW · NO APPLY</span>
        <h3 id="package-preview-title">{{ packagePreview?.starter_kit ? `Starter Kit：${packagePreview.starter_kit.name}` : redactDisplayText(importFileName || '资源包') }}</h3>
        <p id="package-preview-description">预检不会修改目标场景。若需要应用变更，必须把已确认内容创建为受治理的发布提案。</p>
        <el-alert :type="packagePreview?.applicable ? 'success' : importErrors.length ? 'error' : 'warning'" :closable="false" show-icon>
          <template #title>{{ packagePreview?.applicable ? '资源包格式有效，且当前目标没有未解决冲突。' : importErrors.length ? '资源包格式校验未通过；不能进入导入预检。' : '资源包存在目标冲突或待绑定项；不能直接进入应用。' }}</template>
          <template #default>预检模式：{{ packagePreview?.proposal.mode || 'preview' }} · 写入目标：{{ packagePreview?.proposal.mutates_target ? '是' : '否' }}</template>
        </el-alert>
        <p class="field-help package-environment-note">当前预检目标环境：<b>{{ environmentName(packageImportEnvironment) }}</b>。完成环境绑定后请重新预检，服务端会再次校验健康状态。</p>
        <dl v-if="packagePreview" class="confirmation-facts package-preview-facts">
          <div><dt>资源包指纹</dt><dd><code class="package-fingerprint">{{ packagePreview.package_fingerprint || importFingerprint }}</code></dd></div>
          <div v-if="packagePreview.starter_kit"><dt>Starter Kit</dt><dd>{{ packagePreview.starter_kit.name }} · v{{ packagePreview.starter_kit.version }}</dd></div>
          <div><dt>目标场景</dt><dd>{{ packagePreview.proposal.target?.name || selectedScenario?.name || scenarioId }}</dd></div>
          <div><dt>创建</dt><dd>{{ packagePreview.proposal.summary.create || 0 }}</dd></div>
          <div><dt>更新</dt><dd>{{ packagePreview.proposal.summary.update || 0 }}</dd></div>
          <div><dt>冲突</dt><dd>{{ packagePreview.proposal.summary.conflict || 0 }}</dd></div>
          <div><dt>待绑定</dt><dd>{{ packagePreview.proposal.required_bindings.length }}</dd></div>
          <div><dt>已解析绑定</dt><dd>{{ packagePreview.proposal.resolved_bindings?.length || 0 }}</dd></div>
        </dl>
        <section v-if="packageIssues.length" class="package-issues" aria-labelledby="package-issues-title">
          <h4 id="package-issues-title">校验信息</h4>
          <ul>
            <li v-for="issue in packageIssues" :key="`${issue.code}-${issue.path}`">
              <b>{{ redactDisplayText(issue.code) }}</b><span>{{ redactDisplayText(issue.path || '资源包') }}：{{ redactDisplayText(issue.message) }}</span>
            </li>
          </ul>
        </section>
        <section v-if="packagePreview?.proposal.conflicts.length" class="package-issues package-issues--warning" aria-labelledby="package-conflicts-title">
          <h4 id="package-conflicts-title">目标冲突</h4>
          <ul><li v-for="conflict in packagePreview.proposal.conflicts" :key="JSON.stringify(conflict)">{{ compactRecord(conflict) }}</li></ul>
        </section>
        <section v-if="packagePreview?.proposal.required_bindings.length" class="package-issues package-issues--info" aria-labelledby="package-bindings-title">
          <h4 id="package-bindings-title">需要在提案中处理的外部绑定</h4>
          <ul>
            <li v-for="binding in packagePreview.proposal.required_bindings" :key="JSON.stringify(binding)" class="package-binding-row">
              <span>{{ compactRecord(binding) }}</span>
              <el-button v-if="canConfigureBinding(binding)" size="small" type="primary" plain @click="openConnectorBinding(binding)">配置环境绑定</el-button>
            </li>
          </ul>
        </section>
        <section v-if="packagePreview?.proposal.resolved_bindings?.length" class="package-issues package-issues--success" aria-labelledby="package-resolved-bindings-title">
          <h4 id="package-resolved-bindings-title">已解析的环境绑定</h4>
          <ul><li v-for="binding in packagePreview.proposal.resolved_bindings" :key="JSON.stringify(binding)">{{ compactRecord(binding) }}</li></ul>
        </section>
        <p v-if="packagePreview && !canCreatePackageImportProposal" class="field-help" role="status">{{ importProposalGateMessage }}</p>
      </section>
      <template #footer>
        <el-button @click="packagePreviewVisible = false">关闭预检</el-button>
        <el-button
          type="primary"
          :disabled="!canCreatePackageImportProposal"
          :title="importProposalGateMessage"
          @click="openPackageImportProposalDialog"
        >创建受治理提案</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="packageImportProposalVisible"
      title="确认创建资源包导入提案"
      width="min(620px, calc(100vw - 32px))"
      :close-on-click-modal="false"
      destroy-on-close
      @closed="resetPackageImportProposalForm"
    >
      <section class="confirmation-dialog" aria-labelledby="package-import-proposal-title" aria-describedby="package-import-proposal-description">
        <span class="eyebrow">GOVERNED PACKAGE PROPOSAL</span>
        <h3 id="package-import-proposal-title">先创建提案，再进入评审与合入</h3>
        <p id="package-import-proposal-description">此操作只创建与当前分支关联的受治理提案，不会直接应用资源包，也不会绕过评审、合入或发布门禁。</p>
        <dl class="confirmation-facts">
          <div><dt>目标场景</dt><dd>{{ selectedScenario?.name || scenarioId }}</dd></div>
          <div><dt>发布分支</dt><dd>{{ selectedBranch?.name || '—' }}</dd></div>
          <div><dt>目标环境</dt><dd>{{ environmentName(packageImportEnvironment) }}</dd></div>
          <div><dt>资源包指纹</dt><dd><code class="package-fingerprint">{{ packagePreview?.package_fingerprint || importFingerprint }}</code></dd></div>
          <div v-if="packagePreview?.starter_kit"><dt>Starter Kit</dt><dd>{{ packagePreview.starter_kit.name }} · v{{ packagePreview.starter_kit.version }}</dd></div>
          <div><dt>预检结果</dt><dd>可创建受治理提案</dd></div>
        </dl>
        <el-form label-position="top" @submit.prevent="createPackageImportProposal">
          <el-form-item label="提案标题" required>
            <el-input v-model.trim="packageImportProposalForm.title" maxlength="200" show-word-limit placeholder="说明本次资源包导入的业务目的" />
          </el-form-item>
          <el-form-item label="提案说明">
            <el-input v-model.trim="packageImportProposalForm.description" type="textarea" :rows="3" maxlength="2000" show-word-limit placeholder="说明影响范围、验证计划或后续绑定安排" />
          </el-form-item>
          <el-checkbox v-model="packageImportProposalForm.submit" class="confirmation-check">创建后立即提交评审</el-checkbox>
        </el-form>
        <el-checkbox v-model="packageImportProposalAcknowledged" class="confirmation-check">我确认当前预检结果仍适用；此操作仅创建提案，不直接写入业务场景或凭据。</el-checkbox>
      </section>
      <template #footer>
        <el-button :disabled="actionLoading === 'import-proposal'" @click="packageImportProposalVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="actionLoading === 'import-proposal'"
          :disabled="!canCreatePackageImportProposal || !packageImportProposalAcknowledged || !packageImportProposalForm.title.trim()"
          @click="createPackageImportProposal"
        >创建受治理提案</el-button>
      </template>
    </el-dialog>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type {
  OntologyResourcePackage,
  PackageIssue,
  PackageImportPreview,
  StarterKit,
  ConnectorReadiness,
  ReleaseBranch,
  ReleaseProposal,
  ReleaseRecord,
  Scenario,
} from '@/types'

type Tone = 'success' | 'warning' | 'danger' | 'info'
type EnvironmentId = 'dev' | 'staging' | 'prod'
type PackagePreviewContext = {
  kind: 'upload' | 'starter-kit'
  scenarioId: string
  environment: EnvironmentId
  starterKitId?: string
}

const ENVIRONMENTS: Array<{ id: EnvironmentId; name: string; description: string }> = [
  { id: 'dev', name: '开发环境', description: '用于集成验证与自动化测试。' },
  { id: 'staging', name: '预发布环境', description: '用于发布前验证和审批确认。' },
  { id: 'prod', name: '生产环境', description: '仅在所有发布门禁通过后提升。' },
]

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const scenarios = ref<Scenario[]>([])
const scenarioId = ref('')
const branches = ref<ReleaseBranch[]>([])
const proposals = ref<ReleaseProposal[]>([])
const records = ref<ReleaseRecord[]>([])
const selectedBranchId = ref('')
const selectedProposalId = ref('')
const loading = ref(false)
const actionLoading = ref('')
const error = ref('')
const feedback = ref('')
const connectorReadiness = ref<Partial<Record<EnvironmentId, ConnectorReadiness>>>({})
const connectorReadinessLoading = ref(false)
const connectorReadinessError = ref('')
let connectorReadinessRequest = 0
let viewDisposed = false
let scenarioLoadRequest = 0
let releaseDataRequest = 0
let starterKitRequest = 0

const branchDialogVisible = ref(false)
const proposalDialogVisible = ref(false)
const reviewDialogVisible = ref(false)
const mergeDialogVisible = ref(false)
const compareVisible = ref(false)
const publishDialogVisible = ref(false)
const rollbackVisible = ref(false)
const exportDialogVisible = ref(false)
const packagePreviewVisible = ref(false)
const packageImportProposalVisible = ref(false)
const reviewTarget = ref<ReleaseProposal | null>(null)
const mergeTarget = ref<ReleaseProposal | null>(null)
const compareTarget = ref<ReleaseProposal | null>(null)
const rollbackTarget = ref<ReleaseRecord | null>(null)
const mergeAcknowledged = ref(false)
const publishAcknowledged = ref(false)
const rollbackAcknowledged = ref(false)
const exportAcknowledged = ref(false)
const packageImportProposalAcknowledged = ref(false)
const mergeNote = ref('')
const rollbackReason = ref('')
const rollbackSelections = ref<Partial<Record<EnvironmentId, string>>>({})
const proposalBaseSnapshotId = ref('')
const packageFileInput = ref<HTMLInputElement>()
const importFileName = ref('')
const packagePreview = ref<PackageImportPreview | null>(null)
const sourceImportPackage = ref<Record<string, any> | null>(null)
const packagePreviewContext = ref<PackagePreviewContext | null>(null)
let packagePreviewRequest = 0
const importErrors = ref<PackageIssue[]>([])
const importWarnings = ref<PackageIssue[]>([])
const importFingerprint = ref('')
const packageImportEnvironment = ref<EnvironmentId>('dev')
const starterKits = ref<StarterKit[]>([])
const starterKitsLoading = ref(false)
const selectedStarterKitId = ref('')
const branchForm = ref({ name: '', description: '' })
const proposalForm = ref({ title: '', description: '', contentText: '', submit: true })
const packageImportProposalForm = ref({ title: '', description: '', submit: true })
const reviewForm = ref<{ decision: 'approve' | 'reject'; comment: string }>({ decision: 'approve', comment: '' })
const publishForm = ref<{ environment: EnvironmentId; notes: string }>({ environment: 'dev', notes: '' })

const selectedBranch = computed(() => branches.value.find((branch) => branch.id === selectedBranchId.value) || null)
const selectedScenario = computed(() => scenarios.value.find((scenario) => scenario.id === scenarioId.value) || null)
const selectedStarterKit = computed(() => starterKits.value.find((kit) => kit.id === selectedStarterKitId.value) || null)
const visibleProposals = computed(() => proposals.value.filter((proposal) => !selectedBranchId.value || proposal.branch_id === selectedBranchId.value))
const selectedProposal = computed(() => visibleProposals.value.find((proposal) => proposal.id === selectedProposalId.value) || null)
const reviewPendingCount = computed(() => visibleProposals.value.filter((proposal) => proposal.status === 'submitted').length)
const rejectedCount = computed(() => visibleProposals.value.filter((proposal) => proposal.status === 'rejected').length)
const latestRecords = computed(() => Object.fromEntries(ENVIRONMENTS.map((environment) => [environment.id, latestRecord(environment.id)])) as Record<EnvironmentId, ReleaseRecord | null>)
const releasedEnvironmentCount = computed(() => ENVIRONMENTS.filter((environment) => latestRecords.value[environment.id]?.status === 'released').length)
const rollbackCandidateCount = computed(() => new Set(records.value.map((record) => record.snapshot_id).filter(Boolean)).size)
const publishSnapshotId = computed(() => selectedBranch.value?.head_snapshot_id || selectedProposal.value?.merged_snapshot_id || selectedProposal.value?.proposed_snapshot_id || '')
const canPublish = computed(() => Boolean(selectedBranch.value && publishSnapshotId.value && !loading.value && !actionLoading.value))
const packageIssues = computed(() => [...importErrors.value, ...importWarnings.value])
const starterKitPreview = computed(() => packagePreview.value?.starter_kit || null)
const canManageImportProposals = computed(() => auth.user?.can_manage === true)
const previewContextMatchesCurrentTarget = computed(() => {
  const context = packagePreviewContext.value
  const preview = packagePreview.value
  if (!context || !preview || !isCurrentPackagePreviewContext(context)) return false
  if (preview.target_scenario_id !== context.scenarioId) return false
  if (String(preview.environment || 'dev') !== context.environment) return false
  return context.kind !== 'starter-kit' || preview.starter_kit?.id === context.starterKitId
})
const hasVerifiedSourcePackageFingerprint = computed(() => {
  const supplied = sourceImportPackage.value?.manifest?.fingerprint
  return typeof supplied === 'string'
    && supplied.trim().length > 0
    && supplied === (packagePreview.value?.package_fingerprint || importFingerprint.value)
})
const starterKitFingerprintMatchesPreview = computed(() => Boolean(
  starterKitPreview.value
  && starterKitPreview.value.fingerprint === (packagePreview.value?.package_fingerprint || importFingerprint.value),
))
const connectorGateSummary = computed(() => {
  const gates = ENVIRONMENTS.map((environment) => ({ environment, gate: connectorGate(environment.id) }))
  const ready = gates.filter((item) => item.gate.ready)
  if (ready.length === gates.length) {
    return { tone: 'success' as Tone, icon: 'CircleCheckFilled', detail: '开发、预发布和生产环境的连接器门禁均已通过。' }
  }
  if (ready.length) {
    const readyNames = ready.map((item) => item.environment.name).join('、')
    const pendingNames = gates.filter((item) => !item.gate.ready).map((item) => item.environment.name).join('、')
    return { tone: 'info' as Tone, icon: 'InfoFilled', detail: `${readyNames}已通过；${pendingNames}仍需按各自环境完成连接器校验。` }
  }
  const primary = gates[0]?.gate
  return {
    tone: primary?.tone || 'warning' as Tone,
    icon: primary?.icon || 'WarningFilled',
    detail: primary?.description || '尚未读取目标环境的服务端连接器状态。',
  }
})
const canCreatePackageImportProposal = computed(() => Boolean(
  packagePreview.value?.applicable
  && previewContextMatchesCurrentTarget.value
  && canManageImportProposals.value
  && (sourceImportPackage.value || starterKitPreview.value)
  && (starterKitPreview.value ? starterKitFingerprintMatchesPreview.value : hasVerifiedSourcePackageFingerprint.value)
  && selectedBranch.value?.status === 'active'
  && selectedBranch.value?.head_snapshot_id
  && !loading.value
  && !actionLoading.value,
))
const importProposalGateMessage = computed(() => {
  if (!packagePreview.value) return '请先完成资源包导入预检。'
  if (!previewContextMatchesCurrentTarget.value) return '预检的场景、环境或 Starter Kit 已变化；请重新预检。'
  if (!packagePreview.value.applicable) return '预检仍存在格式错误、目标冲突或待绑定项，不能创建导入提案。'
  if (!canManageImportProposals.value) return '当前账户可预检，但没有创建受治理提案的组织管理权限。'
  if (!sourceImportPackage.value && !starterKitPreview.value) return '可用于创建提案的资源包已失效；请重新执行预检。'
  if (!starterKitPreview.value && !hasVerifiedSourcePackageFingerprint.value) return '上传资源包缺少或不匹配完整性指纹；可预检但不能创建提案。'
  if (starterKitPreview.value && !starterKitFingerprintMatchesPreview.value) return 'Starter Kit 指纹与预检结果不一致；请重新预检。'
  if (!selectedBranch.value) return '请选择一个发布分支后再创建导入提案。'
  if (selectedBranch.value.status !== 'active') return '只能向状态为“active”的发布分支创建导入提案。'
  if (!selectedBranch.value.head_snapshot_id) return '当前发布分支没有可用快照，暂不能创建导入提案。'
  if (loading.value || actionLoading.value) return '当前仍有发布治理操作进行中，请稍候。'
  return '预检通过；创建后仍需独立评审和合入。'
})
const releaseGates = computed(() => [
  { id: 'branch', title: '发布分支', description: '发布必须关联当前场景中的发布分支和快照。', detail: selectedBranch.value ? `已选择 · ${selectedBranch.value.name}` : '待处理 · 请先选择分支', tone: selectedBranch.value ? 'success' : 'warning' as Tone, icon: selectedBranch.value ? 'CircleCheckFilled' : 'WarningFilled' },
  { id: 'review', title: '提案评审', description: '提交的提案需要经服务端授权的评审操作推进状态。', detail: reviewPendingCount.value ? `待评审 · ${reviewPendingCount.value} 项` : '当前没有待评审提案', tone: reviewPendingCount.value ? 'warning' : 'success' as Tone, icon: reviewPendingCount.value ? 'Clock' : 'CircleCheckFilled' },
  { id: 'connectors', title: '连接器与环境', description: '发布前由服务端复核当前快照在目标环境中的连接器绑定与健康状态。', detail: connectorGateSummary.value.detail, tone: connectorGateSummary.value.tone, icon: connectorGateSummary.value.icon },
  { id: 'records', title: '环境记录', description: '发布和回滚都将写入环境与快照的审计记录。', detail: records.value.length ? `已加载 · ${records.value.length} 条记录` : '暂无环境发布记录', tone: records.value.length ? 'info' : 'warning' as Tone, icon: records.value.length ? 'DocumentChecked' : 'Clock' },
  { id: 'confirmation', title: '显式确认', description: '合入、发布和回滚均需在界面确认后请求后端。', detail: '后端仍会重新验证权限和不变量', tone: 'info' as Tone, icon: 'Lock' },
])

function routeScenarioId() {
  const value = route.query.scenario_id
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}
function isCurrentPackagePreviewContext(context: PackagePreviewContext) {
  return context.scenarioId === scenarioId.value
    && context.environment === packageImportEnvironment.value
    && (context.kind !== 'starter-kit' || context.starterKitId === selectedStarterKitId.value)
}
function shortId(value?: string | null) { return value ? (value.length > 12 ? `${value.slice(0, 10)}…` : value) : '' }
function formatDate(value?: string | null) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
function userLabel(value?: string | null) { return value ? `用户 ${shortId(value)}` : '系统/未知用户' }
function initials(value?: string | null) { return (value || '系').slice(0, 1).toUpperCase() }
function environmentName(value?: string | null) { return ENVIRONMENTS.find((environment) => environment.id === value)?.name || value || '未指定环境' }
function isEnvironment(value?: string | null): value is EnvironmentId { return value === 'dev' || value === 'staging' || value === 'prod' }
function isConnectorKind(value: unknown): value is 'data_source' | 'mcp' | 'llm' {
  return value === 'data_source' || value === 'mcp' || value === 'llm'
}
function latestRecord(environment: EnvironmentId) {
  return records.value
    .filter((record) => record.environment === environment)
    .slice()
    .sort((left, right) => String(right.created_at || '').localeCompare(String(left.created_at || '')))[0] || null
}
function recordStatus(status?: string) {
  const map: Record<string, { label: string; tone: Tone; icon: string; description: string }> = {
    released: { label: '已发布', tone: 'success', icon: 'CircleCheckFilled', description: '最近记录表示该环境已经接收过一个发布快照。' },
    superseded: { label: '已替换', tone: 'info', icon: 'RefreshRight', description: '该环境的最近记录已被后续版本替换。' },
    rolled_back: { label: '已回滚', tone: 'warning', icon: 'RefreshLeft', description: '最近发布记录显示环境已回滚。' },
  }
  return map[status || ''] || { label: '暂无记录', tone: 'info' as Tone, icon: 'Minus', description: '尚未从服务端读取到此环境的发布记录。' }
}
function branchStatus(status?: string) {
  const map: Record<string, { label: string; tone: Tone; icon: string }> = {
    active: { label: '活跃分支', tone: 'success', icon: 'CircleCheckFilled' },
    merged: { label: '已合并', tone: 'info', icon: 'Connection' },
    archived: { label: '已归档', tone: 'info', icon: 'FolderOpened' },
  }
  return map[status || ''] || { label: status || '未知状态', tone: 'info' as Tone, icon: 'InfoFilled' }
}
function proposalStatus(status?: string) {
  const map: Record<string, { label: string; tone: Tone; icon: string }> = {
    draft: { label: '草稿', tone: 'info', icon: 'EditPen' },
    submitted: { label: '待评审', tone: 'warning', icon: 'Clock' },
    approved: { label: '已批准', tone: 'success', icon: 'CircleCheckFilled' },
    rejected: { label: '已拒绝', tone: 'danger', icon: 'CircleCloseFilled' },
    merged: { label: '已合入', tone: 'success', icon: 'Connection' },
    withdrawn: { label: '已撤回', tone: 'info', icon: 'FolderDelete' },
  }
  return map[status || ''] || { label: status || '未知状态', tone: 'info' as Tone, icon: 'InfoFilled' }
}
function reviewDecisionLabel(decision?: string) { return decision === 'approve' ? '批准' : decision === 'reject' ? '拒绝' : decision || '未知' }
function reviewSummary(proposal: ReleaseProposal) {
  const reviews = proposal.reviews || []
  const approved = reviews.filter((review) => review.decision === 'approve').length
  const rejected = reviews.filter((review) => review.decision === 'reject').length
  if (!reviews.length) return '尚无评审记录。'
  return `${reviews.length} 条记录：${approved} 项批准，${rejected} 项拒绝。`
}
function branchGateMessage(branch: ReleaseBranch) {
  if (branch.status === 'active' && branch.head_snapshot_id) return '已具备当前快照；发布前仍需要后端校验政策与权限。'
  if (!branch.head_snapshot_id) return '尚未生成可发布快照；请先创建并推进变更提案。'
  return '请结合分支状态、提案审批和服务端策略决定是否推进。'
}
function environmentView(environment: EnvironmentId) {
  const record = latestRecords.value[environment]
  return { ...recordStatus(record?.status), record }
}
function connectorGate(environment: EnvironmentId): { ready: boolean; tone: Tone; icon: string; label: string; description: string } {
  if (!selectedBranch.value || !publishSnapshotId.value) {
    return { ready: false, tone: 'warning', icon: 'WarningFilled', label: '连接器门禁待处理', description: '请先选择包含可发布快照的发布分支。' }
  }
  if (connectorReadinessLoading.value) {
    return { ready: false, tone: 'info', icon: 'Loading', label: '正在读取连接器门禁', description: '正在向服务端读取当前快照的环境绑定状态。' }
  }
  if (connectorReadinessError.value) {
    return { ready: false, tone: 'danger', icon: 'CircleCloseFilled', label: '连接器门禁暂不可用', description: `${connectorReadinessError.value}。请刷新后重试。` }
  }
  const readiness = connectorReadiness.value[environment]
  if (!readiness) {
    return { ready: false, tone: 'warning', icon: 'WarningFilled', label: '连接器门禁待读取', description: '尚未读取该环境的服务端连接器状态。' }
  }
  if (!readiness.ready) {
    return { ready: false, tone: 'danger', icon: 'CircleCloseFilled', label: '连接器门禁未通过', description: readiness.reasons.join('；') || '目标环境存在未就绪的连接器绑定。' }
  }
  return readiness.audit.length
    ? { ready: true, tone: 'success', icon: 'CircleCheckFilled', label: '连接器门禁已通过', description: `服务端已确认 ${readiness.audit.length} 项目标环境连接器绑定。` }
    : { ready: true, tone: 'success', icon: 'CircleCheckFilled', label: '连接器门禁已通过', description: '当前快照没有需要环境绑定的受治理连接器。' }
}
function canPublishEnvironment(environment: EnvironmentId) {
  return canPublish.value && connectorGate(environment).ready
}
function rollbackCandidates(environment: EnvironmentId) {
  const active = latestRecords.value[environment]
  const branchId = active?.branch_id
  const seenSnapshots = new Set<string>()
  return records.value
    .filter((record) => (
      record.environment === environment
      && Boolean(record.snapshot_id)
      && record.id !== active?.id
      && (!branchId || record.branch_id === branchId)
      && record.snapshot_id !== active?.snapshot_id
    ))
    .slice()
    .sort((left, right) => String(right.created_at || '').localeCompare(String(left.created_at || '')))
    .filter((record) => {
      if (seenSnapshots.has(record.snapshot_id)) return false
      seenSnapshots.add(record.snapshot_id)
      return true
    })
}
function rollbackCandidate(environment: EnvironmentId) {
  const candidates = rollbackCandidates(environment)
  return candidates.find((record) => record.id === rollbackSelections.value[environment]) || candidates[0] || null
}
function rollbackRecordLabel(record: ReleaseRecord) {
  const timestamp = formatDate(record.created_at) || '未知时间'
  return `${shortId(record.snapshot_id)} · ${timestamp}`
}
function syncRollbackSelections() {
  for (const environment of ENVIRONMENTS) {
    const candidates = rollbackCandidates(environment.id)
    const selected = rollbackSelections.value[environment.id]
    if (!candidates.some((record) => record.id === selected)) {
      rollbackSelections.value[environment.id] = candidates[0]?.id || ''
    }
  }
}
function canReview(proposal: ReleaseProposal) { return proposal.status === 'submitted' && proposal.created_by_user_id !== auth.user?.id }
function canSubmitProposal(proposal: ReleaseProposal) { return proposal.status === 'draft' && proposal.created_by_user_id === auth.user?.id }
const SENSITIVE_KEY_PATTERN = /secret|password|passwd|token|credential|api[_-]?key|authorization|bearer|private[_-]?key|access[_-]?key|client[_-]?secret|signature|cookie|session/i
const INLINE_SECRET_PATTERN = /((?:["']?(?:api[_\s-]?key|access[_\s-]?token|refresh[_\s-]?token|id[_\s-]?token|client[_\s-]?secret|private[_\s-]?key|authorization|bearer|secret|password|passwd|credential|token|signature|cookie|session(?:[_\s-]?id)?)["']?\s*[:=]\s*)(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^\s,;)\]}]+))/gi
const QUERY_SECRET_PATTERN = /([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|token|secret|password|credential|signature|sig|key)=)[^&#\s]+/gi
const OPAQUE_SECRET_PATTERN = /\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|glpat-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b/g

function isSensitiveKey(key: string) { return SENSITIVE_KEY_PATTERN.test(key) }
function redactDisplayText(value: string) {
  return value
    .replace(/-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----[\s\S]*?-----END(?: [A-Z0-9]+)? PRIVATE KEY-----/gi, '[已脱敏私钥]')
    .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi, 'Bearer [已脱敏]')
    .replace(INLINE_SECRET_PATTERN, '$1[已脱敏]')
    .replace(QUERY_SECRET_PATTERN, '$1[已脱敏]')
    .replace(OPAQUE_SECRET_PATTERN, '[已脱敏]')
}
function sanitizeForDisplay(value: unknown, depth = 0, seen = new WeakSet<object>()): unknown {
  if (typeof value === 'string') return redactDisplayText(value)
  if (value === null || typeof value === 'boolean' || typeof value === 'number') return value
  if (typeof value !== 'object') return redactDisplayText(String(value))
  if (seen.has(value)) return '[循环引用已省略]'
  if (depth >= 5) return Array.isArray(value) ? `[已折叠的数组，${value.length} 项]` : '[已折叠的嵌套对象]'

  seen.add(value)
  if (Array.isArray(value)) {
    const items = value.slice(0, 24).map((item) => sanitizeForDisplay(item, depth + 1, seen))
    if (value.length > items.length) items.push(`[另有 ${value.length - items.length} 项已省略]`)
    return items
  }

  const source = value as Record<string, unknown>
  const result = Object.create(null) as Record<string, unknown>
  let omitted = 0
  for (const [key, item] of Object.entries(source)) {
    if (isSensitiveKey(key) || Object.keys(result).length >= 24) {
      omitted += 1
      continue
    }
    result[key] = sanitizeForDisplay(item, depth + 1, seen)
  }
  if (omitted) result['…'] = `[已省略 ${omitted} 个敏感或多余字段]`
  return result
}
function displaySummary(value: unknown, maxLength: number) {
  const sanitized = sanitizeForDisplay(value)
  let text = ''
  try {
    text = typeof sanitized === 'string' ? sanitized : JSON.stringify(sanitized) || ''
  } catch {
    text = '[无法安全展示的内容]'
  }
  const redacted = redactDisplayText(text)
  return redacted.length > maxLength ? `${redacted.slice(0, maxLength)}…` : redacted
}
function safeContentEntries(content: Record<string, any>) {
  return Object.entries(content || {})
    .filter(([key]) => !isSensitiveKey(key))
    .slice(0, 24)
    .map(([key, value]) => ({ key: redactDisplayText(key), value: displaySummary(value, 500) }))
}
function compactRecord(value: Record<string, any>) {
  return Object.entries(value)
    .filter(([key]) => !isSensitiveKey(key))
    .slice(0, 6)
    .map(([key, item]) => `${redactDisplayText(key)}=${displaySummary(item, 180)}`)
    .join(' · ')
    .slice(0, 700)
}
function toErrorMessage(cause: unknown, fallback: string) {
  return cause instanceof Error && cause.message ? cause.message : fallback
}

async function loadScenarios() {
  const request = ++scenarioLoadRequest
  loading.value = true
  error.value = ''
  try {
    // Generic discovery also includes foreign public scenarios.  Release
    // governance deliberately lists only scenarios owned by this tenant.
    const availableScenarios = await api.listReleaseScenarios()
    if (viewDisposed || request !== scenarioLoadRequest) return
    scenarios.value = availableScenarios
    const requested = routeScenarioId()
    const available = scenarios.value.some((scenario) => scenario.id === requested) ? requested : scenarios.value[0]?.id || ''
    scenarioId.value = available
    if (available && requested !== available) {
      await router.replace({ query: { ...route.query, scenario_id: available } })
      return
    }
    if (!available) {
      branches.value = []
      proposals.value = []
      records.value = []
      connectorReadiness.value = {}
      connectorReadinessError.value = ''
      connectorReadinessRequest += 1
      return
    }
    await loadReleaseData(available, false)
  } catch (cause) {
    if (!viewDisposed && request === scenarioLoadRequest) error.value = toErrorMessage(cause, '业务场景加载失败')
  } finally {
    if (!viewDisposed && request === scenarioLoadRequest) loading.value = false
  }
}
async function loadStarterKits() {
  const request = ++starterKitRequest
  starterKitsLoading.value = true
  try {
    const kits = await api.listStarterKits()
    if (viewDisposed || request !== starterKitRequest) return
    starterKits.value = kits
    if (!kits.some((kit) => kit.id === selectedStarterKitId.value)) {
      selectedStarterKitId.value = kits[0]?.id || ''
    }
  } catch (cause) {
    // The normal package exchange remains available if the optional static
    // catalog cannot be loaded.  Keep a recoverable message on this page.
    if (!viewDisposed && request === starterKitRequest) error.value = toErrorMessage(cause, 'Starter Kit 目录加载失败')
  } finally {
    if (!viewDisposed && request === starterKitRequest) starterKitsLoading.value = false
  }
}
async function loadConnectorReadiness(id = scenarioId.value) {
  const snapshotId = publishSnapshotId.value
  const request = ++connectorReadinessRequest
  connectorReadiness.value = {}
  connectorReadinessError.value = ''
  if (!id || !snapshotId) {
    connectorReadinessLoading.value = false
    return
  }
  connectorReadinessLoading.value = true
  try {
    const results = await Promise.all(ENVIRONMENTS.map(async (environment) => [
      environment.id,
      await api.getConnectorReadiness(id, { snapshot_id: snapshotId, environment: environment.id }),
    ] as const))
    if (viewDisposed || request !== connectorReadinessRequest || id !== scenarioId.value || snapshotId !== publishSnapshotId.value) return
    connectorReadiness.value = Object.fromEntries(results) as Partial<Record<EnvironmentId, ConnectorReadiness>>
  } catch {
    if (viewDisposed || request !== connectorReadinessRequest || id !== scenarioId.value) return
    connectorReadinessError.value = '连接器状态读取失败'
  } finally {
    if (!viewDisposed && request === connectorReadinessRequest) connectorReadinessLoading.value = false
  }
}
async function loadReleaseData(id = scenarioId.value, showLoading = true) {
  if (!id) return
  const request = ++releaseDataRequest
  if (showLoading) loading.value = true
  error.value = ''
  try {
    const [branchList, proposalList, recordList] = await Promise.all([
      api.listReleaseBranches(id),
      api.listReleaseProposals(id),
      api.listReleaseRecords(id),
    ])
    if (viewDisposed || request !== releaseDataRequest || id !== scenarioId.value) return
    branches.value = branchList
    proposals.value = proposalList
    records.value = recordList
    syncRollbackSelections()
    if (!branchList.some((branch) => branch.id === selectedBranchId.value)) selectedBranchId.value = branchList[0]?.id || ''
    const current = visibleProposals.value
    if (!current.some((proposal) => proposal.id === selectedProposalId.value)) selectedProposalId.value = current[0]?.id || ''
    await loadConnectorReadiness(id)
  } catch (cause) {
    if (!viewDisposed && request === releaseDataRequest) error.value = toErrorMessage(cause, '发布治理数据加载失败')
  } finally {
    if (showLoading && !viewDisposed && request === releaseDataRequest) loading.value = false
  }
}
async function selectScenario(id: string) {
  if (!id) return
  scenarioId.value = id
  selectedBranchId.value = ''
  selectedProposalId.value = ''
  connectorReadiness.value = {}
  connectorReadinessError.value = ''
  connectorReadinessRequest += 1
  clearPackageImportState()
  if (routeScenarioId() !== id) {
    await router.replace({ query: { ...route.query, scenario_id: id } })
    return
  }
  await loadReleaseData(id)
}
function selectProposal(id: string) {
  selectedProposalId.value = id
  const proposal = proposals.value.find((item) => item.id === id)
  feedback.value = proposal ? `当前提案已切换为「${proposal.title}」。` : ''
}
function openBranchDialog() {
  branchForm.value = { name: '', description: '' }
  branchDialogVisible.value = true
}
async function openProposalDialog() {
  const branch = selectedBranch.value
  if (!branch?.head_snapshot_id) {
    error.value = '当前分支没有可编辑的 head snapshot；请先通过受治理的流程生成完整本体快照。'
    return
  }
  actionLoading.value = 'snapshot'
  error.value = ''
  try {
    const snapshot = await api.getReleaseSnapshot(branch.head_snapshot_id)
    if (!snapshot.content || Array.isArray(snapshot.content) || !Object.keys(snapshot.content).length) {
      throw new Error('当前 head snapshot 不包含完整的可编辑本体定义')
    }
    proposalBaseSnapshotId.value = snapshot.id
    proposalForm.value = {
      title: '',
      description: '',
      contentText: JSON.stringify(snapshot.content, null, 2),
      submit: true,
    }
    proposalDialogVisible.value = true
  } catch (cause) {
    error.value = toErrorMessage(cause, '读取当前分支快照失败')
  } finally {
    actionLoading.value = ''
  }
}
function openReviewDialog(proposal: ReleaseProposal) {
  reviewTarget.value = proposal
  reviewForm.value = { decision: 'approve', comment: '' }
  reviewDialogVisible.value = true
}
function openMergeDialog(proposal: ReleaseProposal) {
  mergeTarget.value = proposal
  mergeNote.value = ''
  mergeAcknowledged.value = false
  mergeDialogVisible.value = true
}
function openCompare(proposal: ReleaseProposal) {
  compareTarget.value = proposal
  selectedProposalId.value = proposal.id
  compareVisible.value = true
}
async function openPublishDialog(environment: EnvironmentId) {
  await loadConnectorReadiness()
  const gate = connectorGate(environment)
  if (!gate.ready) {
    feedback.value = `${environmentName(environment)}：${gate.description}`
    return
  }
  publishForm.value = { environment, notes: '' }
  publishAcknowledged.value = false
  publishDialogVisible.value = true
}
function openConnectorSettings(environment: EnvironmentId) {
  if (!scenarioId.value) return
  void router.push({ path: '/connectors', query: { scenario_id: scenarioId.value, environment, return_to: route.fullPath } })
}
function openRollback(record: ReleaseRecord) {
  rollbackTarget.value = record
  rollbackReason.value = ''
  rollbackAcknowledged.value = false
  rollbackVisible.value = true
}
function openExportDialog() {
  exportAcknowledged.value = false
  exportDialogVisible.value = true
}
function openImportPicker() {
  if (!scenarioId.value) return
  packageFileInput.value?.click()
}
function canConfigureBinding(binding: Record<string, any>) {
  return Boolean(binding?.binding_key && isConnectorKind(binding?.kind))
}
function openConnectorBinding(binding: Record<string, any>) {
  if (!scenarioId.value || !canConfigureBinding(binding)) return
  const referenceLabel = String(binding.reference_label || binding.binding_key || '')
  void router.push({
    path: '/connectors',
    query: {
      scenario_id: scenarioId.value,
      environment: packageImportEnvironment.value,
      binding_key: String(binding.binding_key),
      kind: String(binding.kind),
      reference_label: referenceLabel,
      return_to: route.fullPath,
    },
  })
}
function resetPackageImportProposalForm() {
  packageImportProposalAcknowledged.value = false
  packageImportProposalForm.value = { title: '', description: '', submit: true }
}
function clearPackageImportState() {
  packagePreviewRequest += 1
  packagePreviewVisible.value = false
  packageImportProposalVisible.value = false
  packagePreview.value = null
  sourceImportPackage.value = null
  packagePreviewContext.value = null
  importErrors.value = []
  importWarnings.value = []
  importFingerprint.value = ''
  importFileName.value = ''
  resetPackageImportProposalForm()
}
function openPackageImportProposalDialog() {
  if (!canCreatePackageImportProposal.value) {
    error.value = importProposalGateMessage.value
    return
  }
  const kit = starterKitPreview.value
  const sourceName = redactDisplayText(
    kit?.name || sourceImportPackage.value?.manifest?.name || importFileName.value || '资源包',
  )
  const fingerprint = packagePreview.value?.package_fingerprint || importFingerprint.value
  packageImportProposalForm.value = {
    title: `${kit ? '导入 Starter Kit' : '导入资源包'}：${sourceName}`.slice(0, 200),
    description: kit
      ? `Starter Kit：${kit.id}@${kit.version}；资源包指纹：${fingerprint}`
      : fingerprint ? `资源包指纹：${fingerprint}` : '',
    submit: true,
  }
  packageImportProposalAcknowledged.value = false
  packageImportProposalVisible.value = true
}
async function exportPackage() {
  if (!scenarioId.value || !exportAcknowledged.value) return
  actionLoading.value = 'export'
  error.value = ''
  try {
    const resourcePackage: OntologyResourcePackage = await api.exportScenarioPackage(scenarioId.value)
    const blob = new Blob([JSON.stringify(resourcePackage, null, 2)], { type: 'application/json;charset=utf-8' })
    const objectUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = objectUrl
    link.download = `ontology-resource-package-${scenarioId.value.replace(/[^a-zA-Z0-9_-]/g, '_')}.json`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(objectUrl)
    exportDialogVisible.value = false
    feedback.value = `已导出脱敏资源包（指纹 ${shortId(resourcePackage.manifest.fingerprint)}）。`
  } catch (cause) {
    error.value = toErrorMessage(cause, '导出资源包失败')
  } finally {
    actionLoading.value = ''
  }
}
async function readPackageFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || !scenarioId.value) return
  if (file.size > 5 * 1024 * 1024) {
    error.value = '资源包文件不能超过 5 MB；请拆分或确认导出的定义包大小。'
    return
  }
  const context: PackagePreviewContext = {
    kind: 'upload',
    scenarioId: scenarioId.value,
    environment: packageImportEnvironment.value,
  }
  clearPackageImportState()
  const request = ++packagePreviewRequest
  actionLoading.value = 'import-preview'
  error.value = ''
  importFileName.value = file.name
  try {
    const raw = JSON.parse(await file.text())
    if (!raw || Array.isArray(raw) || typeof raw !== 'object') throw new Error('资源包必须是 JSON 对象')
    const validation = await api.validateResourcePackage(raw)
    if (request !== packagePreviewRequest || !isCurrentPackagePreviewContext(context)) return
    sourceImportPackage.value = raw as Record<string, any>
    importErrors.value = validation.errors as PackageIssue[]
    importWarnings.value = validation.warnings as PackageIssue[]
    importFingerprint.value = validation.fingerprint
    packagePreviewVisible.value = true
    if (!validation.valid) {
      feedback.value = '资源包格式未通过校验；已显示只读校验信息，未向目标场景提交任何变更。'
      return
    }
    const preview = await api.previewScenarioPackageImport(
      context.scenarioId,
      sourceImportPackage.value,
      context.environment,
    )
    if (request !== packagePreviewRequest || !isCurrentPackagePreviewContext(context)) return
    packagePreview.value = preview
    packagePreviewContext.value = context
    importErrors.value = preview.errors
    importWarnings.value = preview.warnings
    importFingerprint.value = preview.package_fingerprint
    feedback.value = '资源包导入预检已完成；结果只展示差异和待处理项，未应用任何变更。'
  } catch (cause) {
    if (request === packagePreviewRequest) error.value = toErrorMessage(cause, '资源包导入预检失败')
  } finally {
    if (request === packagePreviewRequest) actionLoading.value = ''
  }
}
async function previewStarterKitImport() {
  if (!scenarioId.value || !selectedStarterKitId.value) return
  const context: PackagePreviewContext = {
    kind: 'starter-kit',
    scenarioId: scenarioId.value,
    environment: packageImportEnvironment.value,
    starterKitId: selectedStarterKitId.value,
  }
  clearPackageImportState()
  const request = ++packagePreviewRequest
  actionLoading.value = 'starter-kit-preview'
  error.value = ''
  try {
    const preview = await api.previewStarterKitImport(
      context.starterKitId!,
      context.scenarioId,
      context.environment,
    )
    if (request !== packagePreviewRequest || !isCurrentPackagePreviewContext(context)) return
    packagePreview.value = preview
    packagePreviewContext.value = context
    importErrors.value = preview.errors
    importWarnings.value = preview.warnings
    importFingerprint.value = preview.package_fingerprint
    importFileName.value = preview.starter_kit?.name || selectedStarterKit.value?.name || 'Starter Kit'
    packagePreviewVisible.value = true
    feedback.value = 'Starter Kit 预检已完成；结果仅展示差异和待处理项，未应用任何变更。'
  } catch (cause) {
    if (request === packagePreviewRequest) error.value = toErrorMessage(cause, 'Starter Kit 导入预检失败')
  } finally {
    if (request === packagePreviewRequest) actionLoading.value = ''
  }
}
async function createPackageImportProposal() {
  const branch = selectedBranch.value
  const resourcePackage = sourceImportPackage.value
  const kit = starterKitPreview.value
  if (
    !scenarioId.value
    || !branch
    || branch.status !== 'active'
    || !canCreatePackageImportProposal.value
    || (!resourcePackage && !kit)
    || !packageImportProposalAcknowledged.value
    || !packageImportProposalForm.value.title.trim()
  ) return
  actionLoading.value = 'import-proposal'
  error.value = ''
  try {
    const proposal = kit
      ? await api.createStarterKitImportProposal(kit.id, scenarioId.value, {
        branch_id: branch.id,
        environment: packageImportEnvironment.value,
        expected_fingerprint: packagePreview.value!.package_fingerprint,
        title: packageImportProposalForm.value.title,
        description: packageImportProposalForm.value.description || undefined,
        submit: packageImportProposalForm.value.submit,
      })
      : await api.createPackageImportProposal(scenarioId.value, {
        package: resourcePackage!,
        branch_id: branch.id,
        environment: packageImportEnvironment.value,
        title: packageImportProposalForm.value.title,
        description: packageImportProposalForm.value.description || undefined,
        submit: packageImportProposalForm.value.submit,
      })
    const submitted = packageImportProposalForm.value.submit
    selectedProposalId.value = proposal.id
    clearPackageImportState()
    feedback.value = submitted
      ? `已创建${kit ? ' Starter Kit' : '资源包'}导入提案 ${shortId(proposal.id)}，现已提交评审。`
      : `已创建${kit ? ' Starter Kit' : '资源包'}导入提案 ${shortId(proposal.id)} 草稿。`
    await loadReleaseData()
  } catch (cause) {
    error.value = toErrorMessage(cause, '创建资源包导入提案失败')
  } finally {
    actionLoading.value = ''
  }
}
function showEnvironmentPreview(environment: EnvironmentId) {
  const view = environmentView(environment)
  const gate = connectorGate(environment)
  feedback.value = `${environmentName(environment)}：${view.description} ${gate.label}，${gate.description}`
}
async function createBranch() {
  if (!scenarioId.value || !branchForm.value.name) return
  actionLoading.value = 'branch'
  error.value = ''
  try {
    const branch = await api.createReleaseBranch(scenarioId.value, branchForm.value)
    branches.value.unshift(branch)
    selectedBranchId.value = branch.id
    branchDialogVisible.value = false
    feedback.value = `已创建发布分支「${branch.name}」。`
    await loadReleaseData()
  } catch (cause) {
    error.value = toErrorMessage(cause, '创建发布分支失败')
  } finally {
    actionLoading.value = ''
  }
}
async function createProposal() {
  if (!selectedBranch.value || !proposalForm.value.title) return
  let content: Record<string, any>
  try {
    const parsed = JSON.parse(proposalForm.value.contentText)
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('内容必须是 JSON 对象')
    if (!Object.keys(parsed).length) throw new Error('提案内容不能为空；请基于当前分支完整快照编辑后再提交')
    content = parsed
  } catch (cause) {
    error.value = toErrorMessage(cause, '提案内容必须是有效的 JSON 对象')
    return
  }
  actionLoading.value = 'proposal'
  error.value = ''
  try {
    const proposal = await api.createReleaseProposal(selectedBranch.value.id, {
      title: proposalForm.value.title,
      description: proposalForm.value.description || undefined,
      content,
      submit: proposalForm.value.submit,
    })
    proposals.value.unshift(proposal)
    selectedProposalId.value = proposal.id
    proposalDialogVisible.value = false
    feedback.value = proposalForm.value.submit ? '提案已创建并提交评审。' : '提案草稿已创建。'
    await loadReleaseData()
  } catch (cause) {
    error.value = toErrorMessage(cause, '创建变更提案失败')
  } finally {
    actionLoading.value = ''
  }
}
async function submitReleaseProposal(proposal: ReleaseProposal) {
  if (!canSubmitProposal(proposal)) return
  actionLoading.value = 'submit-proposal'
  error.value = ''
  try {
    const submitted = await api.submitReleaseProposal(proposal.id)
    const index = proposals.value.findIndex((item) => item.id === submitted.id)
    if (index >= 0) proposals.value.splice(index, 1, submitted)
    selectedProposalId.value = submitted.id
    feedback.value = `提案「${submitted.title}」已提交评审。`
    await loadReleaseData()
  } catch (cause) {
    error.value = toErrorMessage(cause, '提交提案评审失败')
  } finally {
    actionLoading.value = ''
  }
}
async function submitReview() {
  if (!reviewTarget.value) return
  actionLoading.value = 'review'
  error.value = ''
  try {
    const review = await api.reviewReleaseProposal(reviewTarget.value.id, reviewForm.value)
    const proposal = proposals.value.find((item) => item.id === reviewTarget.value?.id)
    if (proposal) proposal.reviews = [...(proposal.reviews || []), review]
    reviewDialogVisible.value = false
    feedback.value = `已提交${reviewDecisionLabel(review.decision)}评审。`
    await loadReleaseData()
  } catch (cause) {
    error.value = toErrorMessage(cause, '提交评审失败')
  } finally {
    actionLoading.value = ''
  }
}
async function mergeProposal() {
  if (!mergeTarget.value || !mergeAcknowledged.value) return
  actionLoading.value = 'merge'
  error.value = ''
  try {
    const merged = await api.mergeReleaseProposal(mergeTarget.value.id, mergeNote.value)
    const index = proposals.value.findIndex((proposal) => proposal.id === merged.id)
    if (index >= 0) proposals.value.splice(index, 1, merged)
    mergeDialogVisible.value = false
    feedback.value = `已请求合入提案「${merged.title}」。`
    await loadReleaseData()
  } catch (cause) {
    error.value = toErrorMessage(cause, '合入提案失败')
  } finally {
    actionLoading.value = ''
  }
}
async function publishRelease() {
  if (!scenarioId.value || !selectedBranch.value || !publishSnapshotId.value || !publishAcknowledged.value) return
  actionLoading.value = 'publish'
  error.value = ''
  try {
    await loadConnectorReadiness()
    const gate = connectorGate(publishForm.value.environment)
    if (!gate.ready) throw new Error(gate.description)
    const target = selectedProposal.value?.status === 'merged'
      ? { proposal_id: selectedProposal.value.id }
      : { branch_id: selectedBranch.value.id }
    const record = await api.publishRelease(scenarioId.value, {
      environment: publishForm.value.environment,
      ...target,
      notes: publishForm.value.notes || undefined,
    })
    records.value.unshift(record)
    publishDialogVisible.value = false
    feedback.value = `已请求发布到${environmentName(record.environment)}，记录 ${shortId(record.id)} 已写入。`
    await loadReleaseData()
  } catch (cause) {
    error.value = toErrorMessage(cause, '发布请求失败')
  } finally {
    actionLoading.value = ''
  }
}
async function rollbackRelease() {
  if (!scenarioId.value || !rollbackTarget.value || !rollbackAcknowledged.value) return
  const environment = isEnvironment(rollbackTarget.value.environment) ? rollbackTarget.value.environment : undefined
  actionLoading.value = 'rollback'
  error.value = ''
  try {
    const rollback = await api.rollbackRelease(scenarioId.value, {
      target_snapshot_id: rollbackTarget.value.snapshot_id,
      branch_id: rollbackTarget.value.branch_id,
      environment,
      reason: rollbackReason.value || undefined,
    })
    rollbackVisible.value = false
    feedback.value = `已请求回滚到快照 ${shortId(rollback.target_snapshot_id)}，回滚记录 ${shortId(rollback.id)} 已写入。`
    await loadReleaseData()
  } catch (cause) {
    error.value = toErrorMessage(cause, '回滚请求失败')
  } finally {
    actionLoading.value = ''
  }
}

watch(selectedBranchId, () => {
  if (!visibleProposals.value.some((proposal) => proposal.id === selectedProposalId.value)) selectedProposalId.value = visibleProposals.value[0]?.id || ''
  void loadConnectorReadiness()
})
watch([packageImportEnvironment, selectedStarterKitId], () => {
  if (packagePreview.value || sourceImportPackage.value || packagePreviewVisible.value) {
    clearPackageImportState()
    feedback.value = '资源包导入的场景、环境或 Starter Kit 已变化；请重新预检。'
  } else {
    packagePreviewRequest += 1
  }
})
onMounted(() => {
  viewDisposed = false
  void loadScenarios()
  void loadStarterKits()
})
onBeforeUnmount(() => {
  viewDisposed = true
  scenarioLoadRequest += 1
  releaseDataRequest += 1
  starterKitRequest += 1
  connectorReadinessRequest += 1
  packagePreviewRequest += 1
})
</script>

<style scoped>
.release-page { min-height: 100%; max-width: 1600px; margin: 0 auto; padding: 24px 28px 36px; }
.release-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 14px; }
.eyebrow { display: inline-block; color: var(--primary); font-size: 10px; font-weight: 800; letter-spacing: .15em; }
.release-header h1 { margin: 5px 0 6px; color: var(--text); font-size: 25px; letter-spacing: -.035em; }
.release-header p { max-width: 680px; margin: 0; color: var(--text-2); font-size: 13px; }
.release-header-actions, .section-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
.release-header-actions :deep(.el-button), .section-actions :deep(.el-button) { min-height: 40px; }
.scenario-select { width: min(264px, 62vw); }
.branch-select { width: min(240px, 52vw); }
.release-alert, .release-feedback { margin: 0 0 14px; }
.release-feedback { display: flex; align-items: flex-start; gap: 8px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--info) 34%, var(--border)); border-radius: var(--radius-xs); color: var(--text-2); background: var(--info-soft); font-size: 13px; line-height: 1.5; }
.release-feedback .el-icon { flex: 0 0 auto; margin-top: 2px; color: var(--info); }
.empty-card, .branch-empty, .inline-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; min-height: 230px; padding: 26px; text-align: center; color: var(--text-3); }
.empty-card h3, .branch-empty h3 { margin: 0; color: var(--text); font-size: 16px; }
.empty-card p, .branch-empty p, .inline-empty span { max-width: 550px; margin: 0; color: var(--text-2); font-size: 13px; line-height: 1.55; }
.branch-card { display: grid; grid-template-columns: minmax(260px, 1.1fr) minmax(360px, 1.25fr) minmax(230px, .8fr); align-items: center; gap: 22px; margin-bottom: 16px; padding: 20px; }
.branch-heading { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
.branch-icon { display: inline-flex; flex: 0 0 auto; align-items: center; justify-content: center; width: 42px; height: 42px; border-radius: 12px; color: var(--primary-600); background: var(--primary-soft); font-size: 20px; }
.branch-heading h3 { margin: 3px 0 4px; color: var(--text); font-size: 17px; overflow-wrap: anywhere; }
.branch-heading p { margin: 0; color: var(--text-2); font-size: 12px; line-height: 1.55; }
.branch-facts, .proposal-meta, .environment-meta, .compare-facts, .confirmation-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 18px; margin: 0; }
.branch-facts div, .proposal-meta div, .environment-meta div, .compare-facts div, .confirmation-facts div { min-width: 0; }
.branch-facts dt, .proposal-meta dt, .environment-meta dt, .compare-facts dt, .confirmation-facts dt { margin-bottom: 2px; color: var(--text-3); font-size: 11px; font-weight: 650; }
.branch-facts dd, .proposal-meta dd, .environment-meta dd, .compare-facts dd, .confirmation-facts dd { margin: 0; color: var(--text); font-size: 12px; font-weight: 650; overflow-wrap: anywhere; }
code, .mono { font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace; font-size: .92em; }
.branch-gate { display: flex; align-items: flex-start; gap: 9px; padding: 11px; border: 1px solid var(--border); border-radius: 11px; color: var(--text-2); background: var(--surface-2); font-size: 12px; line-height: 1.5; }
.branch-gate > .el-icon { margin-top: 2px; font-size: 17px; }
.branch-gate b { display: block; color: var(--text); font-size: 13px; }
.gate--warning > .el-icon { color: var(--warning); }.gate--success > .el-icon { color: var(--success); }.gate--danger > .el-icon { color: var(--danger); }.gate--info > .el-icon { color: var(--info); }
.branch-empty { grid-column: 1 / -1; min-height: 130px; }
.governance-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.summary-card { display: grid; grid-template-columns: 40px 1fr; gap: 2px 10px; min-height: 102px; padding: 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow-xs); }
.summary-icon { grid-row: span 2; display: inline-flex; align-items: center; justify-content: center; width: 40px; height: 40px; border-radius: 11px; font-size: 19px; }
.summary-card b, .summary-card small { display: block; }.summary-card b { color: var(--text); font-size: 21px; font-variant-numeric: tabular-nums; line-height: 1.15; }.summary-card small { color: var(--text-2); font-size: 12px; font-weight: 650; }.summary-card p { grid-column: 1 / -1; margin: 3px 0 0; color: var(--text-3); font-size: 11px; line-height: 1.45; }
.summary-card--review .summary-icon { color: var(--primary-600); background: var(--primary-soft); }.summary-card--risk .summary-icon { color: var(--warning); background: var(--warning-soft); }.summary-card--ready .summary-icon { color: var(--success); background: var(--success-soft); }.summary-card--rollback .summary-icon { color: var(--info); background: var(--info-soft); }
.release-layout { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(290px, .8fr); gap: 16px; margin-bottom: 16px; }.proposal-panel, .policy-panel, .environment-panel { padding: 20px; }.section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 14px; }.section-head h3 { margin: 4px 0 0; color: var(--text); font-size: 17px; }.section-head p { max-width: 315px; margin: 1px 0 0; color: var(--text-3); font-size: 12px; line-height: 1.5; }
.proposal-list, .policy-list, .environment-list, .reviewer-list { margin: 0; padding: 0; list-style: none; }.proposal-list { display: flex; flex-direction: column; gap: 10px; }.proposal-card { padding: 14px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2); transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }.proposal-card.selected { border-color: color-mix(in srgb, var(--primary) 65%, var(--border)); box-shadow: inset 3px 0 0 var(--primary); }.proposal-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }.proposal-title-wrap { display: flex; align-items: baseline; gap: 8px; min-width: 0; }.proposal-id { flex: 0 0 auto; color: var(--primary-600); font-size: 11px; font-weight: 750; }.proposal-heading h4 { margin: 0; color: var(--text); font-size: 14px; line-height: 1.4; }
.state-pill { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 4px; min-height: 25px; padding: 3px 7px; border: 1px solid currentColor; border-radius: 999px; font-size: 11px; font-weight: 700; white-space: nowrap; }.state-pill--success { color: var(--success); background: var(--success-soft); }.state-pill--warning { color: var(--warning); background: var(--warning-soft); }.state-pill--danger { color: var(--danger); background: var(--danger-soft); }.state-pill--info { color: var(--info); background: var(--info-soft); }
.proposal-description, .environment-description { margin: 8px 0 11px; color: var(--text-2); font-size: 12.5px; line-height: 1.55; }.review-row { display: grid; grid-template-columns: minmax(120px, .5fr) minmax(0, 1.5fr); gap: 12px; align-items: start; margin-top: 13px; padding-top: 12px; border-top: 1px solid var(--border); }.review-row b { color: var(--text); font-size: 12px; }.review-row p { margin: 3px 0 0; color: var(--text-3); font-size: 11px; line-height: 1.45; }.reviewer-list { display: flex; flex-wrap: wrap; gap: 7px; }.reviewer-list li { display: inline-flex; align-items: center; gap: 4px; min-height: 28px; padding: 3px 7px 3px 3px; border: 1px solid var(--border); border-radius: 999px; color: var(--text-2); background: var(--surface); font-size: 11px; }.reviewer-avatar { display: inline-flex; align-items: center; justify-content: center; width: 21px; height: 21px; border-radius: 50%; color: var(--primary-600); background: var(--primary-soft); font-size: 10px; font-weight: 800; }.reviewer-list small, .review-empty { color: var(--text-3); font-size: 10px; }.reviewer--approved { border-color: color-mix(in srgb, var(--success) 35%, var(--border)) !important; }.reviewer--changes_requested { border-color: color-mix(in srgb, var(--danger) 35%, var(--border)) !important; }.proposal-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px; margin-top: 9px; }.proposal-actions :deep(.el-button) { min-height: 36px; }
.policy-list { display: flex; flex-direction: column; gap: 10px; }.policy-list li { display: flex; align-items: flex-start; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--border); }.policy-list li:last-child { padding-bottom: 2px; border-bottom: 0; }.policy-icon { display: inline-flex; flex: 0 0 auto; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 9px; background: var(--surface-2); }.policy--success .policy-icon { color: var(--success); background: var(--success-soft); }.policy--warning .policy-icon { color: var(--warning); background: var(--warning-soft); }.policy--danger .policy-icon { color: var(--danger); background: var(--danger-soft); }.policy--info .policy-icon { color: var(--info); background: var(--info-soft); }.policy-list h4 { margin: 0; color: var(--text); font-size: 13px; }.policy-list p { margin: 2px 0; color: var(--text-2); font-size: 12px; line-height: 1.45; }.policy-list small { color: var(--text-3); font-size: 11px; }.policy-foot { display: flex; gap: 7px; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); color: var(--text-3); font-size: 11px; line-height: 1.5; }.policy-foot .el-icon { flex: 0 0 auto; margin-top: 2px; color: var(--primary); }.policy-foot p { margin: 0; }
.package-panel { margin-top: 16px; padding: 20px; }.package-body { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }.package-body > div { padding: 13px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface-2); }.package-body h4 { display: flex; align-items: center; gap: 6px; margin: 0 0 5px; color: var(--text); font-size: 13px; }.package-body h4 .el-icon { color: var(--primary); }.package-body p { margin: 0; color: var(--text-2); font-size: 12px; line-height: 1.55; }.package-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin-top: 12px; }.package-environment-select { width: min(194px, 100%); }.package-preview-dialog > h3 { margin: 4px 0 7px; color: var(--text); font-size: 18px; }.package-preview-dialog > p { margin: 0 0 15px; color: var(--text-2); font-size: 13px; line-height: 1.6; }.package-preview-dialog :deep(.el-alert) { margin-bottom: 14px; }.package-preview-facts { margin-bottom: 14px; }.package-environment-note { margin-bottom: 12px; }.package-issues { margin-top: 12px; padding: 12px; border: 1px solid color-mix(in srgb, var(--danger) 28%, var(--border)); border-radius: 10px; background: var(--danger-soft); }.package-issues--warning { border-color: color-mix(in srgb, var(--warning) 32%, var(--border)); background: var(--warning-soft); }.package-issues--info { border-color: color-mix(in srgb, var(--info) 32%, var(--border)); background: var(--info-soft); }.package-issues--success { border-color: color-mix(in srgb, var(--success) 32%, var(--border)); background: var(--success-soft); }.package-issues h4 { margin: 0 0 7px; color: var(--text); font-size: 13px; }.package-issues ul { display: flex; flex-direction: column; gap: 5px; margin: 0; padding-left: 18px; color: var(--text-2); font-size: 12px; line-height: 1.5; }.package-issues b { margin-right: 5px; color: var(--text); }.package-binding-row { display: flex; align-items: center; justify-content: space-between; gap: 9px; }.package-binding-row > span { min-width: 0; overflow-wrap: anywhere; }
.package-fingerprint { display: block; overflow-wrap: anywhere; word-break: break-all; }
.package-body { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.starter-kit-card { display: flex; flex-direction: column; min-width: 0; }
.starter-kit-select { width: 100%; margin-top: 10px; }
.starter-kit-summary { margin-top: 8px !important; overflow-wrap: anywhere; }
.environment-section-head { margin-bottom: 18px; }.environment-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }.environment-stage { position: relative; min-width: 0; }.environment-stage:not(:last-child)::after { position: absolute; z-index: 0; top: 22px; right: -11px; width: 9px; height: 2px; background: var(--border-strong); content: ''; }.stage-index { position: relative; z-index: 1; display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; margin: 0 0 -8px 14px; border: 2px solid var(--surface); border-radius: 50%; color: var(--primary-600); background: var(--primary-soft); font-size: 12px; font-weight: 800; }.environment-stage--success .stage-index { color: var(--success); background: var(--success-soft); }.environment-stage--warning .stage-index { color: var(--warning); background: var(--warning-soft); }.environment-stage--danger .stage-index { color: var(--danger); background: var(--danger-soft); }.environment-card { min-height: 235px; padding: 17px 14px 13px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2); }.environment-card > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }.environment-card h4 { margin: 0; color: var(--text); font-size: 14px; }.environment-card header p { margin: 3px 0 0; color: var(--text-3); font-size: 11px; overflow-wrap: anywhere; }.environment-meta { margin-bottom: 11px; }.environment-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }.environment-button { display: inline-flex; align-items: center; justify-content: center; gap: 4px; min-height: 40px; padding: 7px 9px; border: 1px solid var(--border-strong); border-radius: 9px; color: var(--primary-600); background: var(--surface); font: inherit; font-size: 12px; font-weight: 650; cursor: pointer; touch-action: manipulation; transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease), transform var(--dur) var(--ease); }.environment-button:hover:not(:disabled) { border-color: var(--primary); background: var(--primary-soft); }.environment-button:active:not(:disabled) { transform: scale(.98); }.environment-button:disabled { cursor: not-allowed; opacity: .55; }.environment-button--primary { color: var(--primary-600); border-color: color-mix(in srgb, var(--primary) 35%, var(--border)); }.environment-button--danger { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 35%, var(--border)); }.environment-button--danger:hover { border-color: var(--danger); background: var(--danger-soft); }
.field-help { width: 100%; margin: 5px 0 0; color: var(--text-3); font-size: 12px; line-height: 1.45; }.json-input :deep(textarea) { font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace; }.review-dialog > p, .compare-dialog > p, .confirmation-dialog > p { margin: 0 0 15px; color: var(--text-2); font-size: 13px; line-height: 1.6; }.confirmation-dialog h3, .compare-dialog h3 { margin: 4px 0 7px; color: var(--text); font-size: 18px; }.confirmation-facts, .compare-facts { margin: 0 0 14px; padding: 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); }.confirmation-dialog :deep(.el-alert) { margin-bottom: 14px; }.confirmation-check { display: flex; align-items: flex-start; margin-top: 14px; white-space: normal; color: var(--text-2); font-size: 13px; line-height: 1.5; }.confirmation-check :deep(.el-checkbox__input) { margin-top: 2px; }.diff-table-wrap { margin-top: 15px; overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; outline-offset: 3px; }.diff-table { width: 100%; min-width: 520px; border-collapse: collapse; color: var(--text-2); font-size: 12px; }.diff-table th, .diff-table td { padding: 10px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; line-height: 1.5; overflow-wrap: anywhere; }.diff-table thead th { color: var(--text); background: var(--surface-2); font-size: 11px; }.diff-table tbody th { color: var(--text); font-weight: 700; }.diff-table tbody tr:last-child th, .diff-table tbody tr:last-child td { border-bottom: 0; }.sr-only { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.environment-connector-gate { display: flex; align-items: flex-start; gap: 7px; min-height: 43px; margin: 0 0 11px; padding: 8px; border: 1px solid var(--border); border-radius: 9px; color: var(--text-2); background: var(--surface); font-size: 11px; line-height: 1.45; }.environment-connector-gate > .el-icon { flex: 0 0 auto; margin-top: 2px; font-size: 15px; }.environment-connector-gate b { display: block; color: var(--text); font-size: 11px; }.environment-connector-gate--success { border-color: color-mix(in srgb, var(--success) 34%, var(--border)); }.environment-connector-gate--success > .el-icon { color: var(--success); }.environment-connector-gate--warning { border-color: color-mix(in srgb, var(--warning) 34%, var(--border)); }.environment-connector-gate--warning > .el-icon { color: var(--warning); }.environment-connector-gate--danger { border-color: color-mix(in srgb, var(--danger) 34%, var(--border)); }.environment-connector-gate--danger > .el-icon { color: var(--danger); }.environment-connector-gate--info { border-color: color-mix(in srgb, var(--info) 34%, var(--border)); }.environment-connector-gate--info > .el-icon { color: var(--info); }
@media (max-width: 1120px) { .branch-card { grid-template-columns: minmax(0, 1fr) minmax(360px, 1.1fr); }.branch-gate { grid-column: 1 / -1; }.environment-list { gap: 11px; }.package-body { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.rollback-target-picker { display: grid; gap: 5px; margin: 0 0 11px; }.rollback-target-picker label { color: var(--text-3); font-size: 11px; font-weight: 650; }.rollback-target-picker :deep(.el-select) { width: 100%; }
@media (max-width: 900px) { .governance-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }.release-layout { grid-template-columns: 1fr; }.environment-list, .package-body { grid-template-columns: 1fr; }.environment-stage:not(:last-child)::after { top: auto; right: auto; bottom: -8px; left: 27px; width: 2px; height: 8px; }.environment-card { min-height: 0; } }
@media (max-width: 720px) { .release-page { padding: 18px 14px 28px; }.release-header { flex-direction: column; gap: 13px; }.release-header-actions { width: 100%; justify-content: flex-start; }.branch-card { grid-template-columns: 1fr; gap: 16px; padding: 16px; }.branch-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }.section-head { flex-direction: column; gap: 8px; }.section-head p { max-width: none; } }
@media (max-width: 560px) { .package-body { grid-template-columns: 1fr; }.package-actions { justify-content: flex-start; } }
@media (max-width: 460px) { .governance-summary, .branch-facts, .proposal-meta, .environment-meta, .compare-facts, .confirmation-facts { grid-template-columns: 1fr; }.proposal-heading, .environment-card > header { flex-direction: column; }.proposal-title-wrap { align-items: flex-start; flex-direction: column; gap: 2px; }.review-row { grid-template-columns: 1fr; gap: 7px; }.proposal-actions, .environment-actions { justify-content: flex-start; }.environment-button { min-height: 44px; } }
@media (prefers-reduced-motion: reduce) { .proposal-card, .environment-button { transition: none; }.environment-button:active { transform: none; } }
</style>
