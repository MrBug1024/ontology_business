# 插件编码工作台交互重做：需求账本与验收

本次需求来自用户对大弹窗、表单式编程体验的明确否定。文档只记录本次目标和证据，不以旧文档或通过的测试证明旧设计正确。

| ID | 原文目标 | 非目标 | 可观察验收 | 影响层/文件 | 保持不变项 | 测试证据 |
| --- | --- | --- | --- | --- | --- | --- |
| U1 | 按 AI 编程协作方式设计，改善实际界面 | 复制 Codex 私有实现、修改全平台主题 | 发布列表进入独立路由；刷新、返回、会话切换可恢复；不再在封装弹窗内编码 | router、release list、Studio view、context composable | 发布与权限由服务端裁决 | 前端测试、构建、浏览器 |
| U2 | 编码时持续观察并修正 | 伪造终端、模型思考或测试结果 | 真实保存的每轮需求与模型公开进度可恢复；固定输入区可以停止、修正、继续；错误保留草稿 | workspace 输出、独立 conversation service、DTO、typed UI | 同一后台任务/lease；不运行租户代码 | 后端契约、隔离 PG、浏览器 |
| U3 | 便于审阅实际代码变更 | 把前后全文当成差异 | 文件/逐行增删/验证独立浏览；可编辑文件保存校验；冲突保留草稿并明确重设基准 | editor、diff util、workbench composable | CAS、受保护适配器、版本冻结 | 差异与状态行为测试、浏览器 |
| U4 | 审阅后向第三方交付 | 未实现的自动远程发布 | 顶部交付入口打开聚焦审阅面板；版本、人工审阅、插件/市场包清晰；窄屏与键盘可用 | delivery component、responsive CSS | 后端精确发布、案例与版本门禁；无凭据入包 | 前端门禁、浏览器 |

影响图：发布列表 → 独立 Studio route/view、App 聚焦工作区外壳 → context/composer/inspector/delivery → pluginCoding API + 单条发布只读查询 → workspace application service → 已存在的 AssistantMessage/RequestRun 持久历史 → 原 worker/统一能力执行与工件导出。

单条发布查询用于刷新恢复精确上下文，沿用现有租户与场景读取权限；前端拒绝在当前发布界面展示其他发布的编码会话。App 聚焦外壳为工作台提供整页高度，将平台导航收进可打开的菜单，避免双重常驻侧栏。

仅新增有界、所属校验后的公开编码轮次投影，不返回原始 payload、thinking、附件、运行输入、模型配置密钥，不新增数据库字段或执行内核。旧记录的公开事件保留为活动日志，不猜测归属到某轮次。

设计参考：[OpenAI 官方对话与代码审阅流程](https://developers.openai.com/blog/mastering-codex-remote-for-engineering)。借鉴的是持续任务、公开执行反馈、文件审阅和修正循环；本项目的执行能力仍按现有可执行契约如实显示。UI/UX 技能建议的营销页面布局与紫色主题不适用于仓库现有桌面工作台，采用现有语义色及简洁分栏。

## 实现与验收

U1：发布列表进入 `/plugin-studio/:releaseId` 独立路由；最近五个会话、发布上下文、可书签恢复的会话 query；平台导航收进可打开的菜单。已删除旧封装大弹窗，构建配置在工作台内完成。

U2：从实际持久的 AssistantRequestRun 和用户消息恢复最近二十轮需求与状态；新公开事件携带运行轮次 identity。固定输入区支持修正、停止和继续，对话可打开对应文件与验证缺口。没有伪造终端、模型思考或执行成功。

U3：逐行增删、行号与完整文件入口；限制差异计算和预览数量。冲突保留草稿，核对后才能更新基准；文件切换、路由离开和刷新均保护未提交内容。

U4：顶部入口打开交付抽屉，集中保存版本、人工审阅与插件/市场包选择。后端继续重验发布、业务案例和版本身份；生成工件不等于上传外部仓库。

| 实际命令/检查 | 结果与范围 |
| --- | --- |
| Python 3.12 `-m pytest backend/tests -q -p no:cacheprovider --basetemp .tmp-plugin-studio-boundary-20261004l` | 后端全量 293 passed；PYTHONPATH 使用 backend 与已安装的 pytest 8.x 依赖，禁用插件自动加载、缓存和字节码 |
| `npm --prefix ./frontend test` | 248 passed，涵盖草稿保留、差异重建、停止、错误发布拒绝、上下文切换取消及迟到响应等 |
| `npm --prefix ./frontend run build` | vue-tsc + Vite 通过；现有依赖 PURE 注解、大 chunk 警告保留，未放宽门禁 |
| Python 3.12 `backend/scripts/verify_plugin_coding_postgresql.py` | 新建随机隔离 PG 数据库，迁移到读取到的实际 single head，验收后清理；持久对话、轮次、CAS、lease 取消、跨租户发布读取拒绝与版本冻结通过 |
| 浏览器 + 真实 Vue 组件 | 使用明确的合成 API、模型、回执、下载 Blob；不读取生产业务数据，不调用真实模型 |

浏览器走通：新建任务、三类不同案例、进入编码会话、编辑冲突保留草稿、核对并保存、反馈后继续、停止；刷新恢复历史；离开时提示未提交修改；受保护适配器只读；键盘 Enter 切换验证面板；交付升版、人工审阅和市场包按钮条件；发布不可用的重试/返回。390×844 页面宽度为 390，没有横向溢出，输入区可见；深色验证面板可读。提供 reduced-motion 样式，未切换操作系统运动偏好。

公开轮次投影实测一个 SQL 查询，返回数量上限二十；隔离 fixture 的单次读取耗时见结果 JSON，不作为生产负载基准。

[桌面实图](plugin-studio-desktop-2026-10-04.png) · [窄屏实图](plugin-studio-narrow-2026-10-04.png) · [隔离 PG 结果](plugin-studio-postgresql-results-2026-10-04.json)

旧 modal 证据只记录被替代的界面，不算本次验收。此次未重验远程仓库上传、真实第三方业务调用和真实模型质量。当前编码范围仍是场景 Skill、接入说明、Python 调用示例；统一执行适配器受保护，没有扩展运行时任意代码执行。
