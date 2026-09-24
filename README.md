# 业务场景本体智能平台（Ontology Business Agent Platform）

一个受 **Palantir Ontology** 启发的通用业务能力平台。核心理念是：**不绑定任何特定行业，也不把某一批数据或平台内 Agent 当成业务场景本身**。
平台把业务语义、输入输出契约、规则、操作和工作流建设成可版本化、可治理、可由任意 Agent 或客户端调用的能力；平台内 Agent 是验证这些能力的参考客户端。

长期工程边界和完成定义以根级 [AGENTS.md](./AGENTS.md) 为准；能力契约、可选 Provider 扩展和第三方接入方式见 [能力平台架构与接入指南](./docs/能力平台架构与接入指南.md)。

```text
场景定义版本
  → 本体 + 能力 + 输入输出端口 + 策略 + 建模/评测证据
  → 人工创建场景正式发布，显式启用、停用、退役
  → 按租户、发布身份、受管数据版本和连接器绑定解析
  → 每次调用提供文本、文档、结构化参数或新的业务数据
  → CapabilityInvoker 统一执行
  → 验证 Agent / REST / MCP / SDK 消费同一能力
```

## 功能特性

部署变量 `RUNTIME_ENVIRONMENT` 只描述进程部署配置。开发和上线使用各自独立的数据库、MinIO、Redis 与密钥配置；平台内没有按 dev/staging/prod 划分的数据、权限、发布或运行任务。业务授权始终由真实人员、工作区成员和场景 ACL 决定。

场景发布由人工在“发布与接入”创建，冻结当前能力；创建后停用，由人工启用。正式对外调用只消费已启用版本，可显式固定 `release_id`。停用阻止新调用，退役后可从活动列表删除，历史快照和审计保留。建模验证使用当前定义，与服务器部署模式无关。

验证会话采用纯文本消息：普通分析直接执行，需要副作用授权时回复“确认”，业务审批回复“同意”或“驳回”，可附佐证文件。多项待办必须带编号；实际人员、权限、版本、期限和幂等均在服务端检查。异步完成结果留在原会话，正文逐段持久化并经 SSE 交付，刷新后恢复。

- **业务场景 / 本体建模**：使用行业通用术语定义对象类型、属性、关系类型、对象实例与关系实例，支持命名空间、主键、枚举、约束、生命周期和来源信息。
  - **数据源**：
  - 版本化数据集：PostgreSQL Catalog 管理元数据，MinIO 保存不可变文件与 Parquet，DuckDB 执行只读查询。
  - PostgreSQL 连接器：表浏览与受控只读 SQL；PostgreSQL 也是平台控制面唯一关系型存储。
  - 资料库还支持 MySQL 只读结构调查和受管 SQLite3 快照（`.db` / `.sqlite` / `.sqlite3`，最大 32 MB）。SQLite3 文件上传后保存在 MinIO 并校验内容身份，不接受服务器本机路径；这两类调查来源不自动成为正式运行连接器。
  - 文件桶（file bucket）：上传 Excel / Word / Markdown / PDF / 图片，自动解析入库用于 RAG 检索；验证 Agent 的附件先登记持久上传任务、流式写入 MinIO，再由后台 worker 解析，页面请求不等待解析完成。
- **技能（Skill）**：安装受控的本地能力，供已配置的操作或工作流调用；内置 `ocr-parser`（OCR 文档解析）与 `data-analyzer`。
- **MCP 服务**：接入 Model Context Protocol 工具服务（SSE / Streamable HTTP，以及由运维显式开启的 stdio）；支持表单配置请求头，也支持批量导入常见客户端的 `mcpServers` JSON。
- **LLM 配置**：OpenAI 兼容协议（OpenAI / DeepSeek / 通义 / Ollama / vLLM…），多配置、可设默认、可测试连通性。
- **业务能力**：用结构化表单配置无副作用函数、可预演操作、规则、事件和可视化工作流，无需手写 JSON。
- **验证 Agent**：平台内用于验证场景能力、模型、运行输入和证据链的参考客户端。Agent Turn 可引用仍在上传/解析的持久任务，消息先落库并返回 `202 Accepted`，后台再准备输入和执行；页面可恢复 SSE 进度，并通过 revision 控制取消和重试冲突。是否可验证按能力契约动态判断；没有数据端口的能力不要求数据源或映射。
- **AI 对话**：场景内可选择完整建模、本体、映射、业务能力、工作流、只读解释或操作预演；完整建模会生成带来源证据、冲突检查和原子确认的跨资源变更清单。带附件的全局顾问消息会先连同占位回复和持久请求台账原子落库，再由后台等待附件并执行，刷新后可恢复、取消或显式重试；重试创建父子审计链，不改写旧终态。文档全文和表格原始行只作为服务端索引/查询来源，LLM 只接收有界检索片段、引用元数据和受限工具结果，不能把未检索部分表述为已阅读。
- **任务中心**：集中处理工作流状态、重试和人工审批；运行时内部保留权限、连接解析和定义快照等安全内核，但不作为独立业务菜单暴露。

