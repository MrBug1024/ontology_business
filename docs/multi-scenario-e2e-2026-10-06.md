# 三类业务场景端到端验收（2026-10-06）

本次用户要求新增至少 2–3 个普通及复杂业务场景，实际完成创建、能力实现、插件开发与发布。本次采用三个合成场景，用户已明确授权发布这些新场景；不修改既有业务场景，不写外部业务系统。模型输出仅为候选，晋级、正式发布、源码定版、业务验收与公开发布分别经过现有服务端门禁。

## 需求账本与影响图

| ID | 原文目标 | 非目标 | 可观察验收 | 影响范围 | 保持不变 | 测试证据 |
|---|---|---|---|---|---|---|
| E2E-1 | 更多普通业务场景 | 接入客户真实数据 | 新建办公采购分流场景；真实 AI 建模；规则成功、阈值边界和业务不通过的结果正确 | 场景 UI、assistant/候选治理现有入口、验收 fixture/脚本 | 行业逻辑留在场景定义与测试 fixture，平台内核不写行业分支 | 实际候选、发布与 invocation 记录 |
| E2E-2 | 更复杂数值业务场景 | 任意代码执行 | 新建供应履约评分场景；加权函数与规则组合；实际分数、阈值、算术溢出安全失败正确 | 同上、统一函数/规则调用、插件源码 | 沿用受信运行时、封闭 Schema、统一 CapabilityInvoker | 函数及规则分别三类实际回执，插件组合结果 |
| E2E-3 | 复杂流程可靠 | 真实资金/库存/通知副作用 | 新建设备维修审批场景；自动通过、拒绝分支、人工批准/拒绝；工作流真实终态且不可重复执行 | 同上、现有 workflow/approval API 和 worker | 服务端审批权限、精确发布快照、持久任务/幂等 | 工作流运行和审批审计、真实终态 |
| E2E-4 | 从实现到插件开发、发布 | 仅生成清单或 mock 模型 | 每场景真实编程 AI 生成源码、审查后定版、公开发布并安装；安装后的 MCP 能调用精确发布 | 编程 AI、插件 review/publication、受信 adapter、独立安装目录 | 源码/产物 hash、CAS、人工确认、技能只读受信包 | 源码及产物身份、实际安装和协议调用报告 |
| E2E-5 | 平台正常可靠 | 用三场景声称全平台绝无缺陷 | 校验 REST/MCP 一致性、幂等、错误输入、跨场景拒绝、凭据撤销；发现缺陷先复现再修复 | 现有协议入口、按需局部修复与对应测试 | 主体/租户/ACL、凭据只在内存、HTTPS 验证、不改默认值 | 真实 PostgreSQL、浏览器、必要回归/构建 |

影响图：用户授权 → 场景列表/建模助手 → 当前场景候选治理 → 不可变 Release → CapabilityInvoker / WorkflowRun / 审批 → 编程 AI 工作区 → 源码审查与真实业务回执 → 插件产物 → publication/独立安装 → REST 与安装后的 MCP → 回执/审计。

先复用现有应用链路。新增行业例子仅位于本验收文档、fixture 和脚本；无预期数据库结构变化。若实际阻断需要代码修复，先在下表追加必要消费者与失败复现。数据库与外部服务仅由既有 Settings 消费，不读取或输出 `.env` 内容。临时 API key 仅绑定本次场景与当前已验证账户，read/invoke 最小 scope，内存保存，验证后撤销。

## 固定业务验收

1. **办公采购分流**：采购申请包含金额、类别、资料完整标记。金额不超过 100、类别为 standard/priority 且资料完整时规则通过。50 通过，100 边界通过，101 业务不通过；资料不完整不通过，非法输入另按 Schema 拒绝。规则判定“不通过”仍是成功执行，不能冒称系统失败。
2. **供应履约评分**：quality 与 timeliness 为非负数；分数为 `quality × 2 + timeliness`，评分规则为 score ≥ 80 且资料完整。30/20 → 80，0/0 → 0，有限大数输入溢出必须产生安全失败回执。80 达标、79 未达标，插件用函数真实输出再调用规则，不伪造分数。
3. **设备维修审批**：估算金额不超过 100 自动通过，超过 100 且不超过 1000 等待 owner/admin 审批，超过 1000 输出超预算拒绝。覆盖 50、100、101 人工拒绝、101 人工批准、1000 上边界人工批准、1001 超预算；审批工作流必须达到终态后才能定版插件，不以排队回执代替完成。正式实现固定 typed input 为 `maintenance_request.estimated_amount`，通过本体 input_binding 校验；输出 `decision / reason / estimated_amount`，decision 为 `auto_approved / approved / over_budget`。这将初始描述的金额与分流结果明确为可执行的本体契约，不改变阈值及审批行为。

