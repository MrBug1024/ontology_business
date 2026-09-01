# 业务场景本体智能平台开发宪法

> 文件：根级 `AGENTS.md`  
> 版本：1.0  
> 生效日期：2026-09-01  
> 适用范围：本仓库全部代码、迁移、测试、SDK、Skill、文档与构建配置

本文件是项目级工程宪法，服务于人类开发者和编码 Agent。目标不是增加流程负担，而是防止项目扩大后出现需求错配、架构漂移、重复实现和无关功能回归。

本文中的“必须 / MUST”“禁止 / MUST NOT”是不可绕过的约束；“应该 / SHOULD”是默认规则，偏离时必须给出可验证的项目理由。子目录以后可以增加更具体的 `AGENTS.md`，但不得弱化本文件的安全、租户、统一执行、数据边界和验证要求；永久例外必须先修改本文件。

## 1. 项目身份与终极目标

### 1.1 项目概述

本项目是受 Palantir Ontology 启发的通用业务能力平台。平台将业务语义、对象和关系、输入输出契约、函数、规则、Action、事件、Workflow 与证据建设为可发现、可版本化、可治理、可审计的能力。

平台不绑定某个行业、某个客户、某一批数据或某个 Agent。平台内 Agent 是验证能力的参考客户端，不是业务场景本身，也不是唯一运行入口。验证 Agent、REST、MCP 和 SDK 必须消费同一能力定义并获得一致的身份、权限、执行与审计语义。

### 1.2 终极目标

项目最终必须让业务专家能够在不修改平台内核代码的前提下：

1. 建立行业无关的场景本体与版本化能力契约。
2. 通过人工或 AI 形成候选定义，并经确定性校验、评审、发布和回退完成治理。
3. 在 `dev / staging / prod` 解析相同 Definition 的受控部署。
4. 在每次调用中提供当前文本、文档、结构化参数或受管业务数据，而不重建能力。
5. 通过验证 Agent、REST、MCP 或 SDK 统一调用 `CapabilityInvoker`。
6. 在多租户、高并发和失败重试下保持权限、幂等、证据、版本身份与审计链正确。

目标主链路为：

```text
Scenario Definition Version
  -> Ontology + Capability + Port + Policy + Evidence
  -> Release to dev/staging/prod
  -> Resolved Deployment
  -> Per-invocation typed/managed input
  -> CapabilityInvoker
  -> Agent / REST / MCP / SDK
  -> Receipt + audit + governed feedback
```

### 1.3 明确的非目标

- 不把医保、财务、零售或其他单一业务写进平台通用内核。
- 不把固定客户数据、DataSource、表列、对象路径或凭据固化进 Definition。
- 不为不同协议复制执行内核、权限、readiness、数据解析或业务算法。
- 不以 Redis、进程内字典、浏览器本地状态或 LLM 输出作为权威事实。
- 不恢复 SQLite/MySQL 平台后端；正式平台关系型存储只有 PostgreSQL。
- 不用“兼容”长期保留无边界的双实现；兼容层必须有范围、测试和退出条件。
- 不因 AI 生成、内部调用或管理员身份绕过发布、副作用、租户或凭据门禁。

## 2. 权威来源与冲突处理

按以下顺序解释项目事实和目标：

1. **本 `AGENTS.md`**：工程行为、架构底线、安全门禁和完成定义。
2. **当前用户明确批准的需求与验收条件**：本次工作的业务范围。若与宪法冲突，禁止静默执行；必须指出冲突并先取得修改宪法或需求的明确决定。
3. **`docs/优化升级任务计划文档.md` 的当前目标、任务和验收矩阵**：当前架构升级的唯一执行计划。
4. **`docs/能力平台架构与接入指南.md`**：稳定能力边界、Provider 和第三方协议约定。
5. **`docs/PostgreSQL-MinIO-通用数据资产架构.md`**：存储职责与数据生命周期。
6. **`README.md`、配置、入口代码和测试**：当前运行方式与已实现行为的证据。
7. **`docs/实现计划.md`**：历史基线，只用于理解遗留行为，不得覆盖当前计划。
8. **计划进度日志、问题记录、示例和架构构想图**：审计证据或参考，不等于当前完成状态或目标设计。

执行规则：

- 代码和测试只能证明“现在如何运行”，不能在目标文档冲突时自动成为正确设计。
- 文档互相冲突时，不得挑选最省事的一种，也不得同时新增两套语义。先定位冲突，再以较高权威来源统一目标、依赖和验收条件。
- 架构、产品语义、公共契约或执行顺序发生变化时，必须先更新当前计划，再改代码。
- 不复制文档中的固定迁移 head；必须从 Alembic 实际 single head 获取当前版本。
- 进度日志中的 `DONE` 不能替代可复现验收。已知 P0、未运行的全量测试或缺失的真实用户闭环仍然存在时，禁止宣称完成。

