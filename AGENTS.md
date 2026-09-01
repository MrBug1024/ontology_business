# 业务场景本体智能平台开发宪法

> 根级 `AGENTS.md` | 版本 1.2 | 生效日期 2026-09-01 | 适用于整个仓库

本文件约束人类开发者与编码 Agent，目标是让需求、实现和验收始终一一对应，并在项目扩大后仍保持通用、低耦合、可并发、可审计。文中的“必须 / MUST”和“禁止 / MUST NOT”不可绕过；“应该 / SHOULD”是默认规则，偏离时必须说明可验证的项目理由。

子目录可增加更具体的 `AGENTS.md`，但不得弱化本文件的租户、安全、统一执行、数据边界和验证要求。永久例外必须先修改本文件。

## 0. 七条硬门禁

1. **先固定需求**：编辑前建立需求账本/影响图，每项有可观察验收；禁止以相似功能替代。
2. **只改必要范围**：保留用户改动，不顺手重构、升级、改默认值或修旁支；新增范围先更新影响图。
3. **行业无关、统一执行**：行业逻辑只进 Provider/用户定义；Agent、REST、MCP、SDK 不复制通用内核。
4. **服务端与租户权威**：校验 principal、tenant/归属及适用 ACL/role/scope；前端、LLM、Redis 不裁决事实，真实密钥不外泄。
5. **并发靠持久契约**：使用幂等、约束、CAS、lease/fencing、intent/outbox；PostgreSQL 只经 Alembic 演进，运行角色不做 DDL。
6. **拒绝巨型文件**：独立状态、交互和领域逻辑拆成有明确所有权且可测试的模块。
7. **验证后才完成**：按风险跑测试、构建及真实 PG/浏览器验收；未运行项如实报告。

## 1. 项目身份与终极目标

### 1.1 项目概述

本项目是受 Palantir Ontology 启发的通用业务能力平台。平台把业务语义、对象与关系、输入输出契约、函数、规则、Action、事件、Workflow 和证据建设为可发现、可版本化、可治理、可审计的能力。

平台不绑定某个行业、客户、固定数据批次或某个 Agent。平台内 Agent 是验证能力的参考客户端，不是业务场景本身，也不是唯一运行入口。验证 Agent、REST、MCP 与 SDK 必须消费同一能力定义，并获得一致的身份、权限、执行和审计语义。

### 1.2 终极目标

业务专家应能在不修改平台通用内核的前提下，建立行业无关的本体与版本化能力契约；治理人工、AI 或导入候选；在 `dev / staging / prod` 解析受控部署；每次调用提供当前输入而不重建能力；通过任意受支持协议统一执行，并在多租户、并发、重试和进程故障下保持权限、幂等、版本、证据与审计正确。

主链路为 `Definition -> Release/Deployment -> Invocation -> CapabilityInvoker -> Agent/REST/MCP/SDK -> Receipt/Audit`。

### 1.3 非目标

- 不把行业逻辑、客户数据、固定批次、DataSource、物理表列、对象路径、凭据或平台内 Agent 变成 Definition/平台本体。
- 不为协议、页面或兼容模式维护第二套业务内核；兼容层必须有范围、测试和退出条件。
- 不把 Redis、浏览器状态、LLM 输出或进程内字典作为权威，也不恢复 SQLite/MySQL 生产平台后端。
- 不因 AI、内部调用或管理员身份绕过发布、副作用、租户和凭据门禁。

## 2. 权威来源与冲突处理

本宪法必须自包含，不依赖可能删除或归档的阶段性方案文档。权威顺序如下：

1. 本 `AGENTS.md`：长期工程原则、架构底线和完成定义。
2. 当前用户明确批准的需求与验收：本次业务目标和范围；若与宪法冲突，先由用户决定修改需求还是宪法。
3. 当前任务中用户明确指定的有效规格/决策记录：只约束对应功能和版本，不自动晋升为宪法。
4. 可执行契约：公共 Schema、协议类型、Alembic migration、数据库约束、配置校验和自动化测试。
5. 入口代码、依赖清单、lockfile、`.env.example` 与 `README.md`：当前命令、技术栈和实现行为的证据。
6. 其他 docs、问题记录、示例和架构图：仅作背景或审计证据，除非用户在当前任务中明确指定。