每个被插件选中的能力有成功、边界、失败三个不同实际 invocation。业务不通过允许 succeeded 回执但必须断言结构化结果；数值异常为 failed；人工拒绝为 WorkflowRun rejected，enqueue invocation 状态单独记录。额外验证非法 Schema、重复输入重放、同幂等键不同输入冲突、不同场景访问拒绝和撤销后拒绝。

## 当前执行状态

三个新场景已由真实登录浏览器创建，实际完成真实模型建模、候选治理、正式能力启用与不可变发布、编程 AI 生成源码、人工审阅定版、插件发布、独立宿主安装及正式协议调用。账户为当前浏览器确认的 mrbug，工作区为 mrbug 的工作区；每次应用调用均恢复并校验实际主体、租户及场景权限。

## 必要修复与验收结果

| 问题 | 必要范围 | 旧行为复现 | 预期验收 |
|---|---|---|---|
| 审批节点在 AI 归一化时静默丢失 approver_roles / approver_user_ids / requires_evidence | workflow_authoring_data、其现有 compiler 消费者与版本/指导、test_workflow_authoring_data | 新增四条回归：角色/用户/evidence 丢失；非法系统角色、超长用户 ID、字符串布尔未拒绝；旧实现 4 failed / 19 passed | 复用既有 ApprovalAudience 封闭校验；正确保留合法配置，非法配置拒绝；真实维修场景审批与终态 |
| 候选批量重校验已持久完成，但 HTTP response 把内部 candidate_results 传入不接受该字段的 DTO 导致 500 | scenarios 既有 revalidate-batch 协议适配、对应响应回归 | 两个真实场景在同入口触发 extra_forbidden，闭包治理服务本身已返回真实结果 | 显式映射既有公开 counts / eligible_draft_ids；内部逐候选结果继续供 compiler 使用，不放宽 extra='forbid' 或泄露内部结果 |
| 无附件的普通 /chat 与 /chat/stream 忽略 construction_resolution，未校验持久 proposal revision | construction_resolution_service 现有服务、assistant 两个入口薄编排、对应回归 | 当前只有附件分支执行 prepare_resolution；正常零数据澄清未消费已声明 DTO 字段 | 两入口在模型/任务写入前复用同一主体/场景/proposal/CAS 校验，陈旧与无权请求拒绝；真实重新建模用修复后的入口 |
| 候选写入中断的恢复分支丢弃原异常，无法定位真实建设阻断 | 独立 compilation_failure_diagnostics、assistant 既有恢复边界、安全诊断回归 | 真实任务只保存 DRAFT_MATERIALIZATION_INTERRUPTED；catch 不保存异常类型或代码位置 | 保留受当前 lease/主体校验的审计，只有有界异常类型及文件名/函数/行号，不保存 exception 文本、SQL、locals、凭据或客户输入 |
| materialization 关闭 lineage 后未 flush，autoflush=False 导致下一次 SQL 用旧 OPEN 状态选到已关闭的对象 | scenario_model_draft_service materialize 事务边界、隔离真实 PG 回归 | 维修真实 safe audit 为 CandidateRevisionConflict（生命周期关闭）；隔离 PG 复现 | 在返回及后续统一重校验前 flush 合法 pending 决策；已关闭候选保持关闭，用户 revision 不被覆盖 |
| rule 候选正式化保留 medium 等编译器已支持的 severity alias，写入的正式值与公共 DTO 不一致 | candidate_governance_service 既有 canonical rule、定向回归 | 采购真实晋级得到 medium，普通 RuleIn 编辑拒绝 | 复用编译器既有确定 alias 归一化；未知 severity 拒绝，原候选 provenance 不变 |
| 已声明的场景模型 resolution 无法定向 workflow，orchestrator 把自身支持的子范围拒为不兼容 | assistant_orchestrator 现有范围映射、authoring route 回归 | 维修 CAS resolution 被拒为范围不一致，未生成 job | 广义 scenario_model 复用统一 compiler 已声明的子范围；保留具体范围、低置信度和只读门禁 |