## 业务蒸馏与场景专属接入

导航顺序为资料库 → 业务蒸馏 → 场景能力 → 验证中心 → 发布与接入。资料库沿用 `/data-sources` 路由，统一管理业务理解和建模所用的长期来源及蒸馏交付物；`/business-distillation` 按业务场景组织协作对话。业务蒸馏是业务调查与认知交付流程，不是大模型蒸馏；平台控制面数据库始终使用 PostgreSQL，资料库中的 SQLite3 仅表示用户提供的外部只读业务快照。

1. 在顶栏选择业务场景，从“新建对话”输入业务问题。已有业务可提供历史数据、历史结果、知识文档或口述描述，由 AI 逆向核对真实 ER、流程、规则和痛点；新想法可直接描述目标和逻辑，由 AI 调查可行性、价值、边界、ER 与流程。左侧只列出当前场景的会话；切换场景不会修改已有对话的归属。AI 结合连续上下文逐步明确受益者、痛点、目标结果和业务边界。
2. 需要长期保存的历史文件、结果快照、过程记录和数据库连接统一在资料库维护，蒸馏页选择已有资料供工具调查。对话输入区可上传临时分析附件，显示可用期限，附件不自动登记到资料库。顶栏“业务系统”配置网页链接、登录账号密码及授权范围。Agent 可用真实浏览器登录、读取动态页面与表格、点击导航、填写查询条件；凭据独立加密保存，由后端注入登录控件，不进入模型、项目文档或对话。
3. AI 对话设置可选择共享模型、调查工具、受信技能方法及 MCP 只读资料。技能读取方法说明，MCP 通过 resources/list/read 调查文本资料；不支持的协议或内容会明确报错。AI 按需调用并显示实际调查步骤与结果。歧义以问题卡交给人，提问后本轮停止，收到回答才继续。对话和工具记录持久保存，刷新可恢复；取消、超时恢复和多实例处理使用 PostgreSQL claim、lease 与 fencing。
4. 中间按业务价值、ER、流程、血缘、历史案例、证据与待澄清页签展示产物，右侧持续对话。无产物时只有简洁空态；AI 返回的当前版本提案直接显示为“待采用”，人工在对话中纠正或采用。窄屏可切换产物与对话。项目或证据变化后拒绝采用陈旧建议，没有逐主题签字或业务建模表单。
5. 保存到资料库时，通过简短引导选择继续建设、调整方向或暂缓建设，可补充决定依据，再由人工确认保存。每个版本原子保存为不可变 PostgreSQL 业务交付文档，可下载 Markdown、现状/目标流程 Mermaid、ER、血缘、JSON 业务契约及证据身份清单。这些有界文档不伪装成 MinIO 上传文件，也不代表 AI 模型已经被训练或蒸馏。
6. 场景顾问/模型编译器读取该场景明确交接的最新基线，保留来源、用途、未决问题及停止判断。历史未归属对话仍可访问，其成果可明确复制到目标场景后保存到资料库。

平台设置位于侧栏底部齿轮，通用、模型、工具、技能、MCP 在全局弹窗中管理；配置仍按工作区隔离。规范与模板附件归资料库管理。智能业务顾问只在 `/scenarios/:id?stage=...` 场景建模页出现；业务蒸馏 AI 和验证中心 Agent 保持独立职责、资源选择和会话状态。

交互与阶段职责见 [业务蒸馏协作设计](./docs/business-distillation-design.md)。

