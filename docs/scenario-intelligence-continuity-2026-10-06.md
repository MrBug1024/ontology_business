# 场景智能认知贯通与标准插件交付

本轮根据用户对完整场景智能能力、对话使用体验和 OpenAI/Claude 标准插件交付的要求，核对现有实现后补齐主链路。此前工作区已有大量未提交改动；本轮保留，不重写或提交这些工作。

## 需求账本

| ID | 原文目标 | 非目标 | 可观察验收 | 影响层/文件 | 保持不变项 | 测试证据 |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | 蒸馏 AI、业务顾问天然知道当前场景资料与已采用的业务认知，把做什么、怎么做、不做什么、为什么、验收聊透 | 不把未采用建议、历史资料自动变为正式能力或运行输入 | 无须重复粘贴已保存目标；可见当前认知及交接是否缺失/过期；授权资料目录有界且可按需读取；无权拒绝 | 场景认知只读 DTO/service、蒸馏上下文、顾问上下文、现有业务蒸馏 UI/API/type | 人工采用/交接、tenant/ACL、完整冻结编译、revision/CAS、明确运行输入 | 目标回归、前后端全量、浏览器、真实 PG 只读检查 |
| R2 | 所有阶段能够解释场景业务认知与模型/能力建设的关系，用户知道下一步 | 不全站重写、不新增另一套本体或执行内核 | 业务蒸馏入口展示目标/边界/验收、资料和交接状态；提供现有交接/顾问入口；窄屏与键盘可用；切换场景旧响应不能覆盖 | 蒸馏领域 component/composable、API/types、现有页面薄编排 | 现有设计系统、对话草稿、取消/重试、服务端状态权威 | 前端行为测试、构建、浏览器 |
| R3 | 将场景能力按 OpenAI、Claude 官方标准插件化 | 不声称格式检查等于真实宿主安装、官方目录审核或公网发布 | 新项目可明确选择宿主；生成该宿主标准 manifest/MCP/Skill/安装说明；受保护固定场景/Release/Definition；旧包按原 SHA 恢复 | 插件 host DTO、交付 profile/artifact、源策略与安装器、插件页面 types/UI、tests | Skill/MCP 复用同一 invoker；无密钥/客户数据；人工审阅/验收/发布；历史不可变 | 宿主格式/安全/恢复回归、前端构建、可用真实环境验证 |

## 影响图

`R1/R2 -> 场景业务蒸馏/智能顾问 -> 已采用场景认知只读投影 -> 现有 DistillationScenarioState/Publication 与授权资料库 -> 有界按需调查 -> 目标测试/浏览器`

`R3 -> 插件开发宿主选择 -> 同一固定发布 authoring contract -> 按宿主生成受信 manifest/MCP/Skill -> 现有审阅/业务验收/发布与安装 -> 格式/恢复/权限测试`

不会改变数据库结构、能力业务算法、正式运行输入解析、统一调用内核、现有已发布 Definition 或旧插件字节。当前认知可辅助讨论；只有人工交接的不可变资料进入正式资料编译。认知缺失、停止、未决和交接过期必须明确呈现，不能由 AI 文案宣告建设或执行完成。

## 官方标准核对