代码/测试只能证明现状，不能在明确需求冲突时自动成为正确设计。规格、代码与测试冲突时禁止选择最省事的一种或同时新增两套语义；先定位差异，再按上述顺序统一验收。全项目不变量或门禁永久变化时同步本文件；功能级设计只更新当前仍有效的规格。

禁止复制文档中的固定 Alembic head；必须读取实际 single head。任何 `DONE` 标签都不能替代可复现验收。

## 3. 技术栈、目录与命令

### 3.1 当前技术栈

| 层 | 技术与职责 |
| --- | --- |
| 前端 | Vue 3、TypeScript 5.x（精确版本以 lockfile 为准）、Vite 6、Pinia、Vue Router、Element Plus、Axios、Vue Flow、Marked |
| 后端 | Python 3.12、FastAPI、同步 SQLAlchemy 2、Pydantic v2/settings、psycopg 3、Alembic |
| AI / 协议 | OpenAI 兼容接口、LangChain/LangGraph、MCP SDK、httpx |
| PostgreSQL | 租户、权限、定义、发布、目录、任务、审计与控制面权威 |
| MinIO | 不可变上传、Parquet、产物和证据对象 |
| Redis | 可失效、可降级、可重建的缓存 |
| DuckDB | 对已验证受管 Parquet 的进程内、有界、只读查询 |

可执行工具以依赖清单、`frontend/package.json` 与仓库配置为准。不存在的 lint、formatter、type-check、组件测试或 E2E 命令不得被虚构或声称已运行；新增工具必须提交依赖、配置和 lockfile。

### 3.2 目录所有权

- `backend/app/`：`main.py` 组合应用，`config.py` 管配置，`database.py` 管会话/head，`models.py` 为现有 ORM 大文件，`schemas.py` 与 `*_schemas.py` 为 DTO；`routers/` 适配 HTTP，`services/` 承载应用服务/通用内核；确有正式能力缺口时，`providers/` 才承载可选的版本化领域实现。
- `backend/{migrations,sdk,skills,mcp_servers,scripts,tests}/` 分别承载 Alembic、薄客户端、受信包、MCP 服务、验收脚本和测试。
- `frontend/src/{api,router,stores,types,utils,components,views}/` 分别承载 HTTP/SSE、路由、跨页状态、类型、纯转换、复用组件和页面编排；`frontend/tests/` 存回归。
- `docs/` 保存当前规格和证据，但默认不是权威来源。

`.venv/`、`node_modules/`、`dist/`、`__pycache__/`、`.pytest_cache/`、`*.pyc`、`*.tsbuildinfo`、生成的 `frontend/vite.config.js` / `vite.config.d.ts`、`.runtime/`、`.tmp*`、`.codex-*` 与日志都是生成物或本机状态，禁止手工编辑或提交；`frontend/src/env.d.ts` 是源文件。

`backend/.env` 和生产环境文件可能含密钥。除非用户明确要求诊断本机配置，编码 Agent 不得直接打开、输出、改写或提交其内容；受控 app/test 可通过 `Settings` 消费配置，但不得泄露值。配置契约只改 `backend/.env.example` 与 `Settings`。

### 3.3 标准 PowerShell 命令

从仓库根目录执行：

```powershell
# First setup
python --version  # Supported project environment must be Python 3.12.x
node --version
npm --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\backend\requirements.txt
python -m pip install 'pytest>=8.3,<9'
if (-not (Test-Path -LiteralPath .\backend\.env)) {
    Copy-Item -LiteralPath .\backend\.env.example -Destination .\backend\.env
}
npm --prefix .\frontend ci

# Database migration, using migration owner/admin
python -m alembic -c .\backend\alembic.ini -x use_admin=1 upgrade head
python -m alembic -c .\backend\alembic.ini current

# Backend
python -m uvicorn app.main:app --app-dir .\backend --host 127.0.0.1 --port 8000

# Frontend in another terminal; keep proxy equal to backend port
$env:VITE_API_PROXY_TARGET='http://127.0.0.1:8000'
npm --prefix .\frontend run dev

# Regression and build
$env:PYTHONPATH=(Resolve-Path .\backend).Path
python -m pytest .\backend\tests -q
npm --prefix .\frontend test
npm --prefix .\frontend run build
```