当前计划明确优先于旧指南的事项包括：新建和存量 Agent 的产品运行模式为 `capability_only`；旧模式只可作为历史快照值读取。建模资料、旧 `Agent.data_source_ids` 和历史场景绑定不得隐式成为正式运行输入。若要改变这些事实，必须先修改当前计划和验收矩阵。

## 3. 技术栈与运行边界

| 层 | 当前技术与约束 |
| --- | --- |
| 前端 | Vue 3、TypeScript 5.6、Vite 6、Pinia、Vue Router、Element Plus、Axios、Vue Flow、Marked |
| 后端 | Python 3.12、FastAPI、同步 SQLAlchemy 2、Pydantic v2/settings、psycopg 3、Alembic |
| AI / 协议 | OpenAI 兼容接口、LangChain/LangGraph、MCP SDK、httpx |
| 权威控制面 | PostgreSQL，保存租户、权限、定义、发布、目录、任务、审计和元数据 |
| 对象与数据版本 | MinIO，保存不可变上传、Parquet、产物和证据对象 |
| 缓存 | Redis，仅可失效、可降级、可从权威存储重建 |
| 查询 | 进程内 DuckDB，仅对已验证的受管 Parquet 做有界只读查询 |

仓库当前没有统一的 Python formatter/linter/type-check 命令，也没有前端 lint、组件测试或浏览器 E2E 脚本。禁止虚构或声称运行了不存在的门禁；新增工具必须作为显式依赖变更，说明理由并提交对应配置和 lockfile。

## 4. 目录结构与所有权

```text
/
|-- AGENTS.md                         # 本工程宪法
|-- README.md                         # 项目入口、安装与运行说明
|-- docs/                             # 当前计划、稳定架构、存储和问题证据
|-- backend/
|   |-- app/main.py                   # FastAPI 组合根和生命周期
|   |-- app/config.py                 # 部署配置与安全默认值
|   |-- app/database.py               # Engine、Session 和 schema-head 校验
|   |-- app/models.py                 # 当前主 ORM 声明（遗留大文件）
|   |-- app/*_schemas.py              # 边界 DTO
|   |-- app/routers/                  # HTTP 协议适配
|   |-- app/services/                 # 应用服务、通用领域内核和基础设施编排
|   |-- app/providers/<provider>/     # 行业/专用能力 Provider
|   |-- migrations/                   # Alembic 增量迁移
|   |-- sdk/                          # REST v2 薄客户端，不复制业务实现
|   |-- skills/                       # 受信内置 Skill 包
|   |-- mcp_servers/                  # 受控 MCP 服务示例/实现
|   |-- scripts/                      # 运行时与迁移验收脚本
|   `-- tests/                        # 后端单元、契约、回归和集成测试
`-- frontend/
    |-- src/main.ts                   # 前端组合根
    |-- src/router/                   # 路由与导航守卫
    |-- src/api/                      # HTTP/SSE、序列化和错误归一化
    |-- src/stores/                   # 跨页面权威客户端状态；当前以认证为主
    |-- src/types/                    # 前后端共享语义的 TypeScript 类型
    |-- src/utils/                    # 无副作用转换、校验和 payload 构造
    |-- src/components/               # 可复用领域组件
    |-- src/views/                    # 路由级编排页面
    |-- src/styles/                   # 全局基础样式
    `-- tests/                        # Node 契约/回归测试
```

以下是生成物、依赖、缓存或本机状态，不是源代码：`.venv/`、`node_modules/`、`dist/`、`__pycache__/`、`.pytest_cache/`、`*.pyc`、`*.tsbuildinfo`、生成的 `frontend/vite.config.js` / `.d.ts`、`.runtime/`、`.tmp*`、`.codex-*` 和日志。禁止手工编辑、提交或把它们当作实现依据。

`backend/.env` 及生产环境文件可能含密钥。除非用户明确要求诊断某个本地配置，编码 Agent不得读取、展示、改写或提交其值；配置契约只改 `backend/.env.example` 和 `Settings`。

## 5. 标准命令

除明确注明外，命令从仓库根目录执行，PowerShell 为基准 shell。

### 5.1 首次安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\backend\requirements.txt
Copy-Item .\backend\.env.example .\backend\.env
npm --prefix .\frontend ci
```

若 lockfile 与当前 npm 环境无法使用 `ci`，才使用 `npm --prefix .\frontend install`。若 npm 拦截受信安装脚本：

```powershell
npm --prefix .\frontend approve-scripts esbuild vue-demi
```

依赖未改变时禁止重写 `requirements.txt` 或 `package-lock.json`。

### 5.2 数据库迁移

应用运行角色不得执行 DDL。使用迁移 owner/admin：

```powershell
python -m alembic -c .\backend\alembic.ini -x use_admin=1 upgrade head
python -m alembic -c .\backend\alembic.ini current
```

### 5.3 启动开发环境

```powershell
python -m uvicorn app.main:app --app-dir .\backend --host 127.0.0.1 --port 8000
```

另一个终端明确设置与后端一致的代理目标，不依赖本机可能修改过的默认端口：

