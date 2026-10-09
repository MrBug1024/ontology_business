# 插件编码 AI 对话、配置与场景交付

本次需求来源：用户要求插件开发具备传统 AI 编程对话、底部输入、设置、可安装的技能/MCP，以及围绕场景能力开发遵循标准的可发布插件。关联聊天仅作现状背景；本记录固定本次实现和验收。

## 需求账本

| ID | 原文目标 | 非目标 | 可观察验收 | 影响层/文件 | 保持不变项 | 测试证据 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | 像传统对话 AI / AI 编程模式，底部输入框 | 不替换现有代码编辑器、不重写平台主题 | 新建和已有项目均有可滚动历史/上下文、底部输入、发送/停止、设置；窄屏可完整操作，未提交需求/代码在失败时保留 | PluginDevelopment、PluginIdeStart、PluginBuildSetup、PluginCodingWorkbench、会话组件/composable | 文件树、代码编辑/差异、人工审阅与发布入口 | 前端回归、构建、真实浏览器 |
| C2 | 设置并安装技能、MCP 等更强能力 | 不执行任意租户代码，不把 MCP 业务写入或 shell 当作基础能力 | 设置展示服务端可用模型、受信技能和受管 MCP；安装/卸载在任务创建或项目配置保存后生效，刷新可恢复；模型工具支持不足、无权、失效扩展和陈旧配置被拒绝；技能方法进入模型上下文；MCP 可实际列举/读取只读资源并产生安全回执 | 安全目录 API/DTO、workspace proposal JSON、配置 CAS、worker、原生编码工具、设置组件/API/types | 租户/场景 ACL、凭据隔离、受信包、lease/fencing、发布副作用门禁 | 后端权限/边界回归、真实 PG 持久/并发验证、前端取消/冲突/迟到响应 |
| C3 | 高效编程 AI 的基础能力 | 不承诺终端执行、互联网浏览或任意测试运行 | 真实能力目录说明契约理解、公开计划、多文件检索/读取、候选文件修改、确定性检查、自修复、差异、停止/续轮、历史恢复；原生模型可主动读取/检索/检查项目，操作有预算，结果不能伪称业务执行成功 | coding worker/native loop、基础工具模块、目录和 UI | 统一场景执行内核、受保护 adapter/manifest、编写候选与运行事实边界 | 原生多轮工具行为、输出/时间预算、取消/fencing |
| C4 | 清楚场景所需能力，编码成可发布且遵循标准的插件 | 不凭空创建业务能力、不新增宿主或第二执行内核 | 编码上下文明确目标→选定能力→Skill/客户端脚本→输入/输出/确认/回执→验收/安装；标准 Skill 元数据校验；拒绝动态 shell 和越界工具预授权；原有人工定版/发布/安装链路继续通过 | authoring prompt、独立 Skill contract、source policy 薄适配及 files validation 入口、插件契约测试 | exact immutable release、正式凭据场景绑定、preview/confirm、幂等、不可变历史版本 | 官方规范核对、旧行为失败复现、插件/发布/安装回归 |

## 影响图

`需求 -> 插件开发入口 / 项目对话 / 设置抽屉 -> plugin-coding API -> workspace application service -> AssistantMessage.proposal + revision / AssistantRequestRun -> 受信技能读取 / 有界 MCP resources / coding worker -> 候选源码 -> 项目校验 -> 人工业务验收与代码定版 -> 既有发布中心和安装来源`

本次不改 ORM、Alembic、业务能力定义、CapabilityInvoker 或发布数据模型；配置复用现有持久 workspace JSON。只读 MCP 资源用于编程参考，业务工具仍由发布插件通过统一能力入口执行。基础检查仅静态/确定性校验，候选脚本不在平台执行。

必要消费者增补：新增两个静态受信方法技能 `backend/skills/plugin-authoring/SKILL.md` 和 `plugin-contract-review/SKILL.md`；`main.py` 的受限请求体路径纳入 settings，防止配置入口绕过原有预算；独立 Skill 元数据校验模块拒绝宿主动态 shell 执行和越界自动工具授权。

对话闭环增补：已有项目提供显式“编码 / 讨论”模式。讨论可以分析已保存文件、场景契约和只读资料，只提交解释而不写候选文件、不将模板或旧缺陷判为完成；未保存代码先保存。编码模式继续要求实际文件交付。新项目仍从明确编码任务启动。

执行预算增补：既有 `mcp_resource_service` 的 list/read 接口增加可选超时参数，默认行为保持不变；编程 worker 传入当前剩余时间，避免外部读取超出整轮截止。

共享模型输出预算增补：`llm_service.chat_stream` 在调用方显式设置 `max_output_chars` 时也在累计前计算原生工具 id/name/arguments 增量，关闭“只计算正文、原生工具参数无限累积”的旁路。未设置该上限的调用保持原行为；模型输出边界测试和后端全量覆盖另一使用此参数的消费者。

## 实施与验收记录

