# 业务场景本体智能平台（Ontology Business Agent Platform）

一个受 **Palantir Ontology** 启发的通用业务智能平台。核心理念：**不绑定任何特定行业**——
你可以为任意业务场景构建「本体（Ontology）」，再基于本体创建 **Agent**，
让大模型通过 **SQL 查询、文档检索（RAG）、技能（Skill）、MCP 工具** 自主完成该场景下的任意业务需求。

```
业务场景（本体：实体/属性/关系）
        │
        ▼
   Agent（绑定 场景 + LLM + 技能 + MCP + 数据源 + 系统提示词）
        │
        ▼
   AI 对话（ReAct 工具调用循环，SSE 流式输出）
```

## 功能特性

- **业务场景 / 本体建模**：可视化拖拽画布定义实体、属性、关系，支持抽象实体，任意行业通用。
- **数据源**：
  - 关系型数据库：MySQL / PostgreSQL / SQLite（表浏览 + SQL 查询）。
  - 文件桶（file bucket）：上传 Excel / Word / Markdown / PDF / 图片，自动解析入库用于 RAG 检索。
- **技能（Skill）**：以子进程方式安装并调用本地脚本/服务，内置 `ocr-parser`（OCR 文档解析）与 `data-analyzer`。
- **MCP 服务**：接入 Model Context Protocol 工具服务（stdio / SSE / streamable_http），自动发现工具并暴露给 Agent。
- **LLM 配置**：OpenAI 兼容协议（OpenAI / DeepSeek / 通义 / Ollama / vLLM…），多配置、可设默认、可测试连通性。
- **Agent 管理**：绑定场景、LLM、技能、MCP、数据源与系统提示词，一键创建智能体。
- **AI 对话**：可配置轮数的 ReAct 工具调用循环（默认最多 20 轮），SSE 流式输出，工具调用卡片实时展示参数与结果，Markdown 渲染。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue 3 + TypeScript + Vite + Pinia + Vue Router + Element Plus + axios + marked |
| 后端 | Python 3.12 + FastAPI + SQLAlchemy 2.0 + Pydantic v2 + OpenAI SDK + mcp SDK + httpx |
| 存储 | SQLite（平台元数据）+ 各业务数据源（MySQL/PostgreSQL/SQLite/文件桶） |

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
│   ├── examples/              # 可选演示场景与样例文档（不参与平台运行时）
│   │   ├── seed_retail.py
│   │   ├── seed_bookkeeping.py
│   │   └── bookkeeping_docs/
│   ├── skills/
│   │   ├── ocr-parser/        # OCR 文档解析技能（已内置）
│   │   └── data-analyzer/     # 数据分析技能
│   ├── tests/                 # 平台策略与核心行为回归测试
│   ├── data/                  # platform.db + buckets/（文件桶存储）
│   ├── .env                   # OCR / 调试配置
│   └── requirements.txt
└── frontend/
    ├── src/
    │   ├── api/               # axios 实例 + streamChat（SSE）
    │   ├── router/            # 路由
    │   ├── stores/            # Pinia
    │   ├── types/             # 领域类型
    │   ├── styles/            # 全局样式
    │   └── views/             # Dashboard / Scenarios / ScenarioDetail / DataSources / Agents / AgentChat / Skills / MCP / LLMConfigs
    ├── vite.config.ts         # 端口 5173，/api 代理到 127.0.0.1:8001
    └── package.json
```

## 快速开始

### 1. 后端（Python 3.12，conda 环境 `ontology_platform_env`）

```powershell
# 激活环境（已创建）
conda activate ontology_platform_env

# 安装依赖（首次）
pip install -r <project-root>\backend\requirements.txt

# 生成演示数据（可选；演示种子不属于平台运行时代码）
$env:PYTHONPATH="<project-root>\backend"
python <project-root>\backend\examples\seed_retail.py
python <project-root>\backend\examples\seed_bookkeeping.py

# 启动后端（端口 8001，8000 被占用）
$env:PYTHONPATH="<project-root>\backend"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

> 后端 API 文档：http://127.0.0.1:8001/docs

### 2. 前端（Node.js）

