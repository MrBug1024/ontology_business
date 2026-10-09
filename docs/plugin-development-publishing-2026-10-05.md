# 插件开发与发布分离

需求以本次用户要求为准；此前文档中的完成声明不作为验收依据。

| ID | 原文目标 | 非目标 | 可观察验收 | 影响 | 保持不变 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| P1 | 插件开发独立且排在发布之前 | 不重做 AI 编码器 | 主导航依次为验证中心、插件开发、发布中心；开发入口可选择场景、准备固定能力版本、进入或恢复编码 | App/router/开发首页/能力版本列表 | 原工作台深链接、草稿保护 | 前端测试、构建、浏览器 |
| P2 | 开发完成后在发布页选择插件 | 不自动上传外部仓库、不把生成草稿当发布 | 开发页人工审阅定版；发布页仅列不可变审阅版本，选择后下载插件或 Marketplace 材料；后续修改工作台不改变已定版内容 | coding review/artifact service、DTO/router/API/发布组件 | 租户、所有者、场景 ACL、CAS、版本冲突、统一 MCP 执行 | 后端行为测试、真实 PG 与浏览器 |
| P3 | 放弃 REST API 产品接入方式 | 不立即破坏已发布稳定协议 | 发布中心移除 REST 配置卡片及示例，插件成为首个发布入口，保留插件凭据与 MCP 设置 | 发布页面、导航测试 | 已有 REST v2 在兼容期继续运行；MCP 凭据边界不变 | 前端测试、现有协议回归 |

影响图：主导航 → 独立开发首页 → 固定能力版本/既有验证 → 编码工作台 → 人工审阅定版 → 不可变 AssistantMessage 工件快照 → 发布中心选择版本 → 下载准确快照的插件/Marketplace 材料。

能力版本用于固定验证和执行身份，外部插件交付是下一步。首页允许人工准备、启停能力版本，无需先访问发布中心。已有 `/plugin-studio/:releaseId` 与 `/access` 链接保留。既有工件快照自动进入版本选择列表；新审阅不触发下载或外部发布。

复用现有持久快照与版本唯一身份，不新增 ORM/DDL。发布下载不读取当前工作台文件，重新构建须校验受信适配器身份及精确 SHA-256，否则拒绝。无权、停用、退役、版本冲突均由服务端裁决。

REST 退出按 AGENTS.md 的公共协议稳定要求分两步：本次移除产品主流程；路由停用须另行确定弃用窗口与调用方迁移，不删除统一内核或凭据服务。

补充必要消费者：开发页的“验证能力”经 Agent 列表进入验证会话时，必须传递同场景的 `release_id`；跨场景或无效参数不传递。影响图扩展到 `ScenarioReleaseList -> Agents.openAgentChat -> agentValidationTarget -> AgentChat`，后端权限及版本解析保持不变。

发布 MCP 设置的实际页面显示同源 `/mcp`，但 Vite 原先仅代理 `/api`。为使该设置可用于本机插件接入，补充同目标 MCP 代理；普通 HTML 导航仍进入现有 `/mcp` 兼容页面，协议请求进入后端。无凭据协议请求须返回认证拒绝，不能返回 SPA HTML。影响图增加 `MCP 设置 -> Vite proxy -> 原后端 MCP`，部署端口、生产鉴权及内核不变。

## 验收结果

- 环境：Python 3.12.0（`D:/anaconda3/envs/ontology_platform_env/python.exe`）、Node 24.6.0、npm 11.5.1；Node 满足已安装 Vite 的 engines。没有升级依赖或改写 lockfile。
- 后端全量：341 passed。使用任务现有 pytest 8 依赖目录、`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`PYTHONDONTWRITEBYTECODE=1`、`-p no:cacheprovider` 与新建 `--basetemp` 运行。现存的 Starlette/Pydantic 警告仍在。
- 前端：`npm --prefix frontend test`，255 passed；`npm --prefix frontend run build` 通过。现存的 bundle 大小警告仍在。
- `python backend/scripts/verify_plugin_coding_postgresql.py`：脚本自行创建/迁移/删除隔离 PG 数据库，版本冲突、并发定版、旧工件不变、迟到轮次及 lease 拒绝均通过；模型与业务验收使用显式 synthetic fixture。[原始结果](plugin-publication-concurrency-2026-10-05.json)。
- `python backend/scripts/verify_plugin_publication_readonly.py --workspace-id <本次测试工作台> --output <结果文件>`：当前真实 PG 上只读通过。v1.0.0 安装包 9,963 字节、Marketplace 12,312 字节；安装包 SHA-256 与此前真实审阅工件完全一致。匿名、跨租户及错误身份拒绝。[原始结果](plugin-publication-postgresql-2026-10-05.json)。目录首次读取 3 次 SQL、约 2,269ms，是本次环境观测值。
- 已登录真实项目浏览器：主导航独立开发入口 → 恢复已有编码会话 → 人工审阅定版 → 深链接选中 v1.0.0 → Marketplace 材料生成成功；再从主导航进入发布中心手动选择同一插件版本。没有新建凭据或上传外部仓库。
- 从开发页选择 v2 的“验证能力”后，Agent 列表和验证会话都保留 v2；实际“本次验证使用”显示该版本。
- 页面刷新保留已选插件；切换无插件场景显示空状态并清除旧选择；浏览器前进/后退正常。加载期间禁止版本选择，防止选择遮罩下的旧行。
- 390×844 窄屏：文档宽度 390、主区域没有横向溢出；键盘 Tab 从市场包操作到“继续开发新版本”；临时 viewport 已恢复。
- MCP 代理真实 HTTP 检查：`Accept: application/json, text/event-stream` 无凭据请求返回 401 JSON；`Accept: text/html` 返回 200 HTML。发布界面仅呈现 MCP 连接，无 REST 卡片或 curl 示例。
- `git diff --check` 通过；保留既有用户改动，未编辑 `.env`、未新增 ORM/migration、未提交生成目录。

界面证据：[开发入口](plugin-development-2026-10-05.png)、[定版完成](plugin-review-2026-10-05.png)、[发布材料生成](plugin-publishing-2026-10-05.png)、[窄屏](plugin-publishing-narrow-2026-10-05.png)。

本次交付范围是开发、定版与发布材料选择流程；不声称工件已上传外部市场，不把此次浏览器/API 验收扩展为全部业务场景质量或所有第三方宿主兼容性证明。REST v2 正式停用与调用方迁移仍需单独制定弃用窗口。
