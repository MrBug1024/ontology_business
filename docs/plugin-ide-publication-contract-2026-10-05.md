# 插件 IDE 与发布安装：本次需求账本

以真实菜单点击、代码与可执行协议为证据；以前的完成记录不代替本次验收。保留工作区所有既有改动。

| ID | 用户目标 | 可观察验收 | 影响范围 | 保持不变 |
|---|---|---|---|---|
| IDE1 | 插件开发菜单进入代码开发页 | 从场景页点击菜单进入插件 IDE，空工作区也显示资源管理器、主编辑区、AI 区；刷新与返回可用 | App/路由、开发入口、导航回归 | 工作区与登录权限 |
| IDE2 | 像 VS Code 的 AI 代码编辑 | 文件树在左、代码在主区、AI 在右；打开多个源文件、行号、缩进、保存快捷键、差异/校验、生成/停止/修正；旧候选与未保存内容不丢 | 工作台组件、编辑器、现有 composable | 后端生成、CAS/hash、受信适配器、业务执行门禁 |
| PUB1 | 发布中心选场景插件进行发布 | 人工选择已审阅的确切版本并显式发布；服务端持久发布身份；未发布、停用或无权版本不作为安装源 | 发布服务/DTO/路由、前端发布组件 | 人工定版、不可变工件、租户/ACL、场景绑定 |
| PUB2 | 第三方复制安装命令即可安装 | 每个已发布插件提供 PowerShell 与 shell 的可复制安装命令；校验包 SHA，使用官方宿主安装协议；真实隔离宿主安装与 MCP 初始化 | 公共受限包下载、安装命令契约、发布页、安装验收 | 不附调用密钥，不把 localhost 宣称公网源，不把安装成功宣称业务已执行 |

影响图：菜单 -> 插件 IDE -> 现有场景契约 / AI 编码 / 候选文件 -> 人工审阅快照 -> 发布中心选择 -> 场景插件发布服务 -> 仅已发布工件的安装材料 -> 官方宿主安装 -> 既有统一能力执行。

第一支持宿主沿用 Claude Code。遵循官方 plugin manifest、Marketplace、Skill 与 MCP 契约；不同宿主的安装格式不混称通用标准。兼容现有 Claude Code 2.1.208，不能使用仅 2.1.224+ 支持的 archive source 来冒充本机实测通过。公开服务 origin 来自部署配置，未配置公网 HTTPS 时明确本机安装范围。

发布的是经审阅的插件代码与契约，不发布运行数据或调用凭据。调用仍由安装方获得场景专用凭据，保留平台的权限、确认、幂等和固定版本。所有新增公开读取只能访问显式发布、当前可交付的不可变包。

验证计划：旧行为失败的实际菜单/IDE回归；类型/API全部消费者；前后端测试与构建；发布权限、幂等、错误 hash、停用、匿名未发布拒绝及真实 PG；浏览器菜单/多文件/保存/窄屏/发布/复制；复制命令在新隔离配置执行安装。未运行项如实记录。

## 实现与验收

| ID | 本次实际结果 | 验证证据 |
|---|---|---|
| IDE1 | 从真实场景页点击“插件开发”进入独立三栏 IDE；未打开项目时也呈现资源管理器、代码工作区和 AI 区 | 实际 ElMenu / 生产 Router 点击回归；实际空 IDE SFC 测试；`plugin-ide-home-2026-10-05.png` |
| IDE2 | 打开真实 v1.1.0 项目，文件树、多个标签、Python 语法着色和行号；Tab 缩进、未保存切换保护、恢复候选、差异和只读适配器均有效；390px 面板可切换且无横向溢出 | 实际 SFC 键盘/禁用/差异回归；`plugin-ide-editor-2026-10-05.png` 与 narrow 图；浏览器编辑后恢复字节一致，不改已定版工件 |
| PUB1 | 实际选择场景 v1.1.0，人工确认发布；刷新恢复发布状态；撤回后两种匿名材料均 404；重新发布恢复同一 SHA 来源 | `plugin-install-source-postgresql-2026-10-05.json`、`plugin-install-source-withdrawal-2026-10-05.json`、`plugin-install-source-publication-2026-10-05.json` |
| PUB2 | 原样复制页面 PowerShell 命令，在新独立目录和 Claude 配置中完成下载 SHA 校验、专属 venv、清单验证、Marketplace 注册和实际宿主安装；已安装 MCP 初始化及五个工具发现成功 | `plugin-install-source-installation-2026-10-05.json`；`plugin-install-published-2026-10-05.png` 与 narrow 图 |

本次并未证实用户旧浏览器会话为何曾把有效菜单指向场景页。新浏览器实点与真实导航测试均进入插件路由；另外复现并修复了非法嵌套插件 URL 被全局 fallback 送到场景页的问题。没有把路由测试成功当成全部历史症状的根因证明。