```powershell
# 安装依赖（首次）
npm --prefix <project-root>\frontend install
# 若 npm 11 拦截了 postinstall 脚本：
npm --prefix <project-root>\frontend approve-scripts esbuild vue-demi

# 启动开发服务器（端口 5173，/api 自动代理到 8001）
npm --prefix <project-root>\frontend run dev
```

> 打开浏览器访问：http://127.0.0.1:5173

## 配置说明

### OCR 服务（`backend/.env`）

`ocr-parser` 技能与 PDF/图片解析依赖外部 OCR 服务：

```ini
OCR_BASE_URL=https://ocr.rhzy.ai
OCR_API_KEY=你的密钥
```

- 未配置 `OCR_API_KEY` 时：PDF 回退到 `pypdf` 提取文本，图片解析会报错。
- 配置后：PDF / 图片均可走 OCR 服务获得更高质量文本。

### LLM 配置（前端「LLM 配置」页）

种子数据默认创建了一个 `gpt-4o-mini` 配置（占位 API Key）。
请在 **LLM 配置** 页面编辑或新建，填入真实的 `Base URL` / `API Key` / `模型`，
并勾选「设为默认」。支持任意 OpenAI 兼容服务（DeepSeek、通义、Ollama、vLLM 等）。

### 邮箱认证（`backend/.env`）

平台支持邮箱注册、邮箱验证码验证、登录、退出登录和密码重置。邮件服务只从后端环境变量读取，
不会写入平台代码；`backend/.env` 已按当前 SMTP 服务配置完成，部署到其他环境时请替换为对应环境变量。

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

## 使用流程（5 步）

1. **业务场景**：创建场景，在本体画布中定义实体、属性、关系。
2. **数据源**：接入数据库（MySQL/PostgreSQL/SQLite）或上传业务文档到文件桶。
3. **技能 / MCP / LLM**：安装技能、接入 MCP 工具服务、配置大模型。
4. **Agent 管理**：创建 Agent，绑定上述场景、数据源、技能、MCP、LLM 与系统提示词。
5. **AI 对话**：进入对话页，用自然语言提问，Agent 自主调用工具完成任务。

## 内置工具（Agent 可调用）

| 工具 | 说明 |
| --- | --- |
| `list_data_sources` | 列出 Agent 绑定的数据源 |
| `list_tables` | 列出某数据库数据源的表结构 |
| `run_sql` | 在指定数据源执行 SQL 查询 |
| `search_documents` | 在文件桶中做 RAG 语义/关键词检索 |
| `read_document` | 读取指定文档全文 |
| `execute_skill` | 以子进程执行已安装技能 |
| `mcp_{name}_{tool}` | 调用已接入 MCP 服务的工具 |

## 平台边界与安全策略

- `backend/app` 只实现通用平台能力；零售、医疗、财务等具体业务数据和提示词只放在 `backend/examples` 或由用户在界面中配置。
- 数据源、Agent、本体扩展和工作流引用都会校验资源是否存在以及是否属于当前业务场景，避免跨场景串用。
- Agent 与工作流中的 SQL 仅允许单条只读查询，并受最大返回行数限制；脚本节点默认关闭，只有受控部署显式开启后才可执行。
- LLM API Key、数据源密码等凭据不会通过 API 回显；编辑时留空表示保留原凭据。
- 工作流 DAG 保存/执行前会校验开始结束节点、可达性、环路和规则分支完整性。

## 回归验证

```powershell
$env:PYTHONPATH="<project-root>\backend"
python -m unittest discover -s <project-root>\backend\tests -v
npm --prefix <project-root>\frontend run build
```

## 常见问题

- **端口 8000 被占用**：本项目后端固定使用 **8001**，前端代理已指向 8001。
- **npm 11 拦截 postinstall**：执行 `npm approve-scripts esbuild vue-demi`。
- **PowerShell 终端 cwd 重置**：使用 `npm --prefix <project-root>\frontend ...` 与 `$env:PYTHONPATH="<project-root>\backend"` 前缀，避免 `Set-Location` 被剥离。
- **LLM 调用失败**：检查 LLM 配置的 API Key 是否真实有效，可在 LLM 配置页点「测试」。