创建 venv 前必须确认解释器为受支持的 Python 3.12.x；Node/npm 版本也要记录并满足当前依赖的 engines，不能用本机偶然可运行代替支持声明。`backend/requirements.txt` 当前只含运行依赖，因此测试环境额外安装 pytest 8.x；以后若增加受版本控制的 dev requirements，改从该清单安装。`npm ci` 不可用时才用 `npm install`；依赖未变化时禁止重写 lockfile。迁移运行角色不得执行 DDL。改变端口时同步代理和文档。

真实依赖验收：

```powershell
$env:RUN_POSTGRESQL_INTEGRATION_TESTS='1'
python -m pytest .\backend\tests\test_catalog_postgresql_concurrency.py -q
python .\backend\scripts\verify_alembic_roundtrip.py
python .\backend\scripts\verify_postgresql_runtime.py
```

真实 PostgreSQL 并发测试只在专用集成数据库运行；迁移往返脚本只能使用其自行创建的隔离数据库。`verify_postgresql_runtime.py` 是无业务 fixture 假设的只读部署检查，只验证当前 Schema、运行角色权限、MinIO 和可选 Redis 健康；它不创建、修改或删除业务数据。

## 4. 每次开发的强制流程

### 4.1 首次编辑前

1. 查看 `git status`，识别并保留用户已有改动。
2. 阅读本文件、当前有效规格、入口、调用方、被调用方和现有测试；搜索路由、DTO、状态值与错误协议的全部消费者。
3. 在任务计划、进度说明或工作记录中建立需求账本。跨层任务使用完整模板；局部任务可合并字段，但不得省略验收与保持不变项：

```text
需求 ID | 原文目标 | 非目标 | 可观察验收 | 影响层/文件 | 保持不变项 | 测试证据
```

4. 建立影响图：`需求 -> UI/协议入口 -> application service -> model/storage -> worker/external I/O -> tests/docs`。明确哪些层不应改变。
5. 用户已明确“做什么、怎么做”时不得自行换成相似方案；发现事实或宪法冲突才暂停并给出证据。

### 4.2 实现中

- 只改账本与影响图内的内容。发现新的必要消费者时，先扩展账本并说明原因；不必要的旁支缺陷只记录。
- 用最小影响面完成一条端到端纵向链路，不创建只对某入口有效的旁路。
- Bug 修复应先加入失败复现，或至少在同一改动中加入会对旧错误行为失败的测试。
- 不做无关重命名、全文件格式化、目录搬迁、默认值调整、依赖升级或大范围“顺手清理”。
- 不通过放宽类型/Schema、删除校验、吞异常、静默 fallback、跳过测试或复制旧逻辑换取表面成功。
- 不留伪实现、永真占位、无负责人 TODO，或“返回成功但未持久化”的状态。
- 用户可见功能必须形成产品闭环；API-only 能力必须在需求和契约中明确。后端对象存在但用户无法配置、发现或调用，不算完成。

### 4.3 交付前

1. 逐项对账需求、代码与测试，确认没有把 A 做成 B。
2. 按第 10 节运行与风险相称的验证，并检查相邻共享流程。
3. 覆盖适用的加载、空、错误、无权、冲突、取消、重试、迟到响应与成功状态；N/A 项说明原因。
4. 复查 `git diff` / `git status`，排除密钥、生成物、调试输出、用户改动覆盖和无关格式噪声。
5. 报告实际命令、结果、未运行项及原因；禁止把“已写代码”表述为“验收通过”。

## 5. 不可破坏的领域架构

### 5.1 三层身份

