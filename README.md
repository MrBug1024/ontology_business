# 业务场景本体智能平台（Ontology Business Agent Platform）

一个受 **Palantir Ontology** 启发的通用业务能力平台。核心理念是：**不绑定任何特定行业，也不把某一批数据或平台内 Agent 当成业务场景本身**。
平台把业务语义、输入输出契约、规则、操作和工作流建设成可版本化、可治理、可由任意 Agent 或客户端调用的能力；平台内 Agent 是验证这些能力的参考客户端。

长期工程边界和完成定义以根级 [AGENTS.md](./AGENTS.md) 为准；能力契约、可选 Provider 扩展和第三方接入方式见 [能力平台架构与接入指南](./docs/能力平台架构与接入指南.md)。

```text
场景定义版本
  → 本体 + 能力 + 输入输出端口 + 策略 + 建模/评测证据
  → 发布到 dev / staging / prod
  → 按环境解析数据集、连接器和规则绑定
  → 每次调用提供文本、文档、结构化参数或新的业务数据
  → CapabilityInvoker 统一执行
  → 验证 Agent / REST / MCP / SDK 消费同一能力
```

## 功能特性

- **业务场景 / 本体建模**：使用行业通用术语定义对象类型、属性、关系类型、对象实例与关系实例，支持命名空间、主键、枚举、约束、生命周期和来源信息。
  - **数据源**：
  - 版本化数据集：PostgreSQL Catalog 管理元数据，MinIO 保存不可变文件与 Parquet，DuckDB 执行只读查询。
  - PostgreSQL 连接器：表浏览与受控只读 SQL；PostgreSQL 也是平台控制面唯一关系型存储。
  - 文件桶（file bucket）：上传 Excel / Word / Markdown / PDF / 图片，自动解析入库用于 RAG 检索；验证 Agent 的附件先登记持久上传任务、流式写入 MinIO，再由后台 worker 解析，页面请求不等待解析完成。
- **技能（Skill）**：安装受控的本地能力，供已配置的操作或工作流调用；内置 `ocr-parser`（OCR 文档解析）与 `data-analyzer`。
- **MCP 服务**：接入 Model Context Protocol 工具服务（SSE / Streamable HTTP，以及由运维显式开启的 stdio）；支持表单配置请求头，也支持批量导入常见客户端的 `mcpServers` JSON。
- **LLM 配置**：OpenAI 兼容协议（OpenAI / DeepSeek / 通义 / Ollama / vLLM…），多配置、可设默认、可测试连通性。
- **业务能力**：用结构化表单配置无副作用函数、可预演操作、规则、事件和可视化工作流，无需手写 JSON。
- **验证 Agent**：平台内用于验证场景能力、模型、运行输入和证据链的参考客户端。Agent Turn 可引用仍在上传/解析的持久任务，消息先落库并返回 `202 Accepted`，后台再准备输入和执行；页面可恢复 SSE 进度，并通过 revision 控制取消和重试冲突。是否可验证按能力契约动态判断；没有数据端口的能力不要求数据源或映射。
- **AI 对话**：场景内可选择完整建模、本体、映射、业务能力、工作流、只读解释或操作预演；完整建模会生成带来源证据、冲突检查和原子确认的跨资源变更清单。带附件的全局顾问消息会先连同占位回复和持久请求台账原子落库，再由后台等待附件并执行，刷新后可恢复、取消或显式重试；重试创建父子审计链，不改写旧终态。文档全文和表格原始行只作为服务端索引/查询来源，LLM 只接收有界检索片段、引用元数据和受限工具结果，不能把未检索部分表述为已阅读。
- **任务中心**：集中处理工作流状态、重试和人工审批；运行时内部保留权限、连接解析和定义快照等安全内核，但不作为独立业务菜单暴露。

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

远程 MCP 默认只允许公网 HTTPS，拒绝 URL 凭据、明显的凭据查询参数、本机/私网/链路本地地址和自动重定向；
连接时会固定到已校验的 DNS 解析结果，同时保留原始 Host 与 TLS SNI，避免连接阶段再次解析到未授权地址。
stdio 会在 API 宿主机启动进程，因此默认关闭；仅可信的单租户、低权限沙箱部署可在 `backend/.env` 中显式设置：

```ini
ALLOW_MCP_STDIO=false
ALLOW_INSECURE_MCP_HTTP=false
MCP_PRIVATE_HOST_ALLOWLIST=
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
3. **建立可移植映射与绑定要求**：映射连接到逻辑 Dataset Schema 或环境 binding key，不把客户数据库 ID、表名或凭据写入能力定义。
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