编辑前已检查 git status：仓库含关联聊天留下的多处未提交改动与新文件；本次增量保留这些内容。Python 系统默认 3.13.5 不用于项目验收；使用现有 Python 3.12.0 环境，Node 24.6.0、npm 11.5.1。未升级依赖或重写 lockfile。

以下记录实际执行结果；合成协议/并发验证与真实模型、浏览器验证分别说明。

### 插件规范核对与编码语义

依据 [Agent Skills 官方规范](https://agentskills.io/specification) 校验 name、description、compatibility、license 和字符串 metadata；安全 YAML 解析拒绝重复字段、自定义对象标签和无效字段类型，同时接受引用字符串、多行文本及正常 Markdown。入口 Skill 与额外 Skill 消费同一校验模块，避免入口重复正则误拒合法 YAML。项目既有路径契约限定英文 ASCII，因此采用规范允许范围的 ASCII 子集。

[Claude Code Skill 官方说明](https://code.claude.com/docs/en/skills) 明确动态上下文和 Skill hooks 可运行 shell，allowed-tools 是权限预授权。因此本项目的纯客户端交付边界拒绝 Skill hooks、正文或元数据中的动态 shell，并仅允许精确预授权场景发现、回执和审批读取工具；这是本项目受信客户端约束，官方标准本身允许更广的宿主功能。插件 manifest/MCP/执行器继续由受信模板生成，参照 [插件 manifest 规范](https://code.claude.com/docs/en/plugins-reference) 和 [MCP 工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)，模型不能替换执行器或凭据配置。

编码提示要求对全部选定能力给出公开的目标、触发条件、输入/输出、Skill/脚本入口和可观察验证覆盖；辅助编码技能/MCP 不会自动成为交付插件的运行依赖。原生工具检查与真实业务执行明确区分，合成用例和静态校验不宣称宿主安装、业务验收或人工发布成功。显式讨论模式只输出公开计划/回答，可读取和检查项目，不输出文件或晋级项目状态。

编码场景目标从已解析并核对 release/snapshot/hash 的正式快照读取，仅提取场景 name/description，经过既有 safe_snapshot_content 和整个编码契约 128KiB 预算。运行图的 scenario 元数据仍可能包含当前场景展示字段，因此不以该对象的 description 为冻结语义来源。修改 live 场景标题/说明不改变固定发布插件的编码目标，建模文档和实际调用资料不进入这一目标上下文。

失败复现：新增 Skill 回归在旧实现得到 19 failed / 2 passed，证明旧正则接受重复/无效元数据、自动 shell 和越界预授权，并拒绝合法 quoted/multiline YAML。修复后 Skill、prompt、coding 和 task 目标回归共 70 passed（两个既有依赖警告），使用 Python 3.12.0 与已有临时 pytest 8.x testdeps；未安装或升级依赖。

固定目标回归新增 6 项，先证明旧行为缺失正式目标、身份核对和目标预算；修复后连同 Skill、prompt、coding、task、resources、discussion、scenario plugin、artifact、publication 和 installer 共 163 passed（两个既有依赖警告）。安装器测试初次默认系统临时目录不可访问，指定仓库内 --basetemp 后通过；这次不使用业务数据库或外部业务数据。

最终 Skill 执行入口复审新增两个失败复现：宿主 hooks 和 frontmatter 中的动态 shell 原先均被放行，补齐后插件目标回归 165 passed。随后实际运行后端全量 `python -m pytest backend/tests -q -p no:cacheprovider --basetemp .tmp-plugin-skill-final-20261005a --tb=short`，得到 470 passed、两个既有依赖警告。此命令使用上面的 Python 3.12.0 和现有 testdeps；不等价于另行启用真实 PostgreSQL 并发、真实模型或外部 MCP 验收。

真实讨论验收发现旧包装模板的 pending 验收标记可能被模型错当当前状态，因此影响图增补只读 project_state 投影：prompt、契约读取及静态检查统一读取 workspace 当前 phase/讨论前源码 phase、已有 validation 与 acceptance_request 的人工确认和案例数量；不复制验收内容或回执 ID，不改模板、工件或发布语义。普通讨论使用业务名称，技术 ID/hash/key 仅在用户明确要求定位或命令时必要展示；外部系统连接行为需要实现证据，不从只读编程工具推断。

状态投影回归先得到 5 个旧行为失败，随后补充编程进行中不沿用旧源码审阅状态的反例，共新增 6 项。最后后端全量使用 `--basetemp .tmp-plugin-project-state-final-20261005a` 得到 479 passed、两个既有依赖警告。`released` 仅代表项目源码定版；该只读投影不读取独立发布子系统或远程宿主安装状态。

真实浏览器增补：失败/取消的轮次可恢复原问题与模式到输入框，保留尚未发送的草稿，由用户再次发送；设置关闭后恢复实际触发按钮焦点。活动讨论的执行状态与源码状态分开显示，新增服务端 `source_phase`，避免讨论期间将已定版源码显示成“候选生成中”。这些变化只补齐 C1/C3 的对话恢复、状态真实性与键盘验收。

真实模型读取增补：原生读取与检索共用文件树的受信模板加候选源码视图，使 `server.py` 与固定场景契约可读但仍不可写，校验清单不公开；旧读取回归先失败，修复后通过。真实模型已完成一轮只读讨论，读取两项工具资料，持久结果为 succeeded、无源码变化、保留已定版状态与已有三项人工业务验收记录。后续模型请求错误的处理继续按有限预算和权限门禁验收。

工具纠错增补：真实模型把目录当文件，原本会终止整轮；现在仅针对确定性的封闭参数、重复路径和不存在的项目路径返回明确拒绝与纠正指引。拒绝消耗持久操作/结果预算、记录 `tool_rejected`，不生成读取回执，不返回参数值；权限、lease、取消、扩展失效、MCP 和预算错误仍终止。失败诊断只保留受信函数名、异常类型和白名单参数形状，原始路径/错误文本/凭据不进入诊断或日志。

原生源码提交同样保留封闭 Schema：过长摘要或无效字段只返回固定的拒绝和字段上限指引，不提交、不记录为成功，模型可在既有轮数/调用/输出预算内纠正；通过 Schema 后，受保护路径、讨论禁止写文件和执行权威门禁仍由服务端拒绝。1001 字符摘要的旧失败复现、纠正后提交与重复无效提交耗尽预算均有行为测试。最后实际运行后端全量得到 489 passed、两个既有依赖警告。

讨论回复消费者增补：真实工具调用完成后，模型可能用原生聊天的正文直接答复；旧循环只消费工具调用，正文被丢弃后把已有回答判为失败。C1 的传统对话验收因此纳入末轮公开正文：仅讨论模式在无工具调用的回答轮读取公开正文，使用既有隐藏推理过滤、输出预算和每条 1000 字符的 summary Schema 持久化。带工具调用的前言不算最终答复，编码模式仍要求实际源文件交付，讨论仍不可写文件或晋级状态。

此消费者补齐后的最后后端全量命令 `python -m pytest backend/tests -q -p no:cacheprovider --basetemp .tmp-plugin-final-backend-20261005 --tb=short`：494 passed、两个既有依赖警告。使用已有 Python 3.12.0 与临时 pytest 8.x testdeps，关闭自动第三方 pytest 插件加载；没有删除、放宽或跳过测试。

最后真实模型轮次 succeeded，实际产生三项只读工具回执；普通正文正常持久保存。受控只读 PostgreSQL 对照验收前基线，确认 candidate files hash、validation、源码定版 phase 和三项业务验收记录数量均保持不变，round_failed=false。回答按当前记录说明源码已人工定版和已有业务验收，未从模板 pending 标记得出相反结论；未以本轮只读讨论证明宿主安装或对外发布。

### 最终验证与边界

前端实际命令 `npm --prefix frontend test`：296 项通过；`npm --prefix frontend run build`：TypeScript 和 Vite 生产构建通过。保留既有 Rollup 注释与大 chunk 警告；未改依赖或 lockfile。默认沙箱不能启动 Node 测试子进程或写构建缓存，以上命令通过已授权的执行环境运行。

真实 PostgreSQL 实际命令 `python backend/scripts/verify_plugin_coding_postgresql.py`：通过。脚本自行创建隔离数据库，读取实际 Alembic single head 后迁移，并最终删除该隔离数据库。验证设置持久化、并发 CAS 200/409、重复请求幂等、身份重用拒绝、运行中配置拒绝、失效扩展拒绝、技能进入真实 worker 上下文、讨论保留源码和原状态、停止后不可重放、不可变版本和租户/归属边界。模型流和业务验收 fixture 均为合成；会话读取为 1 次查询，最近一次合成验收耗时 3.659ms，不能推算生产负载。

真实浏览器验证新建与已有项目底部输入、先写目标、设置取消/Escape 返回真实按钮焦点、失败讨论恢复原问题与模式、技能保存刷新恢复、390×844 无横向溢出和输入无遮挡。桌面验证保留原源码和定版状态；临时 viewport 已恢复。编程 MCP 仅支持受管远程服务的 `resources/list` / `resources/read`，当前未实连第三方 MCP；协议、拒绝路径和安全预算由合成测试覆盖。未运行任意候选脚本、未宣称本次完成新的宿主安装或发布；既有人工验收、源码定版与独立发布门禁保留。

适用状态覆盖：目录/模型加载、无配置空态、读取错误、扩展无权/失效、revision 冲突、主动停止与迟到响应由回归覆盖；真实页面验证配置持久、失败恢复、模型讨论成功、键盘关闭和窄屏。讨论不产生业务副作用，因此业务预演/确认/未知结果由既有插件调用与发布回归验证，本轮不触发真实执行。此次没有 ORM/DDL 变更，迁移往返和独立部署健康检查 N/A；隔离验证仍读取实际 single head 并完成迁移。`git diff --check` 通过，保留原有用户改动；所有秘密配置只由受控 Settings 消费，没有打开、输出或改写 `backend/.env`。