MySQL 调查连接要求受信 TLS，并由部署者通过 `LIBRARY_MYSQL_ALLOWED_HOSTS` 指定精确主机名单（逗号分隔，无通配符）。资料库密码使用现有 `WORKFLOW_PAYLOAD_ENCRYPTION_KEYS` / `WORKFLOW_PAYLOAD_ACTIVE_KEY_ID` 密钥环派生的独立加密域保存；缺少有效密钥时拒绝保存凭据。已有 PostgreSQL 连接仍可读取，显式编辑保存后转为加密配置；接口不返回明文密码，编辑时留空保留原密码。数据库调查支持结构发现及有界样本读取（PostgreSQL、MySQL 和受管 SQLite 快照）：工具仅接受返回的表/字段引用和参数化过滤，不接受 SQL。可对实际样本核对单键/复合键，保留多匹配、未匹配、空值与样本局限，不以样本唯一宣称总体唯一。


浏览器调查由部署显式启用：安装依赖后运行 `python -m playwright install chromium`，设置 `DISTILLATION_BROWSER_ENABLED=true`；`DISTILLATION_BROWSER_MAX_SESSIONS` 限制每个 worker 进程的浏览器数量（默认 2）。使用独立的、低权限应用运行账号，保持 Chromium sandbox。网站授权与现有工作流负载密钥环采用不同加密域，缺少密钥时拒绝保存。

浏览器网络只访问配置的同源路径，复用浏览器适配器自身的 HTTPS/私网白名单及 DNS 固定传输；所有浏览器 HTTP 经受控传输，无自动重定向；POST 只允许当前登录操作的表单地址/授权登录接口及明确声明的只读查询接口。图片、媒体、下载、WebSocket、服务工作线程和跨站请求不用于调查。验证码、多域 SSO、需执行副作用的页面明确交给专家协调，不宣称可以无人值守登录任何系统。会话仅本轮有效，取消/权限变化拒绝继续；登录中断不自动重放。

受信方法位于 `backend/skills/business-discovery/SKILL.md`。它指导 Agent 从历史结果逆向输入、规则与过程，核对复合身份和反例，并通过问答澄清，不生成固定行业结论。`review_business` 返回方法，浏览器/样本/文件工具返回实际观察，`record_human_statement` 显式引用本轮陈述，`ask_human` 暂停等待，`propose_document` 形成待采用产物。接口、数据样本与访问凭据不会变成正式能力的运行绑定。

REST API v2 与 Capability MCP 沿用现有发布机制，但集成密钥现在必须绑定单个业务场景。服务器对能力目录、调用、回执、审批/交互和本次附件统一收窄范围；知道其他场景资源 ID 也不能扩大授权。升级迁移会撤销历史无场景密钥并记录原因，管理员需在“发布与接入”选择场景重新签发。正常停用/退役方式不变；保留蒸馏证据或外部附件归属时，永久删除计划会明确提示阻断原因。

部署前由迁移角色执行 `python -m alembic -c backend/alembic.ini -x use_admin=1 upgrade head`，读取实际 single head，不复制固定 revision。运行角色不得 DDL。新增隔离验收如下（实际 Python 应为 3.12.x）：

```powershell
$env:PYTHONPATH=(Resolve-Path .\backend).Path
$env:RUN_POSTGRESQL_INTEGRATION_TESTS='1'
python -m pytest .\backend\tests -q
npm --prefix .\frontend test
npm --prefix .\frontend run build
```

浏览器验收可运行 `python backend/tests/run_distillation_browser.py --port 8012 --frontend-port 5175 --scripted-distillation-llm`，另开终端设置 `VITE_API_PROXY_TARGET=http://127.0.0.1:8012` 后启动 `npm --prefix frontend run dev -- --host 127.0.0.1 --port 5175 --strictPort`。脚本自行创建、迁移和清理隔离数据库，打印仅用于该次验收的合成登录账号；输入 `stop` 后回车优雅关闭并清理。真实认证、会话与 Origin 校验保留；该选项使用测试脚本模型驱动实际蒸馏 worker，其他后台 worker 和外部服务禁用，因此此验收不代表真实 LLM、邮件或上传 worker 已验证。

## 工作区协作与系统账户