```text
Definition plane  -> definition_hash
Deployment plane  -> deployment_fingerprint
Invocation plane  -> data_context_fingerprint
```

- Definition 保存业务语义、能力、端口、Schema、策略、Provider identity 和验证要求，不保存客户本次数据。
- Deployment 解析 Definition、Release、tenant、服务端环境与受信连接身份。
- Invocation 保存 Actor、typed inputs、本次受管引用、确认、幂等、correlation 与审计。
- 更换数据必须改变 `data_context_fingerprint`，不得改变 `definition_hash`。
- `dev` 可解析受治理的 live definition；`staging/prod` 使用发布的不可变快照。调用方不得越权选择服务端环境。
- 发布后修改 ORM、嵌套 JSON、端口或绑定不得反向改变旧 Release。

### 5.2 数据与运行输入

- 零数据能力必须可建模、验证、发布和调用；readiness 不得强制 DataSource、Dataset 或 Mapping。
- 建模资料只用于理解、结构发现和候选生成，禁止隐式升级为正式运行数据。
- 正式输入来自本次显式 typed input/受管附件、Agent 专属正式连接或第三方显式受管输入/专属连接。
- 禁止从建模 `DataSource`、旧 `Agent.data_source_ids`、`ScenarioDatasetBinding`、历史对话或环境变量隐式补数据。
- `DatasetHead` 只用于选择；调用开始固定为不可变 `DatasetVersion`，并发更新使用 expected version/CAS。
- 公开调用只接受受管逻辑引用或受信 `binding_key`，不接受连接串、任意 SQL、物理表列、bucket、object key 或凭据。

### 5.3 统一执行与 Provider

- Agent、REST v2、MCP 和 SDK 只做认证、协议转换、统一调用与 Receipt 渲染；不得复制 capability/readiness、ACL、数据解析、Provider、preview/confirmation/idempotency 或 provenance。
- 行业字段、算法、场景工具、grounding、历史 alias 和特定逻辑模型只能进入 `backend/app/providers/<provider>/`。
- 通用内核、Agent shell、协议层与通用 prompt 禁止出现行业名、固定 namespace、业务字段、表名或工具名分支。
- Provider 按精确 `(provider_key, provider_version)` 由受信代码静态注册；数据库不得指定 Python 路径，禁止动态 `import`、`eval` 或 `exec` 租户代码。
- Provider input/config Schema 必须封闭；注册表单例不持有请求状态；版本缺失/不匹配 fail closed，不回退最新版。
- 有数据 Provider 用受管版本 A/B 证明 Definition 不随数据变化；同时覆盖零数据反例与 Agent/REST/MCP 一致性。
- 公共 API、协议 token、`api_name`、表和持久状态一旦发布即稳定；破坏性变化需要显式版本、迁移、弃用窗口与兼容测试。

### 5.4 候选、AI 与发布

- AI、人工和导入仅 provenance 不同，进入同一候选治理状态机。
- LLM 输出只能形成候选、草稿、解释或预演，不能证明正式写入或副作用成功。
- 晋级由服务端确定性校验、依赖闭包、风险、revision 和质量 fingerprint 决定；前端不得推断。
- 编辑/重新校验/晋级携带 expected revision；陈旧写入冲突，不做 last-write-wins。批量晋级全成或全不成。
- Action 正式化不等于自动激活，发布不等于绕过运行确认。

## 6. 模块化、硬编码与代码边界

### 6.1 允许的依赖方向

```text
Backend: protocol/router -> application service -> domain contract/port
        infrastructure/provider adapter --------> domain contract/port

Frontend: route/view -> domain component/composable -> api + types + pure utils
```

Domain contract 不依赖 FastAPI、ORM、具体 Provider 或外部 SDK；Provider/基础设施实现 port，具体类型不泄露回协议层；组合根负责装配。前端 `api/types/utils` 不导入 view，纯 util 不访问 DOM、router 或网络。新增边界或改变方向时扩展架构扫描/导入测试。

后端职责：

