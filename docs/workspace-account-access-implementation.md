# 工作区成员与系统账户管理

## 需求账本（2026-09-07）

| ID | 原文目标 | 非目标 | 可观察验收 | 影响层/文件 | 保持不变项 | 测试证据 |
| --- | --- | --- | --- | --- | --- | --- |
| W1 | 参考线上成员与权限，邀请/移除协作者 | 不复制整个 test 分支 | 所有者/管理员管理成员；角色、移除、重邀有状态与冲突提示 | members UI → workspace router/service → organizations/members | 场景 ACL、租户隔离与统一执行 | 成员行为、真实 PG 双所有者竞态及浏览器移除/重邀通过 |
| W2 | 邮件带线上访问链接，登录同意加入 | 不代用户注册或自动接受；URL 不含认证密钥 | 已验证收件邮箱登录后接受/拒绝；可切换工作区；邀请过期/撤销不可接受 | invitations UI → invitation service → invitation delivery worker + SMTP | 个人工作区及已有业务归属 | 邮箱绑定、过期/撤销/重复接受、邮件内容/双 worker 测试通过；浏览器接受与切换通过；真实 SMTP 待部署验收 |
| A1 | 超级管理员了解平台使用账户，禁用违规账户 | 不删除账户或客户数据 | 有界搜索分页；禁用/恢复；旧会话及凭据失效；禁用不能被注册/验证码解禁 | accounts UI → system account service → users/audit | 业务数据不迁移 | auth/session/API 全量回归通过；浏览器禁用、恢复及审计通过 |
| A2 | 系统角色与工作区角色分离，超级管理员提权降权 | 不按账户名或创建时间猜超级管理员；系统角色不越过工作区 ACL | 系统仅 user/superadmin；工作区 owner/admin/operator/viewer；最后有效管理员/所有者保护、revision 冲突与审计 | system guard + migrations + role consumers | 既有协议角色 token | 权限矩阵、双事务降权、角色归属 FK、隔离迁移往返通过 |

## 影响图及决定

成员/邀请/账户页面、导航与认证 store → typed API/DTO → 独立工作区、邀请、账户服务 → 用户/会话增量字段、邀请/审计/限流表及 Alembic → 有界邮件投递 worker → 单元/协议/PG/浏览器测试、部署配置与本文。

现有 `User.tenant_id` 保留为初始工作区；当前工作区由浏览器会话保存，权限以有效 OrganizationMember 为准。外部 API 凭据沿用签发时的工作区，不跟随浏览器切换。切换后刷新页面以清理旧页面、缓存和流连接。

邀请只记录目标邮箱，不改变目标账户密码、状态或原有工作区。邮件链接指向配置的 `PUBLIC_APP_URL` 的 `/invitations`；登录、验证邮箱和显式接受共同完成加入，不使用 URL bearer token。邀请 24 小时有效；发送结果未知显示“投递结果待确认”，不自动盲重发。

第一位超级管理员通过部署者明确配置 `BOOTSTRAP_SUPERADMIN_EMAIL`、在邮箱已验证后执行一次性 bootstrap；不猜测历史用户。后续升降权只走管理接口。并发管理写入由持久 guard 行串行化，并携带 expected_revision。工作区角色调整/移除保护最后有效所有者；系统禁用适用于个人工作区所有者，保留其数据与成员关系并拒绝所有入口访问，不能让个人工作区阻止平台账户治理。

补充必要消费者：Agent Turn、Assistant Request 和 Managed Upload 的发起账户原来与账户初始工作区做复合 FK，该关系不适用于全局账户。迁移将其改为全局 User 引用；任务与场景、父任务和数据对象的租户复合 FK 保留，入队和执行前仍要求当前有效成员身份。真实 PostgreSQL 测试覆盖共享工作区发起任务，以及拒绝跨工作区数据对象。

浏览器 session 改用 v2 域分离哈希；升级撤销旧会话并要求重新登录，不保留无域分离回退。新增 cookie 写请求 Origin/Referer 校验与持久认证限流，并修复禁用账户可通过旧注册验证码恢复的入口。

## 验证记录

环境：当前分支 dev；开始时 git status 干净；项目 venv Python 3.12.14，Node 24.18.1，npm 11.16.0；实际原 Alembic single head 为 20260905_24。依赖未变化，不重写 lockfile。

线上参考只做读取，未发送邀请或修改线上成员。

### 自动化验收结果