```powershell
$env:VITE_API_PROXY_TARGET='http://127.0.0.1:8000'
npm --prefix .\frontend run dev
```

前端地址为 `http://127.0.0.1:5173`，API 文档为 `http://127.0.0.1:8000/docs`。改变端口时必须同步代理配置和相关文档，不得在组件中硬编码主机或端口。

### 5.4 测试与构建

```powershell
$env:PYTHONPATH=(Resolve-Path .\backend).Path
python -m pytest .\backend\tests -q
npm --prefix .\frontend test
npm --prefix .\frontend run build
```

涉及真实 PostgreSQL 并发、锁、JSONB、约束或权限时额外执行：

```powershell
$env:RUN_POSTGRESQL_INTEGRATION_TESTS='1'
python -m pytest .\backend\tests\test_catalog_postgresql_concurrency.py -q
```

需要完整部署依赖的验收：

```powershell
python .\backend\scripts\verify_alembic_roundtrip.py
python .\backend\scripts\verify_postgresql_runtime.py
```

迁移往返脚本必须只使用脚本自行创建的隔离数据库。禁止在 live、共享或客户数据库执行 downgrade rehearsal。

## 6. 每次开发的强制流程

### 6.1 开工前：把 A 固定为 A

任何代码修改前必须完成以下只读工作：

1. 查看 `git status`，识别并保留用户已有改动。
2. 阅读本文件、相关权威文档、入口代码、调用方、被调用方和现有测试。
3. 使用搜索确认符号、路由、DTO、状态值和错误协议的全部消费者，不能只读命中的第一个文件。
4. 建立本次“需求账本”，至少包含：明确需求、非目标、用户入口、期望状态变化、失败行为、兼容要求、验收证据。
5. 建立影响图：`需求 -> 前端入口 -> API/协议 -> application service -> model/storage -> worker/external I/O -> tests/docs`。不涉及的层必须明确保持不变。
6. 若用户明确给出了“做什么”和“怎么做”，实现不得自行替换为相似方案。只有发现与项目事实或宪法冲突时才暂停并说明证据。

### 6.2 实现时：最小且完整

- 只修改需求账本和影响图内的文件。发现旁支缺陷时记录，不顺手修复。
- 采用最小影响面完成一条端到端纵向链路，不创建只在某入口生效的旁路实现。
- 行为修复应先增加能复现问题的测试，或至少在同一改动中加入会对旧错误行为失败的测试。
- 不做无关重命名、全文件格式化、目录搬迁、默认值调整或依赖升级。
- 不通过删除校验、放宽类型、吞异常、跳过测试或复制旧逻辑来“让功能工作”。
- 不留下伪实现、静默 fallback、永真占位、无负责人 TODO 或声称成功但未持久化的状态。
- 用户可见功能必须形成真实产品闭环。若能力设计为 API-only，必须在需求和文档中明确；后端对象存在但业务用户无法配置、发现或调用，不算完成。

### 6.3 完成前：逐条对账

1. 按需求账本逐项给出代码和测试证据，确认没有把 A 实现为 B。
2. 运行最小相关测试，再按第 15 节扩大验证范围。
3. 检查加载、空、错误、权限不足、冲突、重试、取消和成功状态。
4. 检查相邻既有流程，尤其是共享服务、状态机、权限和协议消费者。
5. 复查 `git diff` 与 `git status`，确认无密钥、生成物、调试输出或无关改动。
6. 如实报告运行过的命令、结果、未运行项及原因。禁止把“代码已写”表述成“验收通过”。

## 7. 不可破坏的领域与架构约束

### 7.1 三层身份分离

```text
Definition plane  -> definition_hash
Deployment plane  -> deployment_fingerprint
Invocation plane  -> data_context_fingerprint
```

- Definition 描述业务语义、能力、端口、Schema、策略、Provider identity 和验证要求，不保存客户本次数据。
- Deployment 解析 Definition、Release、tenant、服务端环境与受信连接身份。
- Invocation 保存 Actor、typed inputs、本次受管引用、确认、幂等、correlation 和审计。
- 更换数据版本必须改变 `data_context_fingerprint`，不得改变 `definition_hash`。
- `dev` 可解析受治理的 live definition；`staging/prod` 必须使用已发布的不可变快照。
- 环境是服务端部署边界。调用方不得通过任意请求参数选择未授权运行环境。
- 发布后的 ORM 行、嵌套 JSON、端口或绑定变化不得反向改变旧 Release 的解析结果。

### 7.2 数据不是能力的必选前置条件

- 零数据能力必须能够建模、验证、发布和调用，readiness 不得强制要求 DataSource、Dataset 或 Mapping。
- 建模资料默认只用于场景理解、结构发现和候选生成；不得隐式升级为正式运行数据。
- 当前正式运行输入只来自本次显式 typed input / 受管附件、Agent 专属正式连接，或第三方显式受管输入/专属连接。
- 禁止从建模 `DataSource`、旧 `Agent.data_source_ids`、`ScenarioDatasetBinding`、历史对话或环境变量隐式补足运行数据。
- `DatasetHead` 只用于选择，调用开始必须固定为不可变 `DatasetVersion`；并发 Head 更新使用 expected version / compare-and-set。
- 公开调用只接受受管逻辑引用或受信 `binding_key`，不接受连接串、任意 SQL、物理表列、bucket、object key 或凭据。

