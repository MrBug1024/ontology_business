# 场景能力与调用方职责修复

## 需求账本与影响图

| ID | 用户目标 | 可观察验收 | 影响范围 | 保持不变 / 非目标 | 证据 |
| --- | --- | --- | --- | --- | --- |
| C1 | 网页恢复 Markdown，渠道自行选择格式 | 助手标题、列表、表格、代码块在流式和历史消息中正常渲染；用户原文不被去格式 | AgentChat、现有 SafeMarkdown、Agent 提示词 | 原有布局、导航、SSE 协议 | 浏览器新回答/刷新/纯文本验收；分块内容回归通过 |
| C2 | 第三方直接消费业务能力 | MCP 契约明确能力调用与 Agent 调用的区别；结构化 output 保留，纯文本 delivery 只是可选展示 | agent_mcp_server、SDK 文档、回执契约验证 | 既有 API、鉴权、人工发布、确认流程 | MCP 实际协议测试及 Agent/REST/MCP 一致性通过 |
| C3 | 输入和附件由调用方按环境提供、处理 | typed inputs 无需建模数据源；文档内容保持结构化；不把文档草稿当已保存附件 | Agent 提示词、SDK 文档、契约验证 | 在线执行的必要输入、受管引用校验、审计 | 零受管数据输入回归；结构化文档与纯文本摘要同时保留，无伪附件 |
| C4 | 验收发现：空模型回答被标记成功 | 空白且无工具调用的模型结果返回明确错误，经原有 durable turn 路径显示失败和重试 | CapabilityAgentRuntime、既有 worker 错误映射、回归 | 不自动重放能力、不改模型配置或业务结果 | 空流/空白流先失败后通过；worker 稳定错误映射通过 |

影响图：用户消息 -> AgentChat -> 原有 durable turn/SSE -> CapabilityAgentRuntime -> LLM；第三方 Agent -> REST/MCP/SDK -> CapabilityInvoker -> 结构化 Receipt -> 调用方展示/文件工具。仅修复消费边界，不修改业务定义、存储模型或执行内核。

C4 补充影响图：空模型结果 -> AgentRuntimeAdapterError -> 原有 worker 失败持久化 -> SSE/历史错误与重试。浏览器纯文本追加验收时发现，只读查询该合成消息确认 `run_status=succeeded`、回答长度 0、模型 `output_tokens=0`、`tool_count=0`，因此纳入本次范围。

## 诊断

- `AgentChat.vue` 对所有消息使用 `PlainMessage`，主动消除 Markdown；通用 Agent 提示词同时禁止 Markdown、表格等格式。
- Agent 流式链路仍逐段传输原始文本，后端并未缺少所谓专门的 Markdown 流协议。
- 能力回执保留 `output`；`delivery.text` 是另行生成的纯文本投递摘要。将其视作全部业务结果或所有 Agent 的输出规范会丢失结构和渠道自主权。
- 平台已有独立的通用能力 MCP 和 REST/SDK 入口；完整 Agent 接口是另一种可选产品。普通 typed inputs 不要求先写入建模资料或实例库。
- 远程能力执行仍需必要输入；队列恢复、幂等、审计可能保存受保护的运行记录。不能承诺在线执行零数据处理/零留存。严格不出域时，需要受信本地能力包或另行部署的执行环境。

## 依据与边界

- [Palantir OSDK](https://www.palantir.com/docs/foundry/ontology-sdk/overview)：应用程序消费对象、Action 与函数，本体能力与应用界面分层。
- [Palantir 开发工具链](https://www.palantir.com/docs/foundry/dev-toolchain/overview)：Ontology MCP 面向消费本体能力的外部 Agent。
- [MCP Tools 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)：工具输入、结构化结果与资源独立于用户交互形式，协议不要求某种聊天 UI。
- [Agent Skills](https://agentskills.io/specification)：技能包提供说明、可选脚本、参考资料及资产；执行依赖由兼容环境承载。该格式本身不提供平台的权限、事务和审计保证。

场景定义规定业务内容、Schema、规则与状态含义；Agent 决定如何解释结果和组织对话；渠道/宿主决定 Markdown、文件生成、存放位置和分发。若能力本身明确提供平台文件生成，它仍可返回真实的受管附件；内容草稿不是该操作已经完成的证明。

## 验证记录

浏览器使用同一服务的 `127.0.0.1` 地址及已有登录态，`localhost` 是独立 Cookie 域，当前未登录。前后端沿用现有 5173/8001 服务。

- 在用户指定 Agent 内创建独立的格式验收会话。第一个真实回答包含 1 个二级标题、1 处加粗、2 个列表项、1 个表格和 1 个 JSON 代码块，浏览器 DOM 与截图均确认渲染正确；刷新后仍保留，页面无横向溢出。
- 另一次真实回答按请求仅输出“纯文本格式通过”，未出现标题、列表、表格或代码块；较长 Markdown 回答包含 12 个检查项，仍正常呈现。测试会话未出现业务能力执行回执，也未创建附件。
- 观察到真实对话的处理状态到完成状态，后端分块测试确认每个原始 Markdown chunk 与最终正文完全一致。未捕获长回答中途的部分 Markdown 截图，不将最终截图当作中间帧证据。
- 初次纯文本追加验收触发上游空回答，已保留原始验收记录；修复后模型正常回答。没有人为让在线模型故障来验收错误 UI；空结果拒绝和 worker 错误映射使用可控回归验证。
- 后端针对性命令：`python -m pytest backend/tests/test_agent_runtime_adapter.py backend/tests/test_agent_turn_service.py backend/tests/test_channel_interactions.py backend/tests/test_capability_protocol_consistency.py backend/tests/test_agent_mcp_publication.py -q --tb=short`，70 passed。覆盖原始 Markdown 分块、Agent 配置保留、空结果拒绝、结构化业务输出不受纯文本转换影响、无伪附件、统一执行与确认。
- `npm test` 最终 108 passed，更新原有“必须纯文本”断言以符合本次需求；没有新增镜像实现的前端测试脚本，渲染以实际浏览器为主要证据。
- `npm run build` 通过；保留既有大 bundle 和第三方 PURE 注释警告。
- `python -m pytest backend/tests -q --tb=short`：1050 passed、39 skipped、2 warnings，耗时 35 分 29 秒。全量回归在空回答补丁前启动，最终空回答补丁已由上述 70 项针对性检查重新覆盖。跳过项未计为通过，本次未开启额外真实依赖集成开关。两个警告为既有 Pydantic 前向引用提示和 Starlette 测试客户端 Cookie 弃用提示。
- 使用 Python 3.12.0、Node.js v24.19.0、npm 11.16.0；未改依赖或 lockfile。`git diff --check` 通过。
- 页面布局、路由、权限、发布、数据库结构未在本次修改；窄屏设备专项验收和第三方真实微信/文件系统交付未运行。实际调用方环境没有提供，不能以平台浏览器代替第三方接入验收。
