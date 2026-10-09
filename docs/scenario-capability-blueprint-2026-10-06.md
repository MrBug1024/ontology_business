# 场景能力画像与插件交付规范

本次需求是明确资料、业务蒸馏、本体模型、能力建设分别贡献什么，并使插件编程、产物与发布遵循明确的能力边界及标准。此文档是本功能的需求账本与实现说明；根 AGENTS.md、公共 DTO、确定性校验与实际回执仍裁决权限、执行和完成状态。

## 需求账本

| ID | 原文目标 | 非目标 | 可观察验收 | 影响范围 | 保持不变 | 验证 |
|---|---|---|---|---|---|---|
| B-1 | 明确建模资料与业务蒸馏的贡献 | 自动将资料、历史对话变为正式输入 | 用户和编程 AI 均获得资料→候选治理→冻结发布的职责说明；固定发布上下文不受当前草稿变化影响 | 独立场景画像 contract、编程上下文、说明组件/文档 | 资料仅用于理解与候选；正式输入按本次调用提供 | 冻结语义/敏感与数据边界回归、浏览器 |
| B-2 | 明确本体和能力建设的贡献 | 给每个场景强制添加所有能力种类 | 从明确 Release 读取本体语义与实际能力范围，显示插件选中项和场景可调用项的关系；不把本体或事件自动当调用工具 | Release 只读投影、既有 catalog、typed DTO/UI | 统一执行、零数据能力、权限与不可变发布 | 实际组合场景和缺失/零本体测试 |
| B-3 | 插件包含什么、具有哪些能力 | 在插件复制业务算法或自动装入编码扩展 | 场景画像进入编程 AI 上下文及新的受保护交付参考；入口覆盖选择的能力，明确输入、输出、确认、异步、回执与缺口 | 编程契约/产物、受信 Skill 方法、项目静态校验 | 已发布旧产物不重写；编码 Skill/MCP 与运行依赖分离 | 产物/身份/源码验证回归 |
| B-4 | Skill 等遵循什么标准 | 将内部限制冒称外部标准，新增多宿主或任意执行器 | 明确 Agent Skills 格式、Claude Code 宿主布局、MCP 协议与平台 profile 四层；官方链接可查；方法说明和服务端校验一致 | 标准说明、Skill contract/指导与必要校验 | 外部标准不授予业务权限；当前宿主仅 Claude Code | 官方来源核对、合法/拒绝路径测试 |
| B-5 | 逐步完善整个平台能力 | 再创建或变更既有业务发布、重构通用内核 | 页面对用户可见，AI 实际能发现相关信息；按变化运行目标/全量测试和浏览器验证，诚实记录剩余范围 | 既有插件入口与独立组件、tests/docs | 保留已有用户改动、无 DDL/.env/凭据变化 | 前后端门禁、浏览器、git diff --check |

影响图：用户目标 → 插件开发的明确 Release → 现有 ACL/catalog/冻结 Snapshot → 独立能力画像与标准 profile → 公共上下文 DTO / 编码 AI / 受保护产物参考 → 可见职责与覆盖说明 → 确定性校验及测试。数据库结构、真实业务记录、外部执行工具与运行数据解析不应改变。

## 各阶段提供的价值

| 阶段 | 提供什么 | 何时产生可运行能力 |
|---|---|---|
| 建模资料 | 业务事实依据、术语、现状流程、约束、待澄清问题和可追溯来源 | 资料本身不执行，也不自动成为调用数据 |
| 业务蒸馏 | 将资料转为目标、角色、业务任务、决策/规则、流程、验收与缺口的候选规格 | 经人工治理、确定性验证和正式发布后，选定实现才可调用 |
| 本体模型 | 对象、属性、身份、关系和业务语义；为 typed input/output、关联与流程提供共同语言 | 本体使能力语义和输入绑定明确，不能单靠对象图完成业务 |
| 能力建设 | 函数计算、规则判定、Action 副作用、Workflow 编排、事件语义与运行端口；定义权限/确认/证据 | 精确启用 Release 的受支持能力经统一 invoker 执行 |
| 插件 | 宿主可安装入口、Skill 方法、客户端适配、契约参考、依赖与安装说明 | 调用冻结 Release，携带本次输入，经服务端生成真实回执 |

场景按业务需要选择这些元素。零数据能力或没有对象图的纯计算仍可发布；不能把“能力完整”解释为所有种类、所有端口和所有外部连接必须齐全。

## 一个场景要回答的业务问题