### 7.3 唯一执行链路

验证 Agent、REST v2、MCP 和 SDK 只能做认证、协议解析、构造统一请求、调用应用服务/`CapabilityInvoker` 并渲染统一 Receipt。禁止在 router、MCP tool、SDK 或前端复制：

- capability 查找和 readiness；
- 租户、角色、scope 或 ACL；
- 数据端口解析和版本固定；
- Provider 选择或业务算法；
- preview / confirmation / idempotency；
- Receipt、provenance 或错误语义。

旧 `/external/v1` 等兼容入口不得被无意破坏。公共 API、协议 token、`api_name`、数据表和持久状态值一旦发布即视为稳定标识；破坏性变化必须使用显式版本、迁移、弃用窗口和兼容测试。

### 7.4 Provider 边界

- 行业字段、专用算法、场景工具、grounding、历史工具别名和特定逻辑模型解释只能位于 `backend/app/providers/<provider>/`。
- 平台通用内核、Agent shell、协议层和通用 prompt 不得出现行业名、固定场景 namespace、固定业务字段、表名或工具名分支。
- Provider 必须按精确 `(provider_key, provider_version)` 由受信代码静态注册。数据库不得提供 Python 模块路径，禁止 `import_module`、`__import__`、`eval` 或 `exec` 动态装载租户代码。
- Provider `input_schema` 和 `provider_config` 必须封闭，拒绝未声明字段、凭据、物理查询和连接配置。
- 注册表单例不得持有请求状态；请求相关 DB、Actor、数据上下文和工具状态必须放在 request-local binding 中。
- 版本缺失或不匹配必须 fail closed，不得回退到同 key 的“最新版本”。
- 有数据 Provider 必须用受管版本 A/B 证明 Definition 不随数据变化；还必须有零数据反例和 Agent/REST/MCP 语义一致性验证。

### 7.5 候选、发布与 AI

- AI、人工和导入只是 provenance 不同，必须进入同一候选治理状态机。
- LLM 输出只能形成候选、草稿、解释或预演，不能直接宣称正式定义已写入或副作用已完成。
- 是否可晋级由服务端确定性校验、依赖闭包、风险、revision 和质量 fingerprint 决定；前端不得自行推断。
- 编辑、重新校验和晋级必须携带 expected revision；陈旧写入返回冲突，不做 last-write-wins。
- 批量晋级必须原子；任一 blocker、revision 漂移或正式化失败都不得留下部分结果。
- Action 成为正式定义不等于自动激活，发布也不等于绕过运行确认。

## 8. 禁止业务硬编码

生产代码中禁止硬编码：

- tenant、user、scenario、agent、resource 的真实 ID；
- 客户名称、行业名称或样例数据驱动的核心分支；
- 物理数据库、表名、列名、SQL、bucket、object path；
- 密码、API key、token、Cookie、请求头值、加密 key ring；
- 本机绝对路径、部署 Host、端口或 `dev/staging/prod` 的客户端越权选择；
- 用 UI 显示文案、中文标签或数组位置判断稳定业务状态；
- 为通过一个样例而写死阈值、规则、Provider alias 或 namespace；
- 在前端复制服务端权限、readiness、发布资格或输入治理算法。

正确归属：

| 内容 | 应放位置 |
| --- | --- |
| 部署差异、超时、资源上限、安全开关 | `Settings` + 环境变量 + `.env.example` |
| 可由业务配置的规则/阈值/映射 | 受治理 Definition、Provider config 或数据库模型 |
| 行业逻辑和兼容 alias | 独立版本化 Provider |
| 稳定协议词汇、枚举、正则和安全上限 | 命名明确的共享常量/契约模块 |
| 样例 ID、行业数据和断言 | `tests/fixtures` 或 `examples`，不得被生产路径导入 |
| 用户显示文本 | 前端视图/组件，不得反向成为业务标识 |

安全协议常量和有依据的默认上限不是“业务硬编码”，但必须集中、命名、可测试，并在部署可变时进入配置。不得为了表面消除常量而把安全边界变成任意客户端输入。

## 9. 后端模块化与代码边界

### 9.1 分层职责

- `main.py` 只负责应用组合、middleware、router、生命周期和 worker 启停。
- `routers/` 只负责认证依赖、请求 DTO、协议转换、HTTP 状态映射和明确的事务边界。
- `services/` 承担用例编排、通用领域策略和基础设施协调。可复用低层 service 不得依赖 router。
- `*_schemas.py` 定义外部 DTO；`models.py` / `external_api_models.py` 只定义持久化结构、关系和数据库约束，不承载业务流程。
- `capability_contracts.py` 等协议无关内核不得导入 FastAPI、HTTP、SQLAlchemy ORM 或具体 Provider 实现。
- `providers/` 承载专用业务；`sdk/` 只包装公共协议，不解析客户数据或复制 Provider 逻辑。