本体候选真实校验明确要求唯一主键和标题。本次新增来源澄清：采购 request_id、供应 evaluation_id、维修 request_id 均为 string 主键兼标题，不以数值金额作为主键。采购/评分规则只校验实际消费字段；维修工作流的本体 input_binding 消费完整维修申请，因此其 typed input 包含 request_id 与 estimated_amount。澄清通过新的明确建模来源和真实模型候选进入治理，不手写 ORM 补数据。

真实部署只读检查已通过：PostgreSQL Schema 为当前版本、运行角色无 Schema CREATE/危险角色权限；MinIO、Redis 健康。本机 HTTPS 验收桥证书校验通过，无凭据外部请求实际返回 401。

## 最终实际结果

| 场景 | 核心验收 | 应用调用 | REST | 安装后 MCP | 安装后 AI 客户端 |
|---|---|---:|---:|---:|---|
| 办公采购分流 | 50 / 100 通过，101 / 资料不完整不通过 | 4 | 4 | 4 | 4 例、4 个实际回执 |
| 供应履约评分 | 30 / 20 → 80，零分，有限数溢出安全 failed；阈值、未达标、不完整 | 7 | 6 | 6 | 4 例、7 个实际回执；函数失败只调用函数 |
| 设备维修审批 | 50 / 100 自动通过；101 人工拒绝与批准；1000 人工批准；1001 超预算 | 6 | 3 | 6 | 真实安装 MCP 调用完整六分支；未额外执行 examples 模块 |

共核对 **57 个不同 invocation ID**，重放复用原 ID，不计新执行。三个插件均 `published / available / local_test`，已通过真实 Claude Code 2.1.208 的结构校验、marketplace 注册、插件安装及安装后调用。产品安装器为固定受信版本，AI 编写的客户端和 Skill 随审阅后的不可变产物安装。采购和评分的定制客户端在独立进程实际导入运行，回执重新经 REST 查询核对，数据库审计证明没有未报告调用；评分没有在客户端重算业务分数，溢出时没有调用规则。

三个场景均通过跨场景凭据拒绝 404、非法输入 422、同幂等键更换输入 409、陈旧 Definition hash 409、REST 与 MCP 重放同一 invocation，以及撤销凭据后真实 401。维修的三次新人工交互使用实际 advertised code / revision / audience，终态后旧审批回复均拒绝。维修初始存储 enqueue 回执为 succeeded，不等于完成；实际 WorkflowRun 与投影回执分别核对到 succeeded / rejected。超预算是 succeeded + `over_budget` 业务结果，未伪称系统失败。

原始 AI 候选与失败轮次均保留。采购和评分规则经普通候选 PATCH + expected_revision 作明确语法修正后重校验和原子晋级；旧助手摘要仍含历史缺口时，记录人工治理与当前精确正式定义审阅，不把旧摘要伪装为自动完成。维修最终三份 AI 源码经过正常编辑 CAS 的机械修订，保留原源码、人工 diff、actor 与 hash，再经确定性校验和真实业务回执定版。依赖的两条维修规则保存在冻结发布闭包内，未冒称它们作为插件选中能力分别做了三类外部调用。

| 场景 | Release | 插件产物 | Publication | 详细证据 |
|---|---|---|---|---|
| 采购 | `339b9ec22c8945419b95573317d7ceb6` | `aa87a60e413ccdbd2c80376de1f97162` | `807c5278d287598cf8a53a5815a1ce15` | [应用](multi-scenario-artifacts-2026-10-06/purchase.json)、[协议和客户端](multi-scenario-artifacts-2026-10-06/protocol-purchase.json) |
| 评分 | `2cc9b32f732c4cafaaeb4f22811bf990` | `6ea84bd0954f61f9195ad2660639a879` | `ffc74d9a3af9f9c6985f6ed3c4372cdb` | [应用](multi-scenario-artifacts-2026-10-06/score.json)、[协议和客户端](multi-scenario-artifacts-2026-10-06/protocol-score.json) |
| 维修 | `9ce9f445471145e9a60899e66fa2a772` | `8877881fd706c5b0e5b7cf367931e02d` | `f9b0674199e930d12a5935867a642cac` | [应用](multi-scenario-artifacts-2026-10-06/maintenance.json)、[协议和审批](multi-scenario-artifacts-2026-10-06/protocol-maintenance.json) |

[机器可读汇总](multi-scenario-artifacts-2026-10-06/summary.json) 保留数量及证据引用，不保存密钥。所有临时外部凭据已正常撤销；未把原文写到文件、参数、安装源或日志。