2026-10-06 已实际打开 [OpenAI 插件打包文档](https://developers.openai.com/plugins/build/plugins)。文档提供 portable Agent Plugins 根 `plugin.json`/`mcp.json` 格式，以及 `.codex-plugin/plugin.json` 兼容布局；OpenAI 专属配置与其他宿主规范分别处理。Claude Code、Agent Skills 与 MCP 使用各自官方标准。最终实现和验收范围在下方追加，不能用方案描述代替运行证据。

## 实现与验收

最后复查补充两项必要消费者，仍属于 R1/R2：排队轮次/每次模型调用前重新校验冻结资料目录的当前授权和元数据，撤销或变化时停止；从建设入口打开顾问时保留用户已有未发送草稿。对应验收分别为权限撤销后不会调用模型、已有草稿不会被入口提示覆盖。

### 已落地的纵向链路

- R1：新增 `ScenarioDiscoveryContextOut` 与 `scenario_discovery_context`，只读汇总已采用目标、原因、非目标、成功标准、待澄清问题、现状/目标流程、历史案例和当前授权资料目录。`GET /business-distillation/scenario/{scenario_id}/context`、普通/SSE 顾问上下文与蒸馏轮次使用同一投影；目录每次最多 20 条、流程每种最多 12 节点、案例最多 5 条，对话摘要上限 18,000 字符。目录明确 `content_read=false`，正文仍通过已有受控工具按需调查。
- R2：场景页增加独立认知面板，说明缺失/过期/有效交接与服务器返回的建设理由。继续建设切换到本体并打开现有顾问，不自动发送或执行；成功采用结论、交接或资料变更后刷新。API/composable 使用请求取消、代次和场景身份校验；无权清除上下文，失败可以重试。组件行为测试覆盖草稿、导航取消/重复、资料部分成功、切换竞态及清理；真实浏览器另行核验了正常/空状态、导航、草稿与桌面/窄屏交互。
- R3：新建插件项目可选择 Claude Code 或 Codex，选择改变即重载对应规范；已有项目及定版材料保持固定宿主。新增独立 Codex v2 交付 profile、artifact/distribution/installer 适配，不修改旧 Claude 构建及安装器的 hash 输入。包中继续冻结场景、Release、Definition、业务画像和已选能力，复用受信 MCP 客户端；宿主差异不形成新的业务执行内核。
- Codex 使用 `.codex-plugin/plugin.json` 官方兼容布局、Skills 和 `.mcp.json`；本地市场索引位于 `.agents/plugins/marketplace.json`。实机确定 MCP 使用 `args=["server.py"]`、`cwd="./"`，仅 `env_vars` 转发 `SCENARIO_MCP_URL`/`SCENARIO_API_KEY` 两个名称，包中没有值。安装器保留原包校验、独立解释器、安装锁和路径边界，完成源注册及 `codex plugin add` 后才记录安装成功。
- 新增可选受信 `plugin-codex-authoring` 编程方法 Skill 1.0.0，原有方法及冻结版本保留。没有改动数据库、迁移、正式运行输入来源、行业 Provider、正式发布门禁或统一 `CapabilityInvoker`。

### 官方规范与平台约束分开

[OpenAI 官方打包规范](https://developers.openai.com/plugins/build/plugins)同时提供推荐的 portable Agent Plugins 根清单布局和 Codex 兼容布局。本轮交付明确属于 Codex 本地兼容插件，未实现通用 portable 包、ChatGPT 网页分发或官方目录提交。[Claude Code 规范](https://code.claude.com/docs/en/plugins-reference)由原 Claude 适配器消费；[Agent Skills](https://agentskills.io/specification)和[MCP 工具协议](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)分别约束技能和工具协议。

固定场景及人工启用发布、候选代码允许范围、禁用 hooks、业务案例回执验收、人工定版与发布属于平台治理要求。上述官方规范本身不证明业务算法完整、权限正确或执行成功。更新插件必须产生新的人工审阅版本，不能修改原安装包。

### 实际验证

环境：Python 3.12.0、Node 24.6.0、npm 11.5.1；本机使用受支持 Python 环境，未新建 venv 或改 lockfile。运行依赖已有；pytest 8.4.2 作为临时测试依赖安装，未加入生产依赖。

| 验证 | 实际命令/操作 | 结果与边界 |
| --- | --- | --- |
| 后端全量 | `python -m pytest backend/tests -q -p no:cacheprovider --basetemp <本轮专属临时目录>`，`PYTHONPATH` 指向 backend 和临时 pytest 依赖 | **598 passed**，2 项依赖告警；覆盖场景上下文、权限撤销、蒸馏、顾问、共享执行、插件及历史恢复 |
| 前端全量 | `npm --prefix frontend test` | **320 passed**；无失败/跳过；草稿保留回归已先在旧行为复现失败 |
| 类型与生产构建 | `npm --prefix frontend run build` | 通过 vue-tsc 和 Vite；存在依赖注释和现有 >500 kB chunk 提示，未扩大本轮到打包重构 |
| 真实 PG 场景投影 | `python backend/scripts/verify_scenario_discovery_readonly.py --output docs/scenario-intelligence-postgresql-2026-10-06.json` | 3 个现有场景，事务只读并 rollback，零 ORM 写入；1 个有已采用结论/有效交接，2 个无结论/缺失交接；材料归属、范围和正文未读验证通过 |
| 真实 PG 目录重授权 | 只读事务选择有结论且经作者授权的现有场景，调用 `assert_frozen_material_directory`；仅在内存改冻结名称再次检查 | 1 个授权目录通过、陈旧名称返回 409；3 次 warm 查询、15.503 ms，零写入并 rollback；真实数据库的权限未撤销，撤销场景由旧行为会失败的 worker 回归覆盖；见 [证据 JSON](scenario-intelligence-reauthorization-2026-10-06.json) |
| 热路径证据 | 上述投影脚本记录冷调用 SQL 与耗时 | 12/10/10 次查询，59.557/12.493/13.933 ms；只表示 3 个本机样本，不是并发负载证据 |
| 部署依赖与 Schema | `python backend/scripts/verify_postgresql_runtime.py`；`python -m alembic -c backend/alembic.ini heads` | PG Schema current；运行角色无不安全权限/Schema CREATE，MinIO、Redis healthy；实际 single head 与代码一致（运行时读取，不复制为契约） |
| 旧包兼容 | 只读事务经原作者 principal/tenant/场景 ACL，调用 `catalog_query` 和 `restore_artifact` | 5 个现有 Claude 包全部恢复原 SHA；原 v0/v1 adapter 身份未变化，零业务写入；见 [证据 JSON](scenario-intelligence-legacy-artifacts-2026-10-06.json) |
| Codex 真实宿主 | 正式 builder + marketplace + installer 的校验/本地拷贝；独立宿主目录中运行 `codex plugin marketplace add`、`codex plugin add`、`codex plugin list --json` 和 app-server MCP discovery | Codex CLI 0.160.1 安装/启用成功，MCP 初始化并发现真实适配器 5 个工具；原源文件校验不变。合成契约、已有 Python 3.12 解释器，未跑安装器网络下载/依赖 provision 全链路，0 模型轮次/0 业务调用，未改用户宿主配置；见 [证据 JSON](scenario-intelligence-codex-2026-10-06.json) |
| 编程方法技能 | `python -X utf8 <skill-creator>/scripts/quick_validate.py backend/skills/plugin-codex-authoring` + 资源冻结回归 | 格式有效，方法独立发现/冻结/准备；篡改冻结 hash 拒绝，旧方法版本保留 |
| 浏览器 | 经用户授权在实际运行的本机 3099 前端验收；仅页面读取/导航与临时输入，未发送消息/创建项目/执行业务 | 正常认知、缺失交接、刷新、阶段 query、前进后退、键盘 Enter、草稿保留、Codex 宿主与规范切换、历史项目固定宿主通过；390×844 窄屏面板在视口内且无页面横向溢出；临时输入已清理、视口已恢复、测试标签页已关闭，见 [浏览器证据](scenario-intelligence-browser-2026-10-06.json) |
| 差异检查 | `git diff --check` + 本轮新增文件和调用方复查 | 无 whitespace 错误；保留原有用户改动，生成物不交付 |

### 未完成的验收与范围边界

首次浏览器打开被自动审批服务的连续 HTTP 429 拒绝；收到用户明确“授权继续本机浏览器验收”后恢复。5174 原目标未运行，经现有进程/配置核对，使用实际运行的 3099 前端与 8001 后端；未改端口或启动/停止用户服务。浏览器已完成上表范围。真实网络故障/权限撤销/冲突页面、实时模型 SSE 及真实设备上的触控与屏幕阅读器未跑，相关拒绝/竞态由行为回归覆盖；本轮没有为视觉测试修改真实权限或业务资料。

本轮未新增或调用真实模型建设场景、未运行新的真实第三方业务案例、未提交外部市场或 OpenAI 官方目录；不把先前聊天的历史证据冒充本轮全链路验收。没有 Schema/持久状态/数据库角色变更，隔离迁移往返与真实 PG 并发写入测试不适用。本轮的三条链路能够提供持续上下文和可安装的标准宿主材料，不构成所有功能模块、所有使用习惯或 AI 准确率已经达到完整验收的声明。

需求对账：R1 的已采用认知、授权有界目录、统一客户端消费及执行时撤销拒绝已由回归/PG/浏览器分别验证；R2 的状态说明、现有入口、草稿保留和窄屏键盘行为通过上述验收；R3 的双宿主选择、固定发布、标准材料、Codex 实际安装/工具发现及旧 Claude 原 SHA 恢复通过。真实模型质量、完整安装器网络 provision、公开目录和未运行浏览器拒绝态仍保留其独立验收边界。