现有 `routers/assistant.py`、`routers/scenarios.py`、`services/scenario_model_compiler.py`、`models.py`，以及前端 `ScenarioDetail.vue`、`GlobalAssistant.vue` 是遗留巨型文件，不是新代码的范例：

- 禁止继续向巨型文件增加可独立命名、测试或复用的新子领域。
- 新功能应拆成有领域名称的 router/service/schema/provider/component/composable/pure util，再由旧入口薄编排。
- 不创建含混的 `helpers.py`、`utils.py` 或 `common.py` 来转移耦合。
- 不为本次小需求顺手重写整个遗留文件；抽取必须有行为测试，并保持 import、路由、序列化、表名和状态兼容。
- 新函数应保持单一责任。函数接近 80 行或新文件接近 800 行时，必须优先拆分；确实不可拆时在任务说明中记录原因。该阈值用于约束新增代码，不要求无关地重构全部遗留文件。

### 9.2 DTO、契约与错误

- 新增或修改外部输入必须使用有界 Pydantic DTO：关闭额外字段、使用 `Literal`/枚举、设置字符串/集合/数值上限，并用 validator 表达跨字段约束。
- 公开 route 不直接接受含义不明的裸 `dict[str, Any]`；输出使用明确 schema/response model。
- 需要持久化、签名或 hash 的协议值必须先 canonicalize；禁止用 `str(object)`，也不得接受 NaN、Infinity 或任意对象隐式序列化。
- 不可变协议对象优先 frozen dataclass / tuple / immutable mapping，并使用 domain-separated hash。
- 领域异常在边界统一映射为稳定、安全的错误；不得把 traceback、SQL、连接信息、资源存在性或供应商原始错误返回客户端。
- 捕获宽异常只允许在 worker、清理或外部边界，并必须记录内部上下文、向外返回安全信息；若项目检查约定需要，保留 `# noqa: BLE001`。

## 10. 租户、认证与安全底线

- 每个受保护读写必须同时验证 Actor、tenant、资源归属、场景 ACL 和所需 role/scope；知道资源 ID 不等于有权访问。
- deny 优先 allow；公共资源对非所有者只读；缺失权限字段按无权处理。
- 跨租户与不存在资源遵循现有防枚举语义，错误、日志和计时不得泄露目标资源细节。
- 后台任务必须持久化并恢复可审计 execution principal。发起人失效后不得匿名运行或自动升级为 owner/admin。
- 浏览器 HttpOnly session、外部 `X-API-Key` 和 Agent MCP token 是不同认证域，禁止互相回退或复用 hash domain。
- token 原文只允许创建时返回一次；数据库只保存域分离 hash。凭据字段不得出现在 `repr`、日志、错误、LLM prompt、snapshot、Receipt、fixture 或前端持久状态。
- workflow payload 加密 key ring 只来自部署 Secret Manager；缺失或版本不匹配必须 fail closed，不允许默认 key。
- 所有副作用必须执行 `preview -> server-issued confirmation -> execute`，确认绑定 tenant、principal、correlation、输入 hash、Definition、Deployment 和有效期。
- 副作用必须有持久化幂等键和数据库唯一性/CAS。外部结果未知时进入 `indeterminate/reconciliation`，禁止盲目重放。
- 外部 HTTP/MCP 继续执行 HTTPS、SSRF 检查、DNS pinning、私网 allowlist 和禁止自动重定向；stdio、任意脚本和不安全 HTTP 默认关闭。
- Agent/connector SQL 只允许项目策略支持的单条、参数化、只读查询，并受 Schema allowlist、行数和超时限制；不得弱化 parser 以支持任意 SQL。
- 上传必须验证权限、声明长度、实际字节、格式、解压上限、TTL 和内容身份；客户端扩展名、MIME 或 UI 校验不是安全边界。
- 不可信 Markdown 禁止 `v-html`；前端统一使用 `SafeMarkdown` 的转义与链接协议白名单。

## 11. 事务、并发与故障恢复

“高并发”意味着跨请求、跨进程和失败重试下仍然正确，不等于简单增加 `async`。

### 11.1 事务所有权

- 常规低层 service 只 `add/flush`，最外层 application/router/worker 明确 `commit/rollback`。
- 只有已有文档和测试证明的原子 orchestration（如受管上传、持久 worker checkpoint）可以在内部 commit；禁止复制偶然的现有 commit 模式。
- 网络、LLM、MinIO、MCP、子进程等长 I/O 不得无必要地持有数据库事务或行锁。
- 检查后写入必须有 unique/check/FK、`FOR UPDATE`、CAS/expected revision 或捕获 `IntegrityError` 的数据库保障，不接受仅 Python `if` 的竞态窗口。
- 冲突应稳定映射为 409/可重试结果，不做静默覆盖。