## 修复与回归命令

本次修复上表七处必要缺陷，无数据库结构或迁移变化。审批字段旧回归 4 failed → 23 passed；候选批量响应旧回归 2 failed → 25 个相关测试 passed；普通 /chat 与 SSE resolution 旧回归 2 failed → 21 个相关测试 passed；severity 旧回归 3 failed / 1 passed → 42 个相关测试 passed；scope 旧回归 3 failed / 12 passed → 36 个相关测试 passed。生命周期保存顺序的隔离真实 PostgreSQL 脚本在旧实现明确失败于“返回前数据库仍 OPEN”，修复后通过且保留关闭状态与旧用户 revision。诊断回归验证异常文本、SQL、凭据与 locals 不进入审计。

最终执行：

```powershell
$env:PYTHONPATH='E:/实验室/ontology_business/backend;E:/实验室/ontology_business/.tmp-scenario-audit-testdeps'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTHONDONTWRITEBYTECODE='1'
& 'D:/anaconda3/envs/ontology_platform_env/python.exe' -m pytest backend/tests -q -p no:cacheprovider --basetemp .tmp-multi-final-backend2-20261006 --tb=short
& 'D:/anaconda3/envs/ontology_platform_env/python.exe' backend/scripts/verify_candidate_materialization_postgresql.py
& 'D:/anaconda3/envs/ontology_platform_env/python.exe' backend/scripts/verify_postgresql_runtime.py
npm --prefix frontend test
npm --prefix frontend run build
git diff --check
```

后端 **512 passed**（两条既有依赖 warning）；前端 **296 passed**、生产 build 成功（既有 chunk / annotation warning）。最终后端修改后重跑全量；前端源码未因本批场景变动，因此其已通过门禁未无理由重复。Python 3.12.0，Node 24.6.0，npm 11.5.1。隔离 PostgreSQL 按实际 single Alembic head 迁移，自建数据库已清理；只读部署检查验证当前 Schema、ontology_app 最小权限、MinIO 与 Redis 健康。

三个 `scenario_e2e_protocol.py` 命令分别指定真实报告、mrbug、HTTPS `https://127.0.0.1:9444`、临时受信 CA、本次短隔离目录与另一个明确新场景；采购和评分另传 `client-purchase.json` / `client-score.json`，实际结果在对应协议报告。此 HTTPS 桥只代理现有统一应用，未关闭 TLS 验证，也不替代业务内核。

浏览器实际验证创建、正式规则、三个发布详情、刷新恢复、场景切换、返回、编程会话、技能设置、Esc 关闭后焦点返回，以及 390 像素窄屏（documentWidth=390，底部输入完整可见）。未模拟浏览器会话或把直接 ORM fixture 当作新场景。[浏览器记录](multi-scenario-artifacts-2026-10-06/browser.json)、三个发布页截图及窄屏截图保存在本批证据目录。

## 环境边界与已记录限制

本批使用合成输入，未执行外部资金、库存或通知副作用；相应 Action preview / execute 高风险确认验收为 N/A。三场景覆盖规则、函数组合、持久工作流和审批的重要链路，不能证明所有行业、所有故障或负载下绝无缺陷。

验收用 HTTPS 桥已停止，临时私钥已删除。发布演示应用与前端保留运行，便于查看已发布版本；试装配置不等于公网部署。

原实例没有配置插件安装源，发布门禁正确拒绝。本批另启正常应用 `http://127.0.0.1:8002`，仅其进程设置 `PLUGIN_PUBLIC_BASE_URL=http://127.0.0.1:8002` 与项目真实字段 `PLUGIN_ALLOW_LOCAL_HTTP=true`；前端 `http://127.0.0.1:5174` 代理同端口，使用项目已有允许 origin。未读、改写 `.env` 或修改默认配置。此发行是本机试装，远程安装仍需真实公开 HTTPS 部署；原 3099 / 8001 实例保持原配置。

首次隔离安装目录过深导致 Windows WinError 206，改用 `.tmp-ip3` / `.tmp-is3` / `.tmp-im3` 及场景 ID 短目录后真实成功。实际普通默认安装路径树最长 231 字符，本次未修改产品安装器或 Windows 系统长路径设置。评分安装曾被自动审批依据旧“待审阅”状态拦截；核对最新实际审阅 hash、正式产物、published / available 与公开包 SHA 后重试获准，未绕过审阅或发布门禁。