- `main.py` 只做组合、middleware、router、lifespan 与 worker 启停。
- `routers/` 只做认证依赖、DTO、协议转换、HTTP 错误映射和明确事务边界。
- `services/` 做用例编排和通用领域策略；低层 service 不依赖 router。
- `schemas.py` 与 `*_schemas.py` 定义 DTO；ORM 模型只定义持久结构/约束；协议无关 contract 不导入 HTTP/ORM/具体 Provider。
- SDK 是薄客户端，不解析客户数据或复制 Provider 逻辑。

现有 `routers/assistant.py`、`routers/scenarios.py`、`services/scenario_model_compiler.py`、`models.py`、`ScenarioDetail.vue` 与 `GlobalAssistant.vue` 是遗留巨型文件，不是范例。禁止继续加入可独立命名、测试或复用的新子领域；由旧入口薄编排新 router/service/schema/component/composable/pure util。不得用含混的 `helpers.py`、`common.py` 转移耦合，也不得借小改动无测试重写整页/模块。新增函数接近 80 行或文件接近 800 行时优先拆分；确实不可拆需说明理由。

### 6.2 禁止不受治理的硬编码

- 禁止真实 tenant/user/scenario/agent/resource ID、凭据、客户路径、Host/端口和客户数据进入生产代码。
- 平台通用内核不得按客户名、行业名、中文标签、UI 文案、Tab 位置或样例数据分支。
- 客户/外部数据源的物理 DB、表列、SQL、bucket/object path 不得进入 Definition、公开契约或通用内核；平台 ORM、迁移和受控连接器可使用自己的物理结构，但必须隔离、参数化、有权限边界。
- 业务规则/阈值/映射进入受治理 Definition、Provider config 或模型；行业逻辑/alias 进入版本化 Provider；样例进入 `tests/fixtures` 或 `examples`。
- 稳定协议枚举、正则和安全上限可作为集中、命名、可测试的常量；部署可变项进入 `Settings` + 环境变量 + `.env.example`。不得把安全边界反向变成任意客户端输入。

### 6.3 DTO 与错误

- 控制面/协议 envelope 使用有界 Pydantic DTO：关闭额外字段，使用枚举/Literal，限制长度、数量和数值，并校验跨字段关系。
- 用户定义的动态 typed inputs 可用 mapping，但必须限制整体大小，并由已解析的封闭 JSON Schema 再验证；公开 route 不接收含义不明的裸 dict。
- 公开输出使用明确 Schema。持久化、签名或 hash 的值先 canonicalize，拒绝 NaN/Infinity/任意对象隐式 `str()`；不可变契约使用 frozen 数据结构与 domain-separated hash。
- 领域异常在边界映射为稳定安全错误；不得泄露 traceback、SQL、连接信息、资源存在性或供应商原始错误。宽异常只在 worker/清理/外部边界捕获，并记录内部上下文。

### 6.4 前端组件化与交互

- `views/` 只做路由、页面加载与领域组件编排；复用交互进 typed component，复用状态进 composable，转换进 pure util，真正跨页面权威客户端状态才进 Pinia。
- HTTP/SSE endpoint、序列化、响应解包和错误归一化统一在 `src/api/`；页面/组件不得新增直接 Axios/fetch 或复制 endpoint。
- API 字段沿用后端 `snake_case`；新/改请求响应有明确类型。禁止用新增 `any`、非空断言或 `Record<string, any>` 绕契约；不可信边界用 `unknown` 后归一化。
- 服务端是权限、readiness、候选资格、发布、任务、写入与审计的唯一权威；本地 optimistic 状态和助手文案不能证明成功。
- route/query 必须响应式；上下文切换取消旧请求并防止迟到响应覆盖。内部返回地址统一 `safeInternalReturnPath`。
- timer、SSE、AbortController、listener、CustomEvent 在重载/卸载时清理；跨组件事件集中命名并定义 detail 类型。
- 列表/图谱/历史分页或有界；失败/中止保留草稿，服务端确认成功后才清空，提交防重复。
- 交互按适用风险覆盖 loading/empty/error/unauthorized/conflict/success、取消/重试、键盘、label/aria、焦点、reduced motion 和窄屏无遮挡；N/A 项说明原因。
- 不可信 Markdown 禁止 `v-html`，统一使用 `SafeMarkdown`。内部 ID、hash、原始 JSON 和实现细节不得成为普通用户主流程。