### 11.2 多进程与 worker

- 跨进程正确性不得依赖 Python 全局 dict、Lock、进程内队列或单个 API 实例。
- 新持久 worker 必须使用数据库原子 claim、bounded batch、lease token、expiry、generation/fencing、续租、崩溃恢复和安全重试；按场景使用 `SKIP LOCKED` 或 CAS。
- API lifespan 中的 worker 会随进程数扩展。任何新 worker 都必须证明多个实例不会重复执行副作用。
- 自动重试复用原 execution lineage 和派生幂等键；人工重试必须显式创建新的审计 lineage。不得混淆两者。
- 同步 DB、MinIO、LLM 或子进程 I/O 不得阻塞 async event loop；使用同步 FastAPI endpoint/threadpool、`asyncio.to_thread` 或独立 worker，并保持有界并发。
- 所有外部调用设置连接/读取/总超时、取消和资源清理；禁止无界 `gather`、无限轮询或无上限重试。

### 11.3 跨存储一致性

- PostgreSQL 与 MinIO 双写必须使用 durable intent、outbox、lease/fence 和可重入清理；禁止先删对象再提交权威元数据。
- MinIO object key 由服务端生成并带 tenant/场景/generation 作用域；元数据记录 bytes、SHA-256、version/etag 和稳定 `minio://` 身份。
- 上传和下载使用流式分块及配置上限；不得为方便把大文件一次性读入内存。
- Redis 失败时业务正确性仍由 PostgreSQL/MinIO 提供；Redis 不得承担唯一锁、授权、幂等或任务状态。
- DuckDB 只处理已验证 Catalog/manifest 和受管 Parquet，保持 query timeout、内存、线程、临时目录、缓存、行数和并发上限。
- 新连接器缓存必须有锁、容量、淘汰、连接/语句超时和 dispose；禁止新增无界进程内 engine/cache 字典。

## 12. 数据库与 Alembic 规则

- PostgreSQL 是唯一生产平台数据库。生产逻辑和迁移不得增加 SQLite/MySQL 分支。
- ORM、索引、约束、权限或持久字段变化必须新增 Alembic revision，并同步 `backend/app/database.py` 的 `POSTGRESQL_SCHEMA_REVISION`；不得修改可能已经应用的历史迁移来伪造状态。
- 保持单一 Alembic head。若并行迁移产生多 head，显式 merge 并验证依赖，不靠文件名顺序猜测。
- 应用启动只校验 migration head 和结构，不得 `create_all`、自动补列或执行 DDL。
- 数据回填必须确定、可审计、幂等或 fail closed；禁止按中文名、前缀、数量或“最可能的对象”猜测归属。
- upgrade 和 downgrade 都必须明确处理不可逆数据。会丢失审计、密文或对象身份的 downgrade 必须主动拒绝并说明前置条件。
- 租户关联优先使用复合外键、复合唯一键和 check constraint 闭合，不能只靠 service 查询。
- runtime role 保持最小权限，禁止超级用户、建库、建角色和 Schema DDL。
- `SECURITY DEFINER` 函数必须固定 `search_path=pg_catalog, public`，撤销默认 `PUBLIC` 权限后再向精确 runtime role 授权，并有权限回归测试。
- 涉及并发、锁、JSONB、复合 FK、数据库函数或角色权限时，SQLite/Mock 测试不能作为完成证据，必须运行真实 PostgreSQL 门禁。

## 13. 前端架构与组件化

### 13.1 职责划分

- `views/` 负责路由参数、页面级加载和领域组件编排，不承载可复用业务转换或重复协议逻辑。
- `components/` 使用 typed props/emits 封装可复用交互；`utils/` 保存纯函数；跨页面权威客户端状态才进入 Pinia。
- 普通 HTTP、SSE endpoint、序列化、响应解包和错误归一化统一位于 `src/api/`。组件和页面不得新增直接 Axios/fetch 或复制 endpoint；现有共享 SSE helper 是唯一例外入口。
- API 字段保持后端 `snake_case`，避免隐式双命名。新增/修改请求和响应必须有明确 TypeScript 类型并同步全部消费者。
- 禁止新增 `any`、非空断言或 `Record<string, any>` 绕过新契约；不可信边界先用 `unknown`，再通过 type guard/schema 归一化。
- 稳定业务状态使用服务端 key/enum/id，不用显示标签、Tab 位置或翻译文本驱动逻辑。

### 13.2 服务端权威与异步正确性