- `/members` 管理当前工作区成员和邀请。所有者可授予所有者、管理员、操作员、查看者；管理员可邀请和管理操作员、查看者。不能修改自己的成员身份，也不能移除最后一位有效所有者。
- `/invitations` 是个人邀请收件箱。邮件链接来自 `PUBLIC_APP_URL`，收件人使用同一邮箱注册、验证并登录后，显式同意或拒绝；同意后通过顶部菜单切换工作区。初始工作区与既有数据保持原归属。
- `/accounts` 仅系统超级管理员可访问，提供账户搜索、状态筛选、分页、最近登录、禁用/恢复、系统提权/降权及变更记录。系统角色不授予工作区权限；系统禁用适用于个人工作区所有者，保留其成员关系和数据，但所有执行入口拒绝已禁用账户。
- 邀请 24 小时有效，邮件通过持久队列投递；超时或进程崩溃导致的结果未知不会自动重发，可在邀请记录中查看并显式重发。发送限流为每个工作区管理员每小时 30 次，同一邀请重发间隔至少 60 秒。页面只有服务端确认后才更新结果。

部署步骤：

1. 在受控部署环境设置 `PUBLIC_APP_URL` 为实际前端 HTTPS origin，同时设置 `AUTH_COOKIE_SECURE=true`，配置现有 `MAIL_*` SMTP 参数。本地开发可设置 `PUBLIC_APP_URL=http://127.0.0.1:5173`。
2. 明确选择首位系统管理员的邮箱，设置 `BOOTSTRAP_SUPERADMIN_EMAIL`。该账户须完成邮箱验证；启动、验证完成或登录时执行持久一次性 bootstrap，随后应移除该配置。后续提权降权通过账户页完成，不按注册顺序猜测管理员。
3. 用迁移角色执行 `python -m alembic -c backend/alembic.ini -x use_admin=1 upgrade head`，再发布后端和前端。本次升级撤销旧版浏览器会话和验证码，用户须重新登录，未完成验证者可重新发送验证码。
4. 运行 `python backend/scripts/verify_postgresql_runtime.py` 检查实际部署的 Schema、权限及依赖。此命令只读，不创建业务 fixture。

专项验证会自行创建并清理隔离数据库，运行身份没有 DDL 权限：

```powershell
$env:PYTHONPATH=(Resolve-Path .\backend).Path
$env:RUN_POSTGRESQL_INTEGRATION_TESTS='1'
python -m pytest .\backend\tests\test_workspace_access_postgresql.py -q
# 启动独立浏览器验收后端；结束时在另一终端运行同一命令加 stop 清理测试库
python -m tests.access_browser_app
```

在另一 PowerShell 终端启动验收前端：

```powershell
$env:VITE_API_PROXY_TARGET='http://127.0.0.1:18080'
npm --prefix .\frontend run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

测试邮件被模拟，不发送真实邮件。验收账户 `owner@access.example.test` 的密码为仅用于该隔离测试库的 `Synthetic-browser-only-2026!`。实现账本和实际验证范围见 [工作区与账户验收记录](./docs/workspace-account-access-implementation.md)。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue 3 + TypeScript + Vite + Pinia + Vue Router + Element Plus + axios + marked |
| 后端 | Python 3.12 + FastAPI + SQLAlchemy 2.0 + Pydantic v2 + OpenAI SDK + mcp SDK + httpx |
| 存储 | PostgreSQL（控制面与 Catalog）+ MinIO（文件与业务数据版本）+ Redis（可失效缓存）+ DuckDB（无状态查询） |

## 目录结构

```
project-root
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI 入口
│   │   ├── config.py          # 配置（读取 .env）
│   │   ├── database.py        # SQLAlchemy 引擎/会话
│   │   ├── models.py          # ORM 模型
│   │   ├── schemas.py         # Pydantic 模型
│   │   ├── ...                # 平台运行时代码，不包含具体行业种子
│   │   ├── routers/           # scenarios / data_sources / llm_configs / skills / mcp / agents
│   │   └── services/          # datasource / doc_parser / llm / rag / skill / mcp / agent_engine
│   ├── skills/
│   │   ├── ocr-parser/        # OCR 文档解析技能（已内置）
│   │   └── data-analyzer/     # 数据分析技能
│   ├── tests/                 # 平台策略与核心行为回归测试
│   ├── .env.example           # 无密钥配置模板；复制为 .env 后按需填写
│   └── requirements.txt
└── frontend/
    ├── src/
    │   ├── api/               # HTTP 客户端 + 可恢复的持久 Turn SSE
    │   ├── router/            # 路由
    │   ├── stores/            # Pinia
    │   ├── types/             # 领域类型
    │   ├── styles/            # 全局样式
    │   └── views/             # Scenarios / ScenarioDetail / DataSources / Agents / AgentChat / Tasks / Skills / MCP / LLMConfigs
    ├── vite.config.ts         # 端口 5173，/api 代理到 127.0.0.1:8000
    └── package.json