## 7. 租户、安全与外部执行

- 每个受保护读写验证 authenticated principal、tenant 和资源归属；只有 scenario-scoped 资源才要求对应场景 ACL，role/scope 按入口契约适用。deny 优先 allow，公共资源对非所有者只读，缺失权限按无权处理。
- 跨租户与不存在资源沿用防枚举语义；错误、日志和时序不得泄露目标细节。worker 必须恢复可审计 execution principal，发起人失效后不得匿名或升级 owner。
- 浏览器 HttpOnly session、外部 `X-API-Key` 和 Agent MCP token 是独立认证域，不互相回退或复用 hash domain。token 原文只在创建时返回一次，数据库保存域分离 hash。
- Cookie 认证的状态写请求必须有同源 Origin/Referer 或 CSRF token 防护；生产必须启用 Secure Cookie，并在登录、密码重置和权限敏感变化后轮换/撤销会话。登录、验证码、重置和 token issuance 使用跨进程持久限流，不依赖单进程计数。
- 所有 token 类型（含浏览器 session）必须使用显式 domain separation；遗留无前缀格式只能通过有版本、可回归的兼容迁移退出，不得成为新 token 的模板。
- 真实 password/API key/token/header/env/key ring/连接 URL/明文 workflow input 不进入 repr、日志、错误、prompt、snapshot、Receipt、fixture、URL、localStorage 或普通前端状态。安全测试可使用不可复用的 synthetic/ephemeral 值；缺少部署加密 key 时 fail closed。
- 受治理契约声明为 side-effecting、并对外部系统/业务事实产生不可逆或高风险影响的 Action/Workflow 执行 `preview -> server confirmation -> execute`；确认绑定 tenant、principal、correlation、input hash、Definition、Deployment 与 expiry。
- 上述能力使用持久幂等键和 DB unique/CAS；结果未知进入 reconciliation/indeterminate，禁止盲重放。审计、lease/checkpoint 和内部状态推进不要求用户确认，但仍需认证、事务和适用幂等。
- 外部 HTTP/MCP 保持 HTTPS、SSRF、DNS pinning、私网 allowlist、无自动重定向；stdio、任意脚本、不安全 HTTP 默认关闭且只能由部署配置开启。
- Agent/connector SQL 仅允许策略支持的单条、参数化、只读查询，并受 Schema allowlist、行数和超时限制。
- 上传校验权限、声明/实际字节、格式、解压上限、TTL 与内容身份；客户端扩展名/MIME/UI 校验不是安全边界。
- Skill 只允许受信内置包，禁止租户/数据库/LLM 文本变成任意代码。运行时包目录只读，工作文件限批准的 workspace/tmp；修改执行器需路径 containment、环境 allowlist、低权限隔离、超时、输出/CPU/内存上限，密钥不进命令行/stdout/stderr。

## 8. 事务、并发、存储与迁移

“高并发”指跨请求、跨进程和失败重试下仍正确，不等于简单增加 `async`。

### 8.1 事务与 worker

- 常规低层 service 只 `add/flush`，最外层 application/router/worker 明确 commit/rollback；只有已有文档和测试的原子 orchestration 可内部 commit。
- 网络、LLM、MinIO、MCP、子进程等长 I/O 不无必要持有事务/行锁。检查后写入由 unique/check/FK、`FOR UPDATE`、CAS/expected revision 或 IntegrityError 处理闭合；冲突返回稳定 409/可重试结果，不静默覆盖。
- 跨进程正确性不依赖 Python 全局 dict/Lock/进程内队列。单事务内完成的短任务至少使用 DB 原子 claim、bounded batch 和 crash-safe 提交；跨事务或包含外部 I/O 的长任务还必须使用 lease token/expiry/generation/fencing、续租和恢复，按需 `SKIP LOCKED`/CAS。
- lifespan worker 会随 API 进程扩展；必须证明多实例不重复副作用。自动重试复用 execution lineage/派生幂等键，人工重试创建新审计 lineage。
- 同步 DB/MinIO/LLM/子进程 I/O 不阻塞 async event loop；使用同步 endpoint/threadpool、`asyncio.to_thread` 或独立 worker并保持有界并发。外部调用有连接/读取/总超时、取消、清理和有限重试。
- request/correlation ID 贯穿协议、任务和外部调用；结构化日志不含密钥。以有界基数指标监控延迟、queue depth、claim、lease expiry、retry 和 indeterminate；共享查询/热路径变化提供 query count、延迟或负载证据。

