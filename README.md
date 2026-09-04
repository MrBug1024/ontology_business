# 业务场景本体智能平台（Ontology Business Agent Platform）

一个受 **Palantir Ontology** 启发的通用业务智能平台。核心理念：**不绑定任何特定行业**——
你可以为任意业务场景构建「本体（Ontology）」，再基于本体创建 **Agent**，
让大模型通过 **对象与数据查询、文档检索、确定性函数、规则、受控操作和工作流** 完成该场景下的业务需求。

```text
业务场景
  → 本体骨架（对象类型 / 属性 / 关系类型）
  → 数据源接入与测试
  → 数据映射（形成对象实例与关系实例）
  → 函数 / 操作 / 规则 / 事件 / 工作流
  → Agent（场景 + 模型 + 已映射数据）
  → AI 对话（查询、计算、规则判断、操作预演与工作流协作）
```

## 功能特性

- **业务场景 / 本体建模**：使用行业通用术语定义对象类型、属性、关系类型、对象实例与关系实例，支持命名空间、主键、枚举、约束、生命周期和来源信息。
  - **数据源**：
  - 版本化数据集：PostgreSQL Catalog 管理元数据，MinIO 保存不可变文件与 Parquet，DuckDB 执行只读查询。
  - PostgreSQL 连接器：表浏览与受控只读 SQL；PostgreSQL 也是平台控制面唯一关系型存储。
  - 文件桶（file bucket）：上传 Excel / Word / Markdown / PDF / 图片，自动解析入库用于 RAG 检索。
- **技能（Skill）**：安装受控的本地能力，供已配置的操作或工作流调用；内置 `ocr-parser`（OCR 文档解析）与 `data-analyzer`。
- **MCP 服务**：接入 Model Context Protocol 工具服务（SSE / Streamable HTTP，以及由运维显式开启的 stdio）；支持表单配置请求头，也支持批量导入常见客户端的 `mcpServers` JSON。
- **LLM 配置**：OpenAI 兼容协议（OpenAI / DeepSeek / 通义 / Ollama / vLLM…），多配置、可设默认、可测试连通性。
- **业务能力**：用结构化表单配置无副作用函数、可预演操作、规则、事件和可视化工作流，无需手写 JSON。
- **Agent 管理**：绑定场景、LLM 与已映射数据；只有本体、数据源、映射、模型和 Agent 数据绑定均完成后才开放对话。
- **AI 对话**：场景内可选择完整建模、本体、映射、业务能力、工作流、只读解释或操作预演；完整建模会生成带来源证据、冲突检查和原子确认的跨资源变更清单。Agent 对话的 ReAct 工具循环可查询对象与数据、检索文档、调用确定性函数、评估规则、生成操作预演并协助提交工作流。
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
    │   ├── api/               # axios 实例 + streamChat（SSE）
    │   ├── router/            # 路由
    │   ├── stores/            # Pinia
    │   ├── types/             # 领域类型
    │   ├── styles/            # 全局样式
    │   └── views/             # Scenarios / ScenarioDetail / DataSources / Agents / AgentChat / Tasks / Skills / MCP / LLMConfigs
    ├── vite.config.ts         # 端口 5173，/api 代理由 VITE_API_PROXY_TARGET 配置
    └── package.json
```

## 快速开始

以下命令都在仓库根目录执行。

### 1. 后端（Python 3.12）

```powershell
# 创建并激活虚拟环境（首次）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装依赖（首次）
python -m pip install -r .\backend\requirements.txt

# 创建本地配置（首次；不要提交真实密钥）
Copy-Item .\backend\.env.example .\backend\.env

# 启动后端（默认使用 8001；命令从仓库根目录执行）
python -m uvicorn app.main:app --app-dir .\backend --host 127.0.0.1 --port 8001
```

> 后端 API 文档：http://127.0.0.1:8001/docs

后端启动前，PostgreSQL 必须已升级到 Alembic head。当前存储边界、首次建库、迁移和回退要求见
[PostgreSQL / MinIO 通用数据资产架构](./docs/PostgreSQL-MinIO-通用数据资产架构.md)。`init_db()` 只校验迁移版本，不会在生产库隐式建表。

```powershell
# 每次升级后，在启动 API 前执行（命令从仓库根目录执行）
python -m alembic -c .\backend\alembic.ini upgrade head
```

### 2. 前端（Node.js）

```powershell
# 安装依赖（首次）
npm --prefix .\frontend install
# 若 npm 11 拦截了 postinstall 脚本：
npm --prefix .\frontend approve-scripts esbuild vue-demi

# 启动开发服务器（端口 5173；默认 /api 代理到本机 8001）
npm --prefix .\frontend run dev
```

后端位于其他容器或主机时，在启动前设置
`VITE_API_PROXY_TARGET=http://后端服务名:8001`；生产前端不运行 Vite，而由 Nginx 将同源
`/api` 转发给 API 容器。

> 打开浏览器访问：http://127.0.0.1:5173

## 生产部署

生产环境使用 PostgreSQL 作为唯一关系型数据库、外部 MinIO 保存文件，并由 Coolify 直接将
前端域名路由到前端容器；不使用 SQLite、`/app/data/platform.db` 或 Nginx。前端容器使用
`vite preview` 提供已构建的静态 SPA，API 通过独立的 `ontology-api.rhzy.ai` 入口访问。完整的
环境变量、Alembic 迁移顺序、400 MiB 上传限制、API/worker 多进程职责及 Coolify GitHub App
认证要求见[生产部署手册](./docs/生产部署.md)。

## 配置说明

### OCR 服务（把 `backend/.env.example` 复制为 `backend/.env`）