- 服务端是权限、readiness、候选资格、发布、写入结果、任务和审计状态的唯一权威；前端只能做 fail-closed 预检和展示。
- 助手写入成功必须由持久 mutation ledger、proposal/job 和单调 revision 证明；自由文本或本地 optimistic 状态不能作为成功。
- 路由参数和 query 必须响应式处理。同路由 ID 切换时取消旧请求、重置状态，防止旧响应覆盖新场景。
- 统一使用 `safeInternalReturnPath` 处理内部返回地址；禁止各页面自建 URL 安全规则。
- timer、SSE、AbortController、全局 listener 和 CustomEvent 必须在重载/卸载时清理。跨组件事件必须集中命名并定义 detail 类型，禁止继续扩散字符串事件协议。
- 列表、图谱和运行事实必须分页或有界投影；不得为 UI 方便获取完整对象库、全量关系或全部历史。
- 失败和中止必须保留用户草稿；成功只有在服务端确认后才清空。提交按钮要防重复，流式完成要幂等。

### 13.3 交互与可访问性

每个新增或修改交互必须覆盖：

- loading、empty、error、disabled/unauthorized、conflict、success；
- 取消、失败重试、重复提交和后端迟到响应；
- 键盘路径、可读 label、`aria-*`、`role=alert/status`、错误聚焦和 `:focus-visible`；
- `prefers-reduced-motion`，不得用动画作为唯一状态反馈；
- 桌面与窄屏无文字溢出、遮挡和不可达操作；
- 敏感值不进入 localStorage、URL、日志或普通响应状态；
- 用户看得懂的业务表单，不把内部 ID、hash、原始 JSON 或实现细节作为主流程。

新功能不得继续扩大 `ScenarioDetail.vue` 或 `GlobalAssistant.vue` 的独立职责。可独立描述状态、交互、数据加载或转换的部分必须抽为领域组件、composable 或纯函数；但不得借小改动进行无测试的全页重写。

## 14. 代码风格与命名

### 14.1 通用

- 标识符表达业务含义，禁止 `data1`、`temp2`、`handleThing`、`commonUtil` 等含混命名。
- 代码标识符、API key、协议 token 和文件名优先使用英文 ASCII；用户界面和用户可操作错误使用清晰中文。
- 注释解释约束、原因、竞态或安全边界，不复述代码。复杂状态转换要说明不变量。
- 使用项目现有模式，不因个人偏好引入第二套框架、HTTP 客户端、状态库或抽象层。
- 保持原文件换行和局部格式，禁止全文件格式噪声。
- 时间持久化使用 timezone-aware UTC；对外显示时才转换时区。
- stable key / `api_name` 使用可移植小写 token，不从可变显示名临时生成；已发布标识不得复用或偷偷改义。

### 14.2 Python

- 4 空格缩进；模块按现有风格使用 `from __future__ import annotations`。
- 函数、变量、模块为 `snake_case`；类、DTO、异常为 `PascalCase`；常量为 `UPPER_SNAKE_CASE`。
- 新公共函数、service 边界和复杂返回值必须有准确类型；避免把 `Any` 扩散到核心逻辑。
- import 按标准库、第三方、项目内分组。禁止循环依赖和为绕过循环而在任意函数中散布延迟 import；组合根的受控延迟 import 需说明原因。
- 测试命名为 `test_<可观察行为>`，断言行为和不变量，不只断言实现细节。

### 14.3 TypeScript / Vue

- 延续现有 2 空格、单引号、无分号风格；不要对无关文件做格式迁移。
- 变量、函数、composable 为 `camelCase` / `useXxx`；组件、类型、interface 为 `PascalCase`；常量为 `UPPER_SNAKE_CASE`。
- Vue 使用 `<script setup lang="ts">`、Composition API、typed props/emits；模板事件使用稳定 payload。
- 派生状态使用 `computed`，不要用多个 watcher 手工同步同一事实；副作用 watcher 必须有明确清理和竞态保护。
- 共享 CSS 只放 token、reset、布局和可访问性基础；组件样式默认 scoped，禁止为单页需求添加无界全局选择器。

## 15. 测试策略与变更矩阵

测试强度随风险和影响面扩大。最小相关测试通过只是起点，共享内核或跨层契约变更必须运行全量门禁。

### 15.1 通用测试纪律

- Bug 修复必须有失败复现和防回归断言；新功能测试同时覆盖成功与关键拒绝路径。
- 禁止删除、跳过、放宽断言或改 fixture 来掩盖回归。确需修改既有预期时，必须证明需求/权威契约已经改变。
- 架构源码扫描可守护禁词和依赖方向，但不能替代行为、DOM、并发或真实数据库测试。
- 默认测试不得依赖真实外部 LLM/OCR/MCP/邮件；使用受控 mock。明确的集成验收必须显式 opt-in 并记录依赖。
- 不使用生产凭据或客户数据作为 fixture。
- 若环境缺少 PostgreSQL、MinIO、Redis、浏览器或外部服务，必须报告未运行项；不得以替代测试宣称等价通过。

### 15.2 按改动类型验证

