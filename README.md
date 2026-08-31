# 业务场景本体智能平台（Ontology Business Agent Platform）

一个受 **Palantir Ontology** 启发的通用业务能力平台。核心理念是：**不绑定任何特定行业，也不把某一批数据或平台内 Agent 当成业务场景本身**。
平台把业务语义、输入输出契约、规则、操作和工作流建设成可版本化、可治理、可由任意 Agent 或客户端调用的能力；平台内 Agent 是验证这些能力的参考客户端。

当前架构升级以 [平台能力化优化升级任务计划](./docs/优化升级任务计划文档.md) 为唯一执行计划；稳定边界、Provider 规范和第三方接入方式见 [能力平台架构与接入指南](./docs/能力平台架构与接入指南.md)。旧的固定数据型 Agent 流程仅作为兼容模式保留。

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
  - 文件桶（file bucket）：上传 Excel / Word / Markdown / PDF / 图片，自动解析入库用于 RAG 检索。
- **技能（Skill）**：安装受控的本地能力，供已配置的操作或工作流调用；内置 `ocr-parser`（OCR 文档解析）与 `data-analyzer`。
- **MCP 服务**：接入 Model Context Protocol 工具服务（SSE / Streamable HTTP，以及由运维显式开启的 stdio）；支持表单配置请求头，也支持批量导入常见客户端的 `mcpServers` JSON。
- **LLM 配置**：OpenAI 兼容协议（OpenAI / DeepSeek / 通义 / Ollama / vLLM…），多配置、可设默认、可测试连通性。
- **业务能力**：用结构化表单配置无副作用函数、可预演操作、规则、事件和可视化工作流，无需手写 JSON。
- **验证 Agent**：平台内用于验证场景能力、模型、运行输入和证据链的参考客户端。是否可验证按能力契约动态判断；没有数据端口的能力不要求数据源或映射。
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
    ├── vite.config.ts         # 端口 5173，/api 代理到 127.0.0.1:8000
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

# 启动后端（默认使用 8000）
python -m uvicorn app.main:app --app-dir .\backend --host 127.0.0.1 --port 8000
```

> 后端 API 文档：http://127.0.0.1:8000/docs

后端启动前，PostgreSQL 必须已升级到 Alembic head。当前存储边界、首次建库、迁移和回退要求见
[PostgreSQL / MinIO 通用数据资产架构](./docs/PostgreSQL-MinIO-通用数据资产架构.md)。`init_db()` 只校验迁移版本，不会在生产库隐式建表。

### 2. 前端（Node.js）

```powershell
# 安装依赖（首次）
npm --prefix .\frontend install
# 若 npm 11 拦截了 postinstall 脚本：
npm --prefix .\frontend approve-scripts esbuild vue-demi

# 启动开发服务器（端口 5173，/api 自动代理到 8000）
npm --prefix .\frontend run dev
```

> 打开浏览器访问：http://127.0.0.1:5173

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
2. **登记证据与资源用途**：需要数据时，明确区分建模证据、测试夹具、调用输入、参考知识、规则和输出。Excel/Word/数据库样本用于理解结构，并不自动成为永久运行数据。
3. **建立可移植映射与绑定要求**：映射连接到逻辑 Dataset Schema 或环境 binding key，不把客户数据库 ID、表名或凭据写入能力定义。
4. **验证并发布能力版本**：确定性校验通过的定义进入治理快照；副作用、凭据和生产发布继续执行风险门禁。
5. **按调用提供当前业务输入**：验证 Agent 或第三方客户端可以提交文本、文档、结构化参数和受管数据版本；更换数据批次不要求重建能力。

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

- `backend/app` 的能力内核只实现通用平台契约；零售、医疗、财务等具体逻辑必须位于独立 Provider 包或由用户在定义中配置，通过受信注册表接入，不能在 Agent、REST、MCP 或调用内核中按场景名分支。
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

- **需要改后端端口**：同步设置前端环境变量 `VITE_API_PROXY_TARGET`，默认代理目标是 `http://127.0.0.1:8000`。
- **npm 11 拦截 postinstall**：执行 `npm approve-scripts esbuild vue-demi`。
- **LLM 调用失败**：检查 LLM 配置的 API Key 是否真实有效，可在 LLM 配置页点「测试」。