```

## 快速开始

以下命令都在仓库根目录执行。

### 1. 后端（Python 3.12）

```powershell
# 创建并激活虚拟环境（首次）
python --version  # 必须为项目支持的 Python 3.12.x
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装运行依赖与测试依赖（首次）
python -m pip install -r .\backend\requirements.txt
python -m pip install 'pytest>=8.3,<9'
# 显式安装高效 XLSX/XLSM -> Parquet 所需的官方 DuckDB 扩展；运行时不会联网安装
python .\backend\scripts\install_duckdb_extensions.py

# 仅在不存在时创建本地配置；不要覆盖已有密钥
if (-not (Test-Path -LiteralPath .\backend\.env)) {
    Copy-Item -LiteralPath .\backend\.env.example -Destination .\backend\.env
}

# 启动后端（默认使用 8000）
python -m uvicorn app.main:app --app-dir .\backend --host 127.0.0.1 --port 8000
```

> 后端 API 文档：http://127.0.0.1:8000/docs

后端启动前，PostgreSQL 必须已升级到仓库实际的 Alembic single head；必须通过 `alembic heads` 读取仓库事实，并用 `alembic current` 核对目标库。当前存储边界、首次建库、迁移和回退要求见
[PostgreSQL / MinIO 通用数据资产架构](./docs/PostgreSQL-MinIO-通用数据资产架构.md)。`init_db()` 只校验迁移版本，不会在生产库隐式建表；部署仍须以实际 `alembic heads` 和 `alembic current` 为准。

### 2. 前端（Node.js）

```powershell
# 按 lockfile 安装依赖（首次；ci 不可用时才使用 install）
node --version
npm --version
npm --prefix .\frontend ci
# 若 npm 11 拦截了 postinstall 脚本：
npm --prefix .\frontend approve-scripts esbuild vue-demi