### 8.2 跨存储

- PostgreSQL/MinIO 双写使用 durable intent、outbox、lease/fence 和可重入清理；禁止先删对象再提交权威元数据。
- MinIO key 由服务端生成，至少 tenant scoped，按资源需要加 scenario/generation；记录 bytes、SHA-256、version/etag 和稳定 `minio://` 身份。大文件流式分块，不一次性读内存。
- Redis 只加速，不承担唯一锁、授权、幂等或任务状态；失败时正确性仍由 PostgreSQL/MinIO 提供。
- DuckDB 只处理已验证 Catalog/manifest 和受管 Parquet，保持 query timeout、memory/thread/temp/cache/row/concurrency 上限。
- 新连接器缓存有锁、容量、淘汰、连接/语句超时和 dispose；禁止新增无界 engine/cache dict。

### 8.3 PostgreSQL 与 Alembic

- PostgreSQL 是唯一生产平台数据库；生产逻辑/迁移不新增 SQLite/MySQL 分支。
- ORM、索引、约束、权限或持久字段变化新增 Alembic revision，并同步 `backend/app/database.py` 的 `POSTGRESQL_SCHEMA_REVISION`；不得改写可能已应用的历史迁移。
- 保持 single head；并行分支显式 merge。`init_db`/数据库初始化只校验 head/结构，不 `create_all`、自动补列或执行 DDL；lifespan 可做有文档、幂等的授权 bootstrap、清理和任务恢复，但不得改变 schema。
- 回填必须确定、可审计、幂等或 fail closed，禁止按名称、前缀、数量或“最可能对象”猜归属。不可逆 downgrade 主动拒绝并说明前置条件。
- 租户关系优先由复合 FK/unique/check 闭合。runtime role 保持最小权限，禁止超级用户、建库/角色和 Schema DDL。
- `SECURITY DEFINER` 固定 `search_path=pg_catalog, public`，执行 `REVOKE ALL ON FUNCTION ... FROM PUBLIC` 后再精确授权并测试。
- 并发、锁、JSONB、复合 FK、数据库函数或角色权限变更必须通过真实 PostgreSQL；SQLite/Mock 绿灯不等价。

## 9. 代码风格与命名

- 标识符表达业务含义；禁止 `data1`、`temp2`、`handleThing`、`commonUtil`。标识符、协议 token 和文件名优先英文 ASCII；用户界面/可操作错误使用清晰中文。
- 注释解释原因、不变量、竞态或安全边界，不复述代码。遵循现有框架和局部格式，不引入第二套 HTTP/状态/抽象框架，不做无关全文件格式化。
- 持久时间使用 timezone-aware UTC；显示时转换。stable key/`api_name` 使用可移植小写 token，不从显示名生成；已发布标识不复用或偷偷改义。
- Python：4 空格、现有模块风格的 future annotations；`snake_case` 函数/变量、`PascalCase` 类/DTO/异常、`UPPER_SNAKE_CASE` 常量；公共边界有准确类型，import 按标准库/第三方/项目分组。
- TypeScript/Vue：2 空格、单引号、无分号；`camelCase`/`useXxx`、`PascalCase` 组件/类型、`UPPER_SNAKE_CASE` 常量；使用 `<script setup lang="ts">`、Composition API、typed props/emits；派生状态优先 computed，副作用有清理。
- 测试命名 `test_<可观察行为>`，断言行为和不变量，不只匹配实现源码。

