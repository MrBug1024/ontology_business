# 场景插件 AI 编码工作台

历史实现记录：本文的弹窗交互与旧截图已被[独立插件工作台重做](plugin-coding-studio-redesign-2026-10-04.md)替代。保留后台建设和当时验收证据，当前界面以新路由、代码与新截图为准。

需求依据：用户要求构建过程可观察、可及时修正，并提供第三方集成、安装和发布材料。上一轮改动全部保留。本轮不把一键模板打包或演示动画称为 AI 编码。

| ID | 目标 | 非目标 | 可观察验收 | 影响层 | 保持不变 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | 真实 AI 编写场景文件，持久展示计划与进度 | 展示隐藏推理；凭空模拟生成 | 后台任务调用已授权模型，文件/计划逐项持久化，刷新可恢复 | 独立 coding service/worker/DTO、既有 AssistantRequestRun | 原 lease/fencing/认证/发布身份 | 已接入真实 chat_stream；模拟流与真实 PG 通过，真实模型质量待验 |
| C2 | 观察文件、差异、编辑与即时纠偏 | 整批重写正确文件；迟到输出覆盖人工修改 | CAS 编辑；反馈撤销旧任务并创建新轮；旧执行结果拒绝 | workspace JSON/独立前端工作台/composable | 已有助手与候选流程 | PG 并发/运行取消/冲突通过；Vue 实际状态测试、浏览器编辑/差异/恢复/停止通过 |
| C3 | 审阅、验证后才生成可交付版本 | 任意 LLM 代码在平台执行；用语法验证冒充业务验收 | 保护固定运行适配器；校验路径/尺寸/示例代码/Skill/原回执；确认绑定文件 hash/revision | validator/artifact/download | 统一 Invoker、既有人工业务验收 | 19 项 coding 回归与原 7 项插件回归通过；版本不可替换、并发交付通过 |
| C4 | 支持插件 ZIP 与自托管 Marketplace 发布包 | 自动上传私有资料或推送未知仓库 | 同一已审阅工件可下载插件/市场包；包含实际安装步骤、精确版本和校验和 | artifact/distribution/UI | 凭据不入包，场景停用仍阻断调用 | Claude Code 2.1.208 插件/市场校验通过；stdio 五个工具通过，未实际外部发布 |

影响图：发布与三类案例 → 创建持久编码会话 → 既有任务 claim/heartbeat/fencing → AI 公共计划与候选文件 → 编辑/反馈/CAS → 校验与审阅 → 固定工件快照 → 下载插件或 Marketplace → 第三方安装 → 原平台统一运行。

交付影响图补充：受信适配器身份固定 → 人工设置插件版本 → 同一工作区、插件名称、版本只对应一种内容。发现模板更新或同版本内容变化必须明确拒绝，人工升版后重新审阅；不得静默替换已交付版本。新编码入口同样采用请求体大小门禁。Skill 的工具名称必须对应适配器实际注册的工具。

编码质量影响图补充：原接入清单只包含输入/输出 Schema 哈希，无法支撑完整示例生成。编码会话必须由服务端通过同一能力目录读取精确发布的完整契约并固定，交给模型的是定义契约而非回执中的客户运行数据。示例须真正 `await` 受信客户端、包含满足输入 Schema 的合成值；未交付文件的模型轮次明确失败。

实现边界：AI 生成 Skill、接入说明及 Python 调用示例；认证、统一执行和版本固定适配器受保护。候选示例作为文本经过语法/受限结构检查，平台不执行 AI 代码。构建验证与真实业务验收分别显示。首个宿主继续采用 Claude Code。