| 维度 | 应明确的内容 | 验证依据 |
|---|---|---|
| 目标和范围 | 服务谁、解决什么问题、预期结果、明确不处理什么 | 审阅后的业务目标与精确发布语义；蒸馏目标未被正式采用时仍为候选 |
| 业务语义 | 对象身份、属性约束、对象关系及其含义 | 冻结本体与 typed Schema，而非物理表列或模型猜测 |
| 输入与输出 | 必填字段、嵌套结构、受管数据端口、输出字段和状态含义 | 同一版本的封闭契约；每次调用的当前输入 |
| 计算与决策 | 函数怎样提供结果，规则判定怎样解释 | 正式能力契约、受信 Provider / 声明式实现及实际回执 |
| 操作与流程 | 副作用、顺序、分支、人工审批、事件关联、失败终态 | 工作流与 Action 定义；审批和统一执行记录 |
| 依赖与边界 | 哪些是用户直接调用入口，哪些只支撑已选入口，是否需要正式连接 | 当前主体可发现的 catalog、冻结依赖闭包、正式绑定 |
| 治理与版本 | 候选是否被采用，发布是否启用，旧调用和旧插件如何保持可追溯 | 服务端治理、不可变 Release、revision / hash 与审计 |
| 验收与交付 | 成功、边界、拒绝、重试、等待、确认、撤销，以及给用户何种成果 | 真实回执、业务验收、源码审阅、宿主安装与明确发布 |

这八个维度是需要回答的问题，不是八个强制数据库实体。没有外部副作用的场景无需添加 Action；没有外部输入的纯计算无需 DataSource；没有事件的同步规则无需人为添加 event。

本体关系帮助说明“申请由谁提出”“对象之间如何关联”，但对象或关系不是新增可执行 MCP 工具。函数提供计算，规则提供判定，Action 提供受治理操作，Workflow 组织这些能力。事件可提供被发布定义中的事件语义或依赖；不能因为存在事件名称就宣称插件支持主动订阅、自动执行、发通知或写外部系统。

## 插件和编码 AI 各自负责什么

插件提供业务任务的可发现入口与执行方法。入口 Skill 应说明适用目标、输入追问、选择哪个已发布能力、结果含义、必要确认、异步等待、失败/未知结果和升级办法；按需拆成任务 Skill 与 references，不把整个平台手册或实际客户数据塞进入口。

客户端适配将用户本次输入送到固定场景发布，保留结构化业务结果、invocation_id 与证据。插件可以组合函数和规则的调用及交付步骤，但不能在本地重写规则、推算平台未返回的业务结果、替换 Release 或绕过审批。Workflow 的完整执行轨迹与实际终端业务输出要区分，排队和运行中不是已完成。

当前交付包包含宿主清单、入口 Skill、MCP 适配配置、固定契约参考、受信客户端、依赖、说明和校验清单；新 profile 增加服务端生成的场景能力画像。AI 可按任务编写 Skill、references、受限客户端 scripts 和合成 examples。Skill 的方法说明、MCP 的调用协议、插件的版本化安装包装职责不同。

编程 AI 的基础能力包括读取与检索项目、读取明确场景契约、生成可审閱的源码、确定性项目检查、根据反馈修正、变更说明和保留失败草稿。当前基础工具正是 inspect_plugin_files、search_plugin_files、inspect_scenario_contract、validate_plugin_project；它们不等于终端、业务执行或发布权限。

编程设置中安装的受信 Skill 用于开发方法；安装的 MCP 目前用于只读资源理解。它们不会自动打包为运行依赖，也不能由工具返回内容改变业务定义、发布身份或权限。新增更强能力需要单独支持、鉴权、隔离及验收，不能以“装了 MCP”推断任意外部工具都可调用。

## 四层规范

1. Agent Skills：目录与 SKILL.md、YAML name/description、可选 compatibility/license/metadata、渐进披露。它规定方法包格式，不证明方法已执行或授权。
2. 宿主插件规范：当前支持 Claude Code 的 plugin.json、skills、MCP 配置和 marketplace 安装。其他宿主需分别实现和验收适配，不宣称同一包通用。
3. MCP：发现、工具及输入输出/错误协议。工具提示不能替代服务端权限、确认和业务事实。
4. 平台交付 profile：固定场景/Release/Definition 身份；受信 adapter；selected capabilities；不嵌入客户资料或密钥；幂等、审批、结构化回执；源码审阅、真实业务验收与人工发布。禁 hooks/动态 shell、严格文件白名单等是本平台支持范围，不是 Agent Skills 或 Claude Code 对所有插件的规定。

