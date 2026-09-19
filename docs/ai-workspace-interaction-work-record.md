# AI 工作区交互调整（2026-09-19）

本记录约束本次实现和验收。以当前用户请求及两张布局草图为准；引用任务仅用于了解既有资料库、场景隔离和临时附件行为。开始前已检查 git status，保留全部既有未提交修改。

| ID | 原文目标 | 非目标 | 可观察验收 | 影响层/文件 | 保持不变项 | 测试证据 |
| --- | --- | --- | --- | --- | --- | --- |
| W1 | 左侧业务内容 tabs、右侧持续 AI 对话 | 强迫人工填写业务定义表单 | 目标系统、价值、ER、血缘、流程等按页签展示；空态引导对话，人工预览/采用/调整结论 | BusinessDistillation、distillation 展示组件和样式 | 场景、会话、临时附件、发布版本隔离 | 前端行为测试、构建、浏览器 |
| W2 | AI 基于资料推导并主动提问形成共识 | 文案或选择框冒充工具执行 | 调查工具可选择且服务端执行边界生效；问题和结论有可观察状态；已配置模型/技能/MCP按职责使用 | 资源 DTO/API、worker、调查工具、两类对话配置 | 服务端权限、受信执行、不可逆副作用门禁 | 资源拒绝/执行/问答行为、后端回归 |
| W3 | 全局设置移到 side-footer 图标和多 tab 弹窗 | 设置成为另一个主菜单 | 当前页可打开设置并管理模型、工具、技能、MCP，关闭返回原上下文 | App、router、platform 组件和原配置入口 | 当前工作区权限与配置范围 | 导航/弹窗测试、浏览器 |
| W4 | 产物模板附件放资料库 | 删除历史模板或改发布运行契约 | 无独立模板菜单；资料库支持保存/引用规范附件，旧入口有明确去向 | router、资料库说明及旧模板入口 | 历史模板 API、已发布定义 | 前端入口回归 |
| W5 | 智能业务顾问只留场景建模页，各 AI 职责独立 | 蒸馏 AI、验证 Agent 混用状态或工具 | 仅 scenario-detail 挂载建模顾问；蒸馏页仅有蒸馏对话；验证页不出现建模顾问 | App、GlobalAssistant、蒸馏配置组件 | 验证 Agent 正式执行与权限 | 路由、前端和浏览器 |

影响图：导航/业务页签/对话配置 → 既有 API 与资源选择 DTO → 调查编排及受信执行适配 → 已有会话 JSON/lease/审计 → 测试与本记录。全局设置复用现有资源管理 API。资料库继续保存长期资料和正式交付物；临时附件不自动入库。无授权删除历史数据，无对外能力发布范围扩张。持久结构若出现必要变化须先扩展本记录并新增 Alembic revision。

验证环境：Python 3.12.0（既有 ontology_platform_env）、Node v24.18.1、npm 11.16.0。未改依赖和 lockfile。


## 必要范围补齐

- W1/W2：移除旧业务表单后，原发布契约仍要求人工 decision/reason，已补保存前的引导式决定组件。复用既有 CAS 保存与发布；没有选择不能确认，发布失败保留已保存决定并可按同版本重试。
- W2：建模顾问原有模型参考只拼资源名称，部分编译分支会忽略选择。补独立有界方法/工具目录参考，作为编译上下文与身份的一部分；工具目录仅描述可建能力，不证明业务工具执行。
- W2：MCP 读取补安全持久证据回执与后续澄清/发布追溯；增加中立 MCP 响应流字节限制以在 SDK 解析前拒绝超限内容。没有新增数据库列或对外执行权限。

## 最终验证（2026-09-19）