- 后端所有 97 个 `test_*.py` 文件按排序后索引取模分为 4 组，分别用项目 Python 执行 `-m pytest <该组文件> -q --tb=short`。结果为 143/292/253/330 项通过，共 1,018 项通过、12 项显式 opt-in PG 测试跳过。单进程全量运行因耗时较长中止后改为分组，未删除测试或放宽断言。日志：`.runtime/access-backend-group-{0,1,2,3}.log`。
- 设置 `RUN_POSTGRESQL_INTEGRATION_TESTS=1`，执行 `python -m pytest backend/tests/test_workspace_access_postgresql.py -q`：7 项通过。测试自行创建隔离数据库，验证双管理员降权、双所有者移除、重复接受、工作区角色 FK、任务数据 FK、最小权限及邮件 lease/fencing。
- 使用 `tests.access_postgresql.isolated_access_database()` 创建另一隔离库，将其运行角色 URL 仅通过子进程环境传入，再执行 `python -m pytest backend/tests/test_catalog_postgresql_concurrency.py -q`：5 项通过。因此默认回归中的 12 项 PG 测试均已另行执行通过。
- `python backend/scripts/verify_alembic_roundtrip.py`：通过，自动读取的 single head 为 `20260907_25`，完成隔离数据库升级、回退、再升级及运行权限检查。
- `npm --prefix frontend test`：103 项通过；`npm --prefix frontend run build`：通过。构建保留既有大 chunk 提示；后端保留 Pydantic settings/Starlette 的依赖警告。
- `git diff --check`：通过；未改依赖版本或 lockfile，未提交生成物。

### 浏览器验收

使用真实 API、Cookie、中间件、前端和独立 PostgreSQL 数据库，邮箱为 synthetic fixture；SMTP 调用模拟成功，未发送真实邮件。测试后端 `127.0.0.1:18080`，前端 `127.0.0.1:5174`，仅启动邀请投递 worker。

已验证：受保护邀请链接登录后返回、创建邀请、收件人接受、个人/共享工作区切换、操作员只读成员页、普通账户访问账户管理被服务端拒绝、超级管理员禁用/恢复账户及审计原因、成员移除与重新邀请、刷新后状态恢复、邀请 Tab 前进/后退、键盘打开/取消对话框。检查桌面和 390/320 像素窄屏，修复新增入口与既有移动端顾问按钮重叠；表格内部可横向滚动，账户操作列固定。请求失败/重试、卸载取消与迟到响应由前端行为测试覆盖；陈旧写入和权限并发由后端行为/PG 测试覆盖。

### 首次实现时的部署前剩余事项

只读检查显示本机配置指向的业务库仍为 `20260905_24`，尚未对该库执行迁移。需按 README 完成迁移、`PUBLIC_APP_URL`、SMTP、安全 Cookie 和首位系统管理员配置，然后运行 `verify_postgresql_runtime.py`。本次未运行升级后实际部署的 MinIO/Redis 健康检查，也未验证真实 SMTP 收件；不能将隔离验收等同于线上部署完成。

### 本地启动修复（2026-09-07，用户要求处理版本不匹配）

| ID | 目标及影响图 | 保持不变项 | 可观察验收与结果 |
| --- | --- | --- | --- |
| L1 | 后端启动版本不匹配 → 已有 Alembic 迁移 → 当前配置数据库 → init_db/启动检查 | 应用版本校验、业务权限逻辑及 Redis 配置 | 迁移到实际 single head；init_db 通过；实际 Uvicorn 启动成功 |

迁移前确认 runtime 与 admin 配置的数据库目标一致，且现有账户状态和成员角色归属满足新约束。使用项目 Python 3.12 执行以下命令，无源码行为改动：

```powershell
.\.venv\Scripts\python.exe -m alembic -c .\backend\alembic.ini heads
.\.venv\Scripts\python.exe -m alembic -c .\backend\alembic.ini -x use_admin=1 upgrade head
.\.venv\Scripts\python.exe -m alembic -c .\backend\alembic.ini current
$env:PYTHONPATH=(Resolve-Path .\backend).Path
.\.venv\Scripts\python.exe -c "from app.database import init_db; init_db(); print('init_db passed')"
.\.venv\Scripts\python.exe .\backend\scripts\verify_postgresql_runtime.py
```

已从 `20260905_24` 升级到实际 head `20260907_25`；`init_db()` 通过。随后使用原应用和本机配置启动 Uvicorn，日志显示 `Application startup complete`，监听 `127.0.0.1:8000`。`/api/health` 返回 HTTP 200，PostgreSQL/MinIO 为 `ok`，Redis 为 `error`，总体 `degraded`。

完整运行验证脚本通过 Schema、运行角色权限和 MinIO 检查，但因 Redis `AuthenticationError` 未全部通过。Redis 配置未修改；真实 SMTP 收件仍未验证。迁移按既定 v2 cutover 撤销旧会话及验证码，用户需重新登录。