发布方式是生成可部署的本地/自托管 Git Marketplace 材料；不声称已向公网、Git 仓库或第三方目录发布。该范围无需外部凭据，也不创建公开下载权限。规范参考：[Marketplace](https://code.claude.com/docs/en/plugin-marketplaces)、[Plugin manifest](https://code.claude.com/docs/en/plugins-reference)。

## 产品使用路径

1. 在场景发布列表选择“插件工作台”。新建时选择该精确启用发布中已具备执行条件的能力，为每项能力选择不同的成功、边界与失败处理回执，并人工核对业务结果。仍在运行/等待审批的工作流不能充当完成案例。
2. 选择工作区可见的对话模型，填写第三方使用要求与插件版本，开始后台编码。模型得到发布的完整输入/输出契约与当前文件，不读取回执中的本次输入、客户数据或连接凭据。
3. 工作台展示公开计划、已保存文件及修改前/当前候选对照。可直接编辑、保存并校验，或提交修正意见开始新轮。也可单独停止本轮并保留已保存文件。停止的权威动作是撤销任务 lease，而不是仅在前端隐藏进度。
4. 出现并发冲突时保留本地编辑草稿，显示新的候选；比较视图同时显示未保存草稿。核对后显式更新编辑基准，再提交。后台迟到输出必须被服务端拒绝。已保存文件与进度可从最近五个会话恢复；未保存的本地编辑需先保存再离开。
5. 文件结构/合成输入校验通过后，人工审阅当前内容与使用步骤，下载插件包或 Marketplace 包。导出前重新核对原发布、权限、案例与发布状态，并固定审阅文件身份。导出状态表示已生成材料，不能证明已经外部发布。
6. 同一插件名称与版本已经交付后，改动代码必须设置新版本、重新保存与审阅。跨工作台竞争同一版本由数据库唯一身份保护。平台模板更新后拒绝静默重建旧工作台，需新建并重新审阅。

插件 ZIP 面向解压后本地加载；Marketplace ZIP 包含插件原文件、目录清单、相对源路径及安装说明。用户可将市场目录部署到有权发布的 Git 仓库，第三方注册该市场后安装。第三方的仓库访问权限与场景专属业务凭据分别配置；宿主仍需可用的 Python 3.12 和包内受信依赖。

## 持久与代码边界

工作台使用专属 `plugin-coding:<release>` 的 `AssistantThread` 与版本化 `AssistantMessage.proposal`，复用既有 `AssistantRequestRun` 的持久队列、claim、续租、恢复和 fencing。普通助手的页面 scope 校验不能匹配此编码 scope。未新增 ORM 字段或数据库结构，未修改历史迁移；PostgreSQL 验证仍从当前实际 single head 新建隔离库。

编码文件严格限于 Skill、README 与示例；服务端适配器、MCP 配置与发布身份受保护。模型输出是有界 JSON Lines，最多 20 个公开步骤。只有计划而没有文件的轮次失败。路径、尺寸、常见密钥格式、Skill 头、实际工具名、受限 AST、`await` 及完整输入 Schema 均由服务端检查；缺少必填字段时显示具体字段名。校验不执行 AI 代码，不证明业务输出正确。

原助手入口仅薄分派 coding job。编码的契约读取、工作台、流处理、prompt、校验、身份、导出和分发分别有独立模块；前端 API、类型、轮询状态与工作台组件分开。取消仅用于无外部副作用的候选源文件任务，不改变通用助手对外部效果任务的取消语义。

## 实际验证与未验证项

支持环境：Python 3.12、pytest 8.x；Node 24.6.0、npm 11.5.1。全量后端 **291 passed**，前端 **240 passed**，`vue-tsc` 与 Vite 生产构建通过。新增最终局部复验 **26 passed**。现有依赖产生 anyio 别名/Pydantic 设置警告，以及 Rollup 注释/大包警告；本轮未升级依赖或旁支改默认配置。

真实 PostgreSQL 在脚本自行创建、迁移并最终删除的随机隔离数据库上验收：并发创建唯一、双 worker 单次生成、JSON 文件与进度持久、旧 revision 拒绝、运行中的 lease 撤销、原版本快照保留、显式升版、跨工作台同版本不同内容只有一个交付成功、外属会话拒绝、完整 Schema 进入编码上下文，以及无文件输出失败。[结构化结果](plugin-coding-postgresql-results-2026-10-04.json)明确标注模型流、业务验收及权限桩为合成边界；不能用它冒充真实模型、ACL 全矩阵或真实业务案例验证。

受信适配器真实 stdio 初始化与五个工具发现通过；含明确合成候选文件的 ZIP 可重复构建。Claude Code **2.1.208** 对插件和 Marketplace 的 `plugin validate` 均通过，无市场描述警告。[安装检查结果](plugin-coding-installation-results-2026-10-04.json)。没有向业务服务发送执行请求。

浏览器验收运行真实组件与明确合成 API：三类案例选择、模型选择、启动、编辑冲突保留草稿、差异/草稿对照、人工保存、反馈新轮、停止、关闭后恢复、显式升版、审阅门禁与生成材料提示通过。390×844 窄屏页面宽度为 390，无水平溢出；受保护文件没有编辑框，Tab 可导航文件按钮。[工作台界面](plugin-coding-workbench-ui-2026-10-04.png)、[窄屏证据](plugin-coding-workbench-narrow-2026-10-04.png)。下载动作后的界面回调通过，内置浏览器下载事件未捕获到磁盘文件；实际 ZIP 字节和安装结构由前述独立工具验证。此界面验收不是登录真实后端的 E2E。

尚未验收：当前工作区真实模型的完整生成率、真实业务场景及第三方凭据调用、外部 Git 上传/市场发布、其他宿主安装、任意运行模块的隔离 AI Coding Provider。当前只支持 Claude Code 的定制文件封装，执行继续在线委托原平台；没有宣称离线业务引擎、任意模型代码自动上线，或“十个对象至少七个可用”已被真实样本证明。

## 可复现命令

以下命令在符合仓库环境要求的已激活 Python 3.12 环境执行。pytest 使用新的空临时目录；命令示例中的目录不存在时才能使用。

```powershell
$env:PYTHONPATH=(Resolve-Path .\backend).Path
python -m pytest .\backend\tests -q -p no:cacheprovider --basetemp .tmp-plugin-acceptance-new
npm --prefix .\frontend test
npm --prefix .\frontend run build
python .\backend\scripts\verify_plugin_coding_postgresql.py
python .\backend\scripts\verify_scenario_plugin.py --output .tmp-reviewed-plugin-new --reviewed-coding
claude plugin validate .tmp-reviewed-plugin-new/scenario-synthetic
claude plugin validate .tmp-reviewed-plugin-new/scenario-synthetic-marketplace
```

PG 脚本必须具有创建/删除其自行命名隔离数据库的迁移管理权限，仅通过受控 Settings 读取配置；不编辑或输出 `.env`。stdio 脚本只运行受信客户端，不调用业务能力。临时产物与浏览器 fixture 未纳入提交。