| 命令/检查 | 实际结果 |
| --- | --- |
| `npm --prefix frontend test` | 180 passed，0 skipped |
| `npm --prefix frontend run build` | vue-tsc 与 Vite 构建通过；仍有既有大 chunk 和 VueUse PURE 注解警告 |
| Python 3.12：`python -m pytest backend/tests -q -p no:cacheprovider` | 178 passed，72 skipped；默认未启用的真实依赖测试如实跳过 |
| 下列蒸馏隔离 PostgreSQL 套件 | 52 passed，包含 23 项真实 PostgreSQL 测试 |
| 启用 PostgreSQL 后：`python -m pytest backend/tests/test_assistant_resource_selection.py -q -p no:cacheprovider` | 6 passed，包含 4 项真实 PostgreSQL 测试 |
| `git diff --check` | 通过 |

上述 Python 命令使用 `D:/miniconda3/envs/ontology_platform_env/python.exe`，先设置 `$env:PYTHONPATH=(Resolve-Path backend).Path`。PostgreSQL 两组命令另设置 `$env:RUN_POSTGRESQL_INTEGRATION_TESTS='1'`；fixture 自行建立、迁移、清理隔离数据库，不写业务数据库。蒸馏合并命令为：

```powershell
python -m pytest backend/tests/test_distillation_conversation_postgresql.py backend/tests/test_distillation_conversation_review.py backend/tests/test_distillation_resources.py backend/tests/test_distillation_conversation.py backend/tests/test_business_distillation.py backend/tests/test_mcp_bounded_transport.py -q -p no:cacheprovider
```

后端验证覆盖所选资源停用/跨租户拒绝、调查工具执行选择、MCP 回执伪造拒绝、旧版本证据身份兼容，以及真实 PostgreSQL 的读取 → 人工澄清 → 下一轮读取冻结证据 → 提出建议 → 人工采用 → 发布追溯。方法参考测试覆盖普通对话、本体/映射/工作流及完整编译的各策略与恢复，模型失效明确失败。

前端修复均有旧行为失败复现：保存人工决定失败后安全恢复对话入口；切换方向同步旧默认理由并保留自定义理由；并发项目/版本/字段变化不会被失败回滚覆盖。无权、冲突、取消与迟到响应由对应 API/composable 和服务端行为测试覆盖；没有把源码扫描等同于浏览器行为。

浏览器使用本地 Vite 5176 → 隔离后端 8016，后端命令为 `python backend/tests/run_distillation_browser.py --port 8016 --frontend-port 5176 --seed-project --scripted-distillation-llm`。通过真实登录、会话/Origin 校验与 PostgreSQL，模型为显式合成脚本。实际完成：

- 1440×960 左侧七类结论与右侧持续对话；390×844 结论/对话切换，ER 图/文字、方向键和输入区无遮挡。
- 页签问题追加到已有输入并聚焦，保留草稿、不自动发送；首次发言前可加载服务端资源目录并选择模型。
- AI 调查、问题选项、自由输入、人工回答、阶段建议预览、确认采用、刷新后恢复记录。
- 人工决定未选择时不能保存；选择后保存不可变版本，重开切换决定会同步默认理由；资料库可打开并列出七项产物。
- 场景建模页独有智能业务顾问及其资源设置；蒸馏、资料库、场景列表、验证中心没有建模顾问入口。
- 全局设置在当前页面弹出；模型编辑内层弹窗、Esc、MCP 页签均可用。场景 `stage=ontology` 在打开设置、后退、前进与刷新后保留，正确恢复设置页签。
- 最后页面控制台 error 列表为空。验收后恢复浏览器尺寸并关闭测试页；测试进程已退出且两个端口无监听。最后一轮进程会话提前结束留下隔离数据库，按本轮合成用户身份确认其归属、排除配置业务库并验证零活跃连接后完成清理，已确认该测试库不存在。

限制：没有调用真实外部 LLM 或 MCP 服务；MCP 网络返回使用测试替身，不能据此声称真实连接可用。此次没有执行 MinIO 上传链路、只读部署健康检查或 Alembic 往返脚本；本次未改存储结构，隔离数据库启动已运行当前迁移。没有新增依赖或改写 lockfile，原有未提交工作保留。