官方来源（核对日期 2026-10-06）：[Agent Skills](https://agentskills.io/specification)、[Claude Code manifest](https://code.claude.com/docs/en/plugins-reference)、[MCP 工具协议](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。版本身份沿用项目既有协议，不以网站更新静默改变旧产物。

## 实现与验收记录

首次编辑前审计发现：既有编程契约已固定场景与输入输出，但未携带本体语义和可调用/选中/内部依赖范围；新任务的详情实际显示全部能力，容易被误解为本次全部交付。此画像以 allowlist 投影补齐，不新增执行内核、运行输入或数据库表。

新增必要消费者：受保护源码参考和 review/export 产物应使用同一画像；新包可增加 references/scenario-blueprint.json，AI 不得编辑。旧工作区缺字段时明确未保存画像，禁止回写其旧文件/hash。现有默认 README 把真实 capability:read / capability:invoke 写成复数，以及 client_contract 把 Workflow 的完整 result trace 误称最终业务输出；两处直接影响合法使用和结果解释，纳入 B-3 的模板/客户端契约与行为回归，不改变权限或执行语义。

兼容边界：既有 adapter_hash 包含旧构建器源码，因此保留旧构建器与无 profile 产物字节。新交付 profile 使用独立构建包装与有版本的适配器身份，新源码进入新身份校验；未知 profile、身份漂移均拒绝。不得通过放宽 hash 校验、重写旧快照或自动重新审阅达到兼容。

新增验证消费者：只读验收脚本读取三个既有工作区的已审阅业务案例，调用同一经典构建服务生成内存中的新规范候选包，核对保护参考与校验清单。此检查不创建工作区、不审阅新源码、不发布新版本、不再次执行业务；不能作为新版本已经发布或实际安装的证明。

### 实际实现

- `scenario_capability_blueprint_schemas.py` / `services/scenario_capability_blueprint.py`：封闭、有界的 `scenario-capability-blueprint.v1`，只投影明确发布中的目标、本体对象/属性/关系和能力语义。覆盖选中入口、可发现项、内部依赖与未选项；属性经过现有读取权限过滤，不对外暴露隐藏依赖身份。支持零对象模型，超预算明确拒绝，不静默截断。
- `plugin_coding_contract.py` / `plugin_authoring_context.py` / `plugin_coding_workspace.py`：新任务和原生契约查看获得同一画像及交付 profile；新工作区保存画像。冻结语义与读取时的权限说明分开，画像不能授予执行权限。
- `plugin_delivery_profile.py` / `plugin_delivery_reference.py` / `plugin_delivery_artifact.py`：新规范使用独立包装器；校验固定身份、选定范围和参考内容，服务端写入 `references/scenario-blueprint.json` 并纳入 SHA-256 校验清单。经典新建包、编码项目、审阅、恢复和导出消费同一构建路径。
- `PluginScenarioBlueprint.vue`：新任务折叠查看目标、本体、能力范围、五个阶段贡献、组成、标准与平台规则；已有工作区在“能力契约”页查看所保存信息，旧工作区明确提示未保存画像。能力勾选与预览同步；默认折叠减少对话区占用，原底部输入框可继续使用。
- 两个受信方法 Skill 使用标准的 `metadata.version`。客户端说明和新模板修正真实 `capability:read` / `capability:invoke` scope；人工源码不自动改写。Workflow 明确输入绑定、声明的输出节点与 `contract_validation="passed"` 后的 `step.result`，整个 `output.result` 是执行轨迹。即使外层 receipt 为 `succeeded`，内层仍等待/运行时也要按原 invocation 有界复读；不能重新提交或宣称完成。

入口 Skill 应包含可发现的 name/description、适用目标、本次输入的追问、明确能力选择、执行步骤、确认/审批、结果解读、等待/失败/未知状态、参考资料读取方式。Skill 指导不等于业务执行；实际权限、确认、幂等和完成证据仍由统一执行服务裁决。Claude Code 当前官方允许更丰富的 hooks/动态 shell 等组件，本平台未支持；严格清单和禁令属于平台 profile。

### 验证结果

本次支持环境为 Python 3.12.0、Node v24.6.0、npm 11.5.1，未新增或升级依赖。

| 检查 | 实际结果 |
|---|---|
| 后端全部测试 | `python -m pytest backend/tests -q -p no:cacheprovider --basetemp .tmp-blueprint-final-20261006 --tb=short`：**550 passed**，两项已有依赖警告 |
| 插件目标回归 | 13 个相关测试文件 **211 passed**；画像/保护参考/旧字节/身份漂移/人工源码/经典错误映射覆盖；错误映射测试先复现旧失败 |
| 前端全部测试 | `npm --prefix frontend test`：**302 passed**；新增画像、勾选范围、旧工作区及公开类型回归 |
| 前端构建 | `npm --prefix frontend run build`：vue-tsc 与 Vite 构建通过 |
| 受信 Skill 格式 | `python -X utf8 …/skill-creator/scripts/quick_validate.py backend/skills/plugin-authoring` 与 `plugin-contract-review`：均 `Skill is valid!` |
| PostgreSQL 实际画像 | 显式账户下 **3 个正式发布 × 目录/已选范围 = 6 个上下文**通过；单上下文 12,050–15,591 bytes，均低于 128 KiB；维修插件 1 个工作流入口，2 个规则依赖 |
| 新规范实际候选构建 | PostgreSQL 只读事务复用原已确认业务案例，三个场景候选包均通过；保护画像与上下文完全一致、8 个文件校验和有效、scope/Workflow 方法说明正确；未存储或发布新包 |
| 三个既有安装包 | 按各自原 SHA 恢复安装包与 Marketplace 材料；身份错误 409、跨租户拒绝、匿名 401 均通过 |
| 部署只读检查 | `python backend/scripts/verify_postgresql_runtime.py`：当前 Schema、最小权限角色、MinIO 与 Redis 健康 |
| 真实浏览器 | 1300×900 与 390×844：键盘展开、实际属性读取、取消勾选后只显示选中项、切换到旧工作区正确提示；窄屏 documentWidth=390，输入框在可视区；未发送编码目标/保存/审阅/发布 |
| 工作区检查 | `git diff --check` 通过；旧构建器未改，未改 .env/迁移/真实凭据，保留任务开始前已有改动 |

后端命令使用 `D:/anaconda3/envs/ontology_platform_env/python.exe`，`PYTHONPATH` 包含 backend 与既有 `.tmp-scenario-audit-testdeps`，并设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`PYTHONDONTWRITEBYTECODE=1`；不是使用不受支持的系统 Python 3.13。

真实上下文查询每次 14–29 条 SQL，已选范围约 28–37 ms，首次冷目录读取约 1,477 ms；这是本机六次读取样本，不是负载或并发性能结论。新画像投影有完整大小预算；本次未修改数据库、worker 或副作用执行内核，因此迁移往返及新多进程压力测试 N/A。

只读验证的可复现入口：

```powershell
python backend/scripts/verify_scenario_blueprint_readonly.py --user '<显式账户>' --release-id '<精确发布>' --workspace-id '<已定版工作区>' --verify-build --output '<本地报告.json>'
python backend/scripts/verify_plugin_publication_readonly.py --workspace-id '<已定版工作区>' --output '<本地兼容报告.json>'
```

画像验证脚本可重复传入多个发布/工作区参数；它拒绝不唯一账户、非本人工作区和不合规发布。`--verify-build` 使用工作区保存的原业务验收与 expected revision，不替换当前 revision，不新增人工确认，不自动重新执行案例。候选构建只检查新规范经典模板，不冒称 AI 新源码已审阅。

证据：[真实 PostgreSQL 与新候选包](scenario-blueprint-postgresql-2026-10-06.json)、[采购旧产物](scenario-blueprint-legacy-purchase-2026-10-06.json)、[评分旧产物](scenario-blueprint-legacy-score-2026-10-06.json)、[维修旧产物](scenario-blueprint-legacy-maintenance-2026-10-06.json)、[桌面](scenario-blueprint-desktop-2026-10-06.png)、[窄屏](scenario-blueprint-narrow-2026-10-06.png)、[旧工作区](scenario-blueprint-legacy-ui-2026-10-06.png)。

独立审阅还核对了错误使用假设：把资料当运行输入、绕过审批、让只读 MCP 写外部系统、将运行中误报为完成，方法说明均明确拒绝。此为方法说明与确定性契约审阅；本轮没有调用真实模型重新生成新 profile 的 AI 源码，也没有新规范安装/发布演练。三个场景的既有真实模型、57 次业务调用、人工验收及安装发布记录见 [多场景端到端验证](multi-scenario-e2e-2026-10-06.md)，不与本次只读验证混算。