`ocr-parser` 技能与 PDF/图片解析依赖外部 OCR 服务：

```ini
OCR_BASE_URL=https://ocr.rhzy.ai
OCR_API_KEY=你的密钥
```

- 未配置 `OCR_API_KEY` 时：PDF 回退到 `pypdf` 提取文本，图片解析会报错。
- 配置后：PDF / 图片均可走 OCR 服务获得更高质量文本。

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

### 将 Agent 发布为 MCP 服务

「能力配置 → MCP 服务 → Agent 发布」可以把一个已经完成就绪检查的 Agent 发布给第三方。
创建发布时平台自动生成仅绑定该发布的 `agt_sk_...` 不透明令牌，并且只在创建响应中展示一次；
后续如丢失配置，需要轮换令牌，旧配置会立即失效。第三方使用固定的 Streamable HTTP 地址：

```json
{
  "mcpServers": {
    "医保违规审计助手": {
      "type": "http",
      "url": "https://api.example.com/mcp",
      "headers": {
        "Authorization": "Bearer agt_sk_xxx"
      }
    }
  }
}
```

网关只暴露 `invoke_agent`，但工具内部复用平台 Agent 对话的完整运行上下文、能力白名单、
数据权限、LLM 工具循环、引用和审计记录。生产部署应显式配置对外地址与反向代理 Host：

```env
AGENT_MCP_PUBLIC_URL=https://api.example.com/mcp
AGENT_MCP_ALLOWED_HOSTS=api.example.com
```

`invoke_agent.message` 是终端用户消息的透传边界：MCP 宿主应把一条用户消息完整、原样地调用一次，
不要先改写为检索词或拆成多次工具调用。首次返回会包含 `conversation_id`；同一终端用户会话的后续
调用（包括“确认执行”等回复）必须回传该值，即使 MCP transport 已重新连接。兼容仍使用
`Mcp-Session-Id` 的客户端时，客户端也应在同一 transport session 的后续请求中原样续传该响应头。
公开响应只携带最终答案、续接标识、引用标识和工具执行摘要；完整工具参数与结果保留在平台会话及
审计记录中，避免大型审计明细在 MCP 的 `content` / `structuredContent` 中重复传输。

请求头和环境变量在 API 中按只写值处理，但当前仓库不内置数据库静态加密或外部 Secret Manager。
生产部署应启用数据库/磁盘加密并限制备份访问；安全基线要求更高时，应在连接器密钥服务中统一实施信封加密和密钥轮换。

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

## 第一版使用流程

平台只有一套导航和一条主链路：

1. **创建业务场景并建立本体骨架**：先定义对象类型、属性、主键和关系类型；也可以上传业务文档，由 AI 生成可检查的变更清单，确认后才写入。
2. **接入并测试数据源**：版本化数据集先固定 Schema 和 MinIO 资产版本；外部数据库读取表结构，文件桶完成解析与索引。没有真实字段时不能创建数据映射。
3. **配置数据映射**：把源表/文件字段映射到对象属性，预览、测试并刷新为对象实例。顺序是“本体骨架 → 数据源 → 映射”，不是在数据源和映射之间二选一。
4. **配置业务能力**：按需定义函数、操作、规则、事件和工作流。函数用于无副作用计算；操作用于写入或外部调用并要求预演/确认。
5. **创建 Agent 并进入对话**：绑定场景、模型及映射所用数据源，让 Agent 基于本体语义和受治理能力完成业务需求。

## 内置工具（Agent 可调用）

| 工具 | 说明 |
| --- | --- |
| `list_data_sources` | 列出 Agent 绑定的数据源 |
| `list_tables` | 列出版本化数据集或外部数据库的逻辑关系与字段结构 |
| `run_sql` | 对固定数据集版本或外部连接器执行受控只读 SQL |
| `search_documents` | 在文件桶中做 RAG 语义/关键词检索 |
| `read_document` | 读取指定文档全文 |
| `list_functions` / `run_function` | 查看并调用无副作用的确定性业务函数 |
| `list_rules` / `evaluate_rule` | 查看并评估业务规则 |
| `list_actions` / `execute_action` | 查看操作并生成安全预演；真实副作用仍需用户确认 |
| `list_workflows` / `execute_workflow` | 查看工作流并返回显式提交指引 |

## 平台边界与安全策略

- `backend/app` 只实现通用平台能力；零售、医疗、财务等具体业务数据和提示词只放在 `backend/examples` 或由用户在界面中配置。
- 数据源、Agent、本体扩展和工作流引用都会校验资源是否存在以及是否属于当前业务场景；Catalog、语义映射和推导证据还通过数据库复合外键约束租户与数据集作用域。
- Agent 与工作流中的 SQL 仅允许单条只读查询，并受最大返回行数限制；脚本节点默认关闭，只有受控部署显式开启后才可执行。
- LLM API Key、数据源密码等凭据不会通过 API 回显；编辑时留空表示保留原凭据。
- 工作流 DAG 保存/执行前会校验开始结束节点、可达性、环路和规则分支完整性。

## 回归验证

```powershell
$env:PYTHONPATH=(Resolve-Path .\backend).Path
python -m pytest .\backend\tests -q
python .\backend\scripts\verify_postgresql_runtime.py
npm --prefix .\frontend run build
```

## 常见问题

- **需要改开发环境后端地址**：设置 `VITE_API_PROXY_TARGET`；它只影响 Vite 开发代理，生产静态前端始终通过 Nginx 的同源 `/api` 访问后端。
- **npm 11 拦截 postinstall**：执行 `npm approve-scripts esbuild vue-demi`。
- **LLM 调用失败**：检查 LLM 配置的 API Key 是否真实有效，可在 LLM 配置页点「测试」。