## 10. 测试策略与验证矩阵

测试强度随风险扩大。先跑最小目标测试；共享内核、公共契约、权限、模型或主流程变更再扩大到全量。

| 改动 | 最低验证 |
| --- | --- |
| 文档/注释/无行为配置 | 链接/命令/配置一致性、`git diff --check`；行为测试可 N/A 并说明 |
| 后端局部 service/router | 对应 `test_<domain>.py`；影响共享行为时后端全量 |
| capability/provider/Agent/REST/MCP/SDK | architecture、kernel、invoker、protocol/release/provider contract、Agent closure + 全量 |
| auth/tenant/credential | ACL、session、CSRF/Origin、持久 rate-limit、external API、context/payload/request-limit + 全量 |
| ORM/Catalog/Alembic/权限 | Catalog/scope/input/mapping/object tests + 真实 PG；single head、隔离往返和最小权限；fixture verifier 仅专用环境 |
| MinIO/上传/删除 | managed upload、object storage/outbox；覆盖超限、断线、重复、late PUT、清理重试 |
| Workflow/Assistant/worker | operations/confirmation/payload、jobs/leases/compiler；覆盖双 worker、崩溃、重试、indeterminate |
| Dataset/DuckDB/query | dataset/query/mapping/relations/RAG；覆盖 SQL、时间、内存、并发和行数拒绝 |
| 前端逻辑/API/type | `npm test` + `npm run build`；同步后端 Schema、API、类型与全部消费者 |
| 页面/路由/SSE/权限交互 | 前端门禁 + 浏览器验证刷新/前进后退/参数切换/取消/竞态/无权/冲突/窄屏/键盘 |

测试纪律：

- Bug 测试先证明旧行为会失败；新功能覆盖成功与关键拒绝路径。
- 禁止删除/跳过/放宽断言或改 fixture 掩盖回归。架构源码扫描不能替代行为、DOM、并发和真实数据库测试。
- 默认测试 mock 外部 LLM/OCR/MCP/邮件；真实集成显式 opt-in。禁止生产凭据和客户数据进入 fixture。
- 若 manifest 无浏览器 E2E，产品主链路需运行本地前后端并记录实际浏览器路径；已有脚本时以 manifest 为准。
- 缺少 PostgreSQL、MinIO、Redis、浏览器或外部服务时报告未运行项，不得用替代测试宣称等价通过。

## 11. 文档同步与完成定义

同步规则：

- 命令、端口、环境变量或依赖变化：同步 `README.md`、`.env.example`、manifest/lockfile 与本文件中受影响内容。
- 全项目不变量、目录所有权或安全门禁变化：同步本文件。任务级设计只写入用户为该任务指定的当前有效规格。
- 公共能力/Provider/REST/MCP/SDK 变化：同步代码 Schema、协议一致性测试、SDK README 和当前仍维护的接入文档（若存在）。
- 存储/生命周期变化：同步 migration、运行时验证脚本、迁移 README 和当前仍维护的存储文档（若存在）。
- 可删除的阶段计划、历史方案和问题记录不得成为运行或解释宪法的唯一信息源。问题记录保留复现证据，修复后追加验证，不删除历史。

只有同时满足以下条件才可宣称完成：

1. 需求账本逐项有实现和可复现证据，非目标保持不变，用户可从约定入口完成流程；API-only 能力已有明确契约。
2. 影响图中的 API、DTO、类型、模型/迁移、协议、SDK、前端与维护中文档已同步，且未破坏本文件不变量。
3. 已按第 10 节通过必要测试、构建和适用的真实基础设施/E2E；未运行项与残余风险已明确报告。
4. `git diff` 只有预期改动，无用户工作覆盖、密钥、生成物、调试输出、无关格式变化或被削弱的安全/测试。
5. 最终说明包含改动、需求对账、验证结果以及仍需用户或环境完成的事项。

任何一项不满足时，应准确表述为“已实现但尚未完成某项验证”或“受某条件阻塞”，不得宣称完整交付。