| 改动类型 | 最低验证要求 |
| --- | --- |
| 纯后端局部 service/router | 对应 `test_<domain>.py`，随后后端全量 `pytest` |
| capability/core/provider/Agent/REST/MCP/SDK | architecture boundaries、kernel、invoker、protocol consistency、release/provider contract、Agent closure，再跑全量 |
| auth/tenant/permission/credential/security | ACL、external API、assistant context、workflow payload、request-body limit 等相关安全测试，再跑全量 |
| ORM/Catalog/约束/锁 | 相关 Catalog、scope、runtime input、mapping、object tests + 真实 PostgreSQL opt-in |
| Alembic/角色/DDL | single head、隔离库 upgrade/downgrade/upgrade、runtime verifier、最小权限检查 |
| MinIO/上传/大文件/删除 | managed upload、object storage、deletion outbox、validation/template tests + 断线、超限、重复、late PUT、清理重试 |
| Workflow/Action/worker | operations、confirmation、payload security；覆盖双 worker、崩溃点、自动/人工重试和 indeterminate |
| Assistant/compiler/lease | compilation jobs/leases、proposals、compiler regressions、cross-chunk、draft apply safety |
| Dataset/DuckDB/query/mapping | dataset query、mapping、relations、RAG/platform；保持 SQL、时间、内存、并发和行数拒绝路径 |
| 任意前端逻辑/API/type | `npm test` + `npm run build`，同步后端 schema、API 封装、类型和全部消费者 |
| 页面、路由、SSE、权限交互 | 上述前端门禁 + 实际浏览器验证刷新/前进后退/参数切换/取消/竞态/无权/冲突/窄屏/键盘 |
| 文档/配置/命令 | 验证路径、链接和命令与代码一致；同步 README、`.env.example` 和对应权威文档 |

能力内核的重点测试文件包括：

```text
test_capability_architecture_boundaries.py
test_capability_kernel.py
test_capability_invoker.py
test_capability_protocol_consistency.py
test_capability_release_contract.py
test_builtin_capability_providers.py
test_provider_config_contract.py
test_agent_capability_closure.py
```

关键用户流程的 E2E 必须从用户可见入口建立新资源并走到可观察结果，不能只验证预置数据库里存在对象。仓库尚无统一浏览器 E2E 命令，因此涉及产品主链路时，必须运行本地前后端并记录实际浏览器路径；新增自动化后再把稳定命令补入本文件。

## 16. Skill、脚本与外部执行

- Skill 只允许受信、随代码部署的内置包；禁止把租户上传、数据库字符串或 LLM 文本变成任意 Python/PowerShell/shell 执行。
- 修改 Skill 执行边界时必须实施路径 containment、环境变量 allowlist、低权限隔离、超时、输出大小和 CPU/内存限制。
- 密钥不得通过命令行、stdout、stderr 或继承的完整进程环境泄露；只注入任务明确需要的值。
- Skill 包代码视为只读；工作文件必须限制在批准的 workspace/tmp 目录并在失败后清理。
- `ALLOW_UNSAFE_WORKFLOW_NODES`、MCP stdio 和不安全 HTTP 是部署级高风险开关，不得由 API 请求或前端覆盖。

## 17. 文档同步规则

- 安装、启动、端口、环境变量或测试命令变化：同步 `README.md`、`.env.example` 和本文件。
- 架构目标、任务依赖或验收变化：先改 `docs/优化升级任务计划文档.md`。
- Provider、公共能力契约、REST/MCP/SDK 接入变化：同步 `docs/能力平台架构与接入指南.md` 和 SDK README。
- PostgreSQL、MinIO、Redis、DuckDB 职责或生命周期变化：同步存储架构文档和迁移 README。
- `docs/实现计划.md` 保持历史属性；不要把新目标只写进历史文档。
- 问题记录保存实际复现证据，不用未来时态掩盖未修复问题；修复后追加验证结果，不删除历史。
- 文档、代码和测试不一致时，不能只修最容易的一处。必须确定正确目标，并同步所有权威消费者。

## 18. 完成定义（Definition of Done）

只有同时满足以下条件，任务才可以表述为完成：

1. 需求账本中的每一项都有对应实现和可复现验收证据，且非目标未被误改。
2. 用户从约定入口能够完成完整流程；若为 API-only，已有明确契约和接入文档。
3. 未新增行业硬编码、协议旁路、隐式运行数据、跨租户漏洞或客户端权威状态。
4. API、DTO、类型、持久模型、迁移、协议适配、SDK、前端消费者和文档已按影响图同步。
5. 并发、重试、取消、冲突、失败清理、幂等和副作用路径有与风险相称的验证。
6. 相关测试、全量门禁、前端生产构建以及适用的真实基础设施/E2E 已通过。
7. 未运行的门禁、外部阻塞和已知残余风险已明确报告；它们没有被 `DONE`、fallback 或跳过测试掩盖。
8. `git diff` 只包含预期改动，没有覆盖用户工作、生成物、密钥、临时文件、调试日志或无关格式变化。
9. 没有通过削弱测试、安全默认值、权限、Schema、版本固定或错误处理换取表面成功。
10. 最终交付说明包含：改了什么、为何符合目标、验证命令及结果、仍需用户/环境完成的事项。

任何一项不满足时，应准确描述为“已实现但未完成某项验证”或“被某条件阻塞”，不得宣称完整交付。