代码编辑器为实际轻量源码编辑器：安全文本插值着色、行号、缩进、保存、差异和多个标签；未引入 Monaco，未宣称具备语言服务、补全、任意终端执行或调试。AI 继续消费完整固定能力契约并生成持久文件；主视图默认源码，AI 对话移到右侧。切换场景/版本保留目标输入，不再弹出错误的“离开丢失”提示；真正离开与刷新仍保护草稿，旧守卫注入的实际 SFC 测试明确失败。

发布列表在桌面滚动时保留所选版本，窄屏回到正常单列。发布是单独的人工状态操作，下载材料保留为次要自托管渠道，不再将“下载 ZIP”称作已经对外发布。

发布记录沿用已有 PostgreSQL 持久消息契约：确定性身份、工作区根记录行锁、expected revision、不可变工件 hash 与每修订审计。没有新表或 Schema，因此没有迁移。匿名下载不会假冒作者；未发布、撤回、停用与不匹配身份拒绝。公开字节和安装器在发布时固定快照，后续受信代码变化不悄悄替换安装来源。新隔离 PG 证据验证同修订并发只有一个发布和一个审计；匿名小包读取 8 查询、约 13.7ms，不能据此宣称高负载性能。

独立审查复现了安装重试会沿用被修改的本地副本。修复后每轮从本次下载且通过校验的来源重建副本，拒绝路径、链接和虚拟环境逃逸，并有条数、压缩/解压大小和超时限制。下载包不改；仅安装方本地副本把 MCP Python 命令指向该插件独享的虚拟环境。

### 实际命令

- `npm --prefix frontend test`：最终 **279 项通过**，日志 `.tmp-plugin-ide-frontend-test.log`。
- `npm --prefix frontend run build`：TypeScript 与 Vite 构建通过，日志 `.tmp-plugin-ide-build.log`；仍有已有的大 chunk 提示。
- Python 3.12 的 `pytest backend/tests -q -p no:cacheprovider --basetemp=.tmp-plugin-publication-backend-all-final`：**408 项通过**，日志 `.tmp-plugin-publication-backend-all-final.log`。
- `python backend/scripts/verify_plugin_publications_postgresql.py`：其自行创建和清理的隔离 PG 数据库验证通过；head 从实际 Alembic 读取，未使用固定文档 head。
- UI 原样复制的 `.tmp-plugin-publication-install.ps1`：执行退出 0；只设置 PATH、`SCENARIO_PLUGIN_INSTALL_ROOT`、`CLAUDE_CONFIG_DIR`、官方 PyPI 源及输出编码，未更改命令文本、HOME 或日常 Claude 配置。日志 `.tmp-plugin-install-source-command.log`。
- 已安装缓存内 `server.py` 用该插件的 venv Python 做 stdio initialize / list_tools：通过；无业务调用和凭据。
- Git Bash `-n .tmp-plugin-publication-install.sh`：语法检查通过；未执行 macOS/Linux 的真实安装。
- `git diff --check`：通过；保留所有既有用户改动，未提交生成物或凭据。

本机 Python 3.12.0、Node 24.6、npm 11.5；实装宿主 Claude Code 2.1.208。匿名读取和撤回前后的真实 v1.0.0/v1.1.0 包 SHA 均保持原值。真实安装使用 v1.1.0，不是合成安装演示。

### 安装范围与部署

本次服务进程显式设置 `PLUGIN_PUBLIC_BASE_URL=http://127.0.0.1:3099` 与 `PLUGIN_ALLOW_LOCAL_HTTP=1`，仅用于本机试装；未改 `.env`，未上传 Git 或第三方市场。远程安装需要部署者设置可访问的 HTTPS `PLUGIN_PUBLIC_BASE_URL`，保留 API 反向代理，并关闭本机 HTTP 开关。未配置时服务端拒绝发布安装来源，不猜测 Host。若地址改变，界面要求人工撤回并重新发布。

第一支持宿主是 Claude Code。安装后使用页面给出的 `/插件名:run-scenario`，另行配置 HTTPS 的 `SCENARIO_MCP_URL` 与该场景专属 `SCENARIO_API_KEY`。安装不会授予业务执行权限。本次验证了真实安装与工具握手；没有新签发凭据，未通过新安装实例执行业务，也没有完成公网 HTTPS 部署。先前业务验证回执不可充当本次第三方实例调用回执。

### 采用的官方规则

- [Claude Code Plugin 参考](https://code.claude.com/docs/en/plugins-reference)：`.claude-plugin/plugin.json`、Skill 与 MCP 配置结构、插件命名空间和路径。
- [Marketplace 参考](https://code.claude.com/docs/en/plugins/marketplace-reference)：市场索引及 source 类型。URL 市场不能解析相对插件 source；ZIP archive source 需要较新宿主，故本次安装器先验包再注册本地 Marketplace，实测兼容现有 2.1.208。
- [MCP 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)：宿主侧 stdio，适配器调用平台已有 MCP/统一执行，不复制业务规则。