# 启动开发服务器（端口 5173），显式保持 /api 与后端端口一致
$env:VITE_API_PROXY_TARGET='http://127.0.0.1:8000'
npm --prefix .\frontend run dev
```

> 打开浏览器访问：http://127.0.0.1:5173

## 配置说明

### OCR 服务（把 `backend/.env.example` 复制为 `backend/.env`）

`ocr-parser` 技能与扫描 PDF/图片解析可使用外部 OCR 服务：

```ini
OCR_BASE_URL=https://ocr.rhzy.ai
OCR_API_KEY=你的密钥
```

- PDF 总是先读取原生文本层；没有文本层的扫描 PDF 才进入 OCR。
- 未配置 `OCR_API_KEY` 时：有文本层的 PDF 正常解析，扫描 PDF 与图片明确报错。
- OCR 是服务端受控适配器，不是正式输入类型；正式调用仍只传不可变 `asset_version_id`。

### LLM 配置（前端「能力配置 → 大模型」）

种子数据默认创建了一个 `gpt-4o-mini` 配置（占位 API Key）。
请在 **大模型** 页面编辑或新建，填入真实的 `Base URL` / `API Key` / `模型`，
并勾选「设为默认」。支持任意 OpenAI 兼容服务（DeepSeek、通义、Ollama、vLLM 等）。

### 常见 MCP 客户端配置导入（前端「能力配置 → MCP 服务」）

页面支持逐项填写 stdio、SSE、Streamable HTTP，也可以粘贴常见客户端使用的 `mcpServers` JSON：

```json
{
  "mcpServers": {
    "firecrawl": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": {
        "Authorization": "Bearer <your-token>"
      }
    }
  }
}
```

`type: "http"` 会规范化为 Streamable HTTP。导入时先做无副作用预检并隐藏请求头、环境变量的值，
确认服务数量、传输类型以及重名处理策略（报错、跳过或替换）后再原子写入；保存不会自动连接外部服务，
请再使用卡片上的「测试连接」。编辑已有密钥时留空表示保留，删除整行才表示移除。

远程 MCP 允许可连通的 IP 和域名，包括本机、私网、链路本地及保留地址；仍拒绝 URL 凭据、明显的凭据查询参数和自动重定向。
默认只允许 HTTPS，连接时会固定到一次 DNS 解析结果，同时保留原始 Host 与 TLS SNI；需要 HTTP 时由部署配置显式开启。
stdio 会在 API 宿主机启动进程，因此默认关闭；仅可信的单租户、低权限沙箱部署可在 `backend/.env` 中显式设置：

```ini
ALLOW_MCP_STDIO=false
ALLOW_INSECURE_MCP_HTTP=false
DISTILLATION_BROWSER_ENABLED=false
DISTILLATION_BROWSER_MAX_SESSIONS=2
# 业务蒸馏调查使用的 AI 模型调用超时，不是“蒸馏模型”配置。
DISTILLATION_MODEL_TIMEOUT_SECONDS=120
MCP_OPERATION_TIMEOUT_SECONDS=90
```

### 将能力发布给 MCP 客户端

升级后的发布本体是场景能力 Release，Agent 只保留为可选编排和验证配置。通用 MCP 与 REST v2 使用带 `capabilities:read`、`capabilities:invoke` scope 的 `ont_sk_...` 外部凭据；原始 token 只在创建时展示一次。第三方使用固定的 Streamable HTTP 地址：

```json
{
  "mcpServers": {
    "business-capabilities": {
      "type": "http",
      "url": "https://api.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ont_sk_xxx"
      }
    }
  }
}
```

网关通用入口暴露 `list_capabilities`、`invoke_capability` 和 `get_capability_receipt`，三者与 REST v2 复用同一个 `CapabilityInvoker`、权限和回执。旧 `agt_sk_...` 和 `invoke_agent` 继续兼容 message-only 客户端，但不再是新集成的主路径。生产部署应显式配置对外地址与反向代理 Host：

```env
AGENT_MCP_PUBLIC_URL=https://api.example.com/mcp
AGENT_MCP_ALLOWED_HOSTS=api.example.com
```

请求头和环境变量在 API 中按只写值处理。异步 Workflow 输入已使用部署外部 key ring 的 AES-256-GCM 信封保护；连接器和第三方凭据仍应由生产 Secret Manager 托管，并限制数据库、磁盘和备份访问。

### 邮箱认证（`backend/.env`）

平台支持邮箱注册、邮箱验证码验证、登录、退出登录和密码重置。邮件服务只从后端环境变量读取，
不会写入平台代码。仓库只提供不含密钥的 `backend/.env.example`；注册和密码重置需要部署者在本地 `backend/.env` 中配置邮件服务。

```ini
MAIL_USERNAME=你的邮箱账号
MAIL_PASSWORD=你的邮箱授权码
MAIL_FROM=发件邮箱
MAIL_PORT=994
MAIL_SERVER=smtp.example.com
MAIL_STARTTLS=false
MAIL_SSL_TLS=true
MAIL_USE_CREDENTIALS=true
MAIL_TIMEOUT_SECONDS=20
```

认证使用 HttpOnly 会话 Cookie。每个注册用户默认创建独立工作区，只能访问本租户资源；标记为公共的场景、
数据源、LLM、MCP 和技能可被登录用户读取/使用，但公共资源不允许被其他租户修改。首次注册时，旧版本未带租户
信息的私有演示数据会认领到首个用户的工作区。

## 能力建设与验证流程

平台的目标主链路为：

1. **定义业务语义与交互**：建立对象、属性、关系、能力、规则、事件、工作流和输入输出端口。数据不是所有场景的必选前置条件。
2. **登记证据与资源用途**：Catalog 资产和数据集用权威 `usage_plane` 区分 `modeling_material`、`invocation_input` 与 `generated_output`；端口再声明建模证据、测试夹具、调用输入、参考知识、规则或输出角色。Excel/Word/数据库样本只有进入 `modeling_material` 才能贡献建模元数据，验证中心的上传、解析结果和运行数据包不能反向成为建模来源。
3. **建立可移植映射与绑定要求**：映射连接到逻辑 Dataset Schema 或场景内的 binding key，不把客户数据库 ID、表名或凭据写入能力定义。
4. **验证并发布能力版本**：确定性校验通过的定义进入治理快照；副作用、凭据和生产发布继续执行风险门禁。
5. **按调用提供当前业务输入**：验证 Agent 或第三方客户端可以提交文本、文档、结构化参数和受管数据版本；更换数据批次不要求重建能力。

## 统一能力工具（Agent 可调用）

| 工具 | 说明 |
| --- | --- |
| `list_available_capabilities` | 列出当前部署中已授权的 Function、Rule、Action、Workflow 及其输入契约和 readiness |
| `invoke_capability` | 通过统一 `CapabilityInvoker` 调用一个已列出的能力；数据输入、Receipt、预演、确认和幂等语义与 REST/MCP/SDK 一致 |

Agent 不再暴露 `run_function`、`run_sql` 或 Provider 自定义工具名等第二套执行入口。数据查询、文档检索、规则判定和其他行业能力都必须作为受治理能力发布，再由上述两个通用工具发现和调用。

## 平台边界与安全策略

- `backend/app` 的能力内核只实现通用平台契约；零售、医疗、财务等具体逻辑必须位于独立 Provider 包或由用户在定义中配置，通过受信注册表接入，不能在 Agent、REST、MCP 或调用内核中按场景名分支。
- Provider 必须按精确 `(provider_key, provider_version)` 静态注册；场景编辑器只消费服务端发布的受限 manifest，保存和发布时仍由对应 Provider 确定性校验配置，缺少精确版本不会回退到最新版。
- `DataAsset` 与 `LogicalDataset` 的 `usage_plane` 是服务端和数据库共同约束的用途事实；建模资料不会因文件名、标签、前端选择或 LLM 判断被隐式提升为正式运行输入。
- 输入内容契约是可选的；定义后按解析出的表头、字段别名、逻辑类型和最低行数匹配，不依赖文件名。未命中任何端口的额外文件可随本次 Turn 保留；必填端口必须命中，绑定到端口的输入必须通过该端口已定义的契约。
- 数据连接器显式健康检查会持久化有界、无凭据的结构轮廓及 SHA-256 fingerprint；声明内容契约的 connector 端口只消费这份受检轮廓，不让 LLM 读取整库或信任客户端结构描述。
- Agent 只向 LLM 展示 typed input 的有界、无值结构清单和内容哈希；完整 typed input 由统一调用内核直接传给最终选中的 Provider。批量业务值必须通过受管附件、数据集或连接器进入工具链，不能作为 prompt 正文绕过数据边界。
- 数据源、Agent、本体扩展和工作流引用都会校验资源是否存在以及是否属于当前业务场景；Catalog、语义映射和推导证据还通过数据库复合外键约束租户与数据集作用域。
- Agent 与工作流中的 SQL 仅允许单条只读查询，并受最大返回行数限制；脚本节点默认关闭，只有受控部署显式开启后才可执行。
- LLM API Key、数据源密码等凭据不会通过 API 回显；编辑时留空表示保留原凭据。
- 工作流 DAG 保存/执行前会校验开始结束节点、可达性、环路和规则分支完整性。

## 回归验证

```powershell
$env:PYTHONPATH=(Resolve-Path .\backend).Path
python -m pytest .\backend\tests -q
npm --prefix .\frontend test
npm --prefix .\frontend run build
```

`backend/scripts/verify_postgresql_runtime.py` 是无业务 fixture 假设的只读部署检查；它验证当前 Schema、运行角色权限、MinIO 和可选 Redis 健康，不创建、修改或删除业务数据。

## 常见问题

- **需要改后端端口**：启动前端前同步设置 `VITE_API_PROXY_TARGET`，不要依赖本机 `vite.config.ts` 中可能不同的默认代理端口。
- **npm 11 拦截 postinstall**：执行 `npm --prefix .\frontend approve-scripts esbuild vue-demi`。
- **LLM 调用失败**：检查 LLM 配置的 API Key 是否真实有效，可在 LLM 配置页点「测试」。
- **能力调用报 `provider_execution_failed`**：按调用编号查看后端 `Capability Provider failed` 日志，其中只记录异常类型和代码位置。若异常链在 `dataset_query_service` 的目录初始化处出现 `PermissionError`，检查实际服务账户的目录访问权限。在 `DATASET_CACHE_DIRECTORY` 中填写新的绝对路径，由后端正常查询流程创建自己的缓存、锁和租约目录；`DATASET_DUCKDB_TEMP_DIRECTORY` 留空时溢写文件也使用该缓存目录。不要共用其他开发或测试账户创建的目录。更改本机环境配置后重启后端，再从验证中心验证实际调用。
- **验证对话只返回大结果摘要**：超过模型上下文预算的回执仍完整保留在服务端。模型摘要仅保留匹配当前能力定义的输出契约中声明的数值、布尔值和枚举状态，并限制字段数与字节数；逐条明细通过授权回执接口查询。历史回放会重新核验服务端回执，不能用修改聊天记录的方式更改统计结果。
