# 本地数据迁移到 MySQL / MinIO

> **已废止（2026-08-27）**：本文只记录上一阶段从本地 SQLite 脱离的历史迁移，不再代表当前运行架构。当前权威方案是 PostgreSQL 控制面、MinIO 不可变数据资产、Redis 可失效缓存；业务关系不再作为平台 PostgreSQL/MySQL 专表。请以 [PostgreSQL-MinIO 通用数据资产架构](./PostgreSQL-MinIO-通用数据资产架构.md) 为准。

本迁移只保留两个业务场景：

- 代理记账业务：`56e2006148e8499e8599f5c7c8145e60`
- 医保违规审计：`cc5d3ff36d2a468596dfa9f8ef2995da`

迁移脚本为 `backend/scripts/migrate_local_to_services.py`。脚本从 SQLite 迁移固定的 58 张源平台表，并按当前 ORM 在 MySQL 创建 59 张目标平台表；新增的 `object_deletion_jobs` 事务 outbox 以空表开始。同时迁移代理记账 15 张表及 1 个视图、医保 4 张表及 2 个视图，并上传固定的 41 个业务文件。其他场景、6 张废弃平台表、医保 deprecated 实体、retired workflow、过期会话和全部邮箱验证码均不进入目标库。

Redis 仅用于可重建缓存，不承担最终存储。平台与业务事实数据以 MySQL 为准，文件内容以 MinIO 为准。

迁移兼容启用版本控制的 MinIO 以及不实现 versioning API 的 S3 网关。`dry-run` 只读探测并把能力固定为 `Enabled`、`Supported`（API 可用但桶未启用）或 `Unsupported`；`execute` 和 `verify` 必须探测到相同结果，不会修改 bucket 级 versioning 配置。`Enabled` 时数据库保存并验证精确 `object_version_id`；未启用或不支持时 `object_version_id` 可为空，但对象键必须由全局唯一的 `bucket_files.id` 隔离，且重试只接受同 key、同大小、同 ETag、同 SHA-256 的既有对象。运行时仍用 durable upload intent、对象删除任务和延迟二次 sweep 处理进程退出、提交响应丢失及迟到 PUT，Redis 不参与这些权威状态判断。

## 前置条件

在 `backend/.env` 配置 MySQL 目标地址和 MinIO。不要把管理员密钥写入命令、manifest、文档或日志。

MySQL 迁移管理员只能作为迁移进程的一次性环境变量提供：

```powershell
$env:MIGRATION_MYSQL_ADMIN_USER = "<migration-admin>"
$env:MIGRATION_MYSQL_ADMIN_PASSWORD = "<one-time-secret>"
$env:MIGRATION_MYSQL_ACCOUNT_HOST = "%"  # 可收紧为应用网段
```

首次切换为兼容旧配置，未提供 `MIGRATION_MYSQL_ADMIN_*` 时脚本可暂时使用当前 `ANNUAL_MYSQL_USER/PASSWORD`。但端到端 `verify` 成功后会立即将 `backend/.env` 中这两项原子切换为 `ontology_app` 和随机强密码，后续 `cleanup` 必须显式提供一次性管理员变量。不得把 `MIGRATION_MYSQL_ADMIN_*` 写进 `.env`。

`ontology_app` 只获得 `ontology_business.*` 上的 DML 以及应用 schema 升级所需的 `CREATE` / `ALTER` / `INDEX` / `REFERENCES` / view / 临时表权限，没有全局权限、跨库权限、`GRANT OPTION` 或 `CREATE USER`。应用启动后不再使用迁移管理员。

两个业务只读密码是可选配置：

```text
BOOKKEEPING_MYSQL_PASSWORD=...
MEDICAL_MYSQL_PASSWORD=...
```

未配置时，`execute` 会用 `secrets.token_urlsafe(32)` 为每个场景生成强随机密码，将密码直接写入对应 `DataSource.config` 并创建 MySQL 用户。明文不会进入 manifest 或控制台。用户名也可通过 `BOOKKEEPING_MYSQL_USER` 和 `MEDICAL_MYSQL_USER` 覆盖。

两个场景只读账号与运行账号共用 `MIGRATION_MYSQL_ACCOUNT_HOST`，其中场景账号仅能逐表 `SELECT` 自己的业务表/视图。

## 四个阶段

所有命令从 `backend` 目录运行。

### 1. dry-run

```powershell
python -m scripts.migrate_local_to_services dry-run
```

此阶段只读 SQLite、本地文件以及 MinIO bucket 的 versioning 状态；不会连接 MySQL，不会创建 bucket、上传对象或修改 versioning。它需要 MinIO 访问凭据和已存在的目标 bucket，并生成 `backend/migration-manifests/local-to-services.json`，记录表行数、规范化哈希、文件大小/SHA-256、MinIO 能力状态、精确 cleanup 清单和两个确认令牌。manifest 不保存任何凭据。

审核 manifest 中的场景 ID、源计数、目标地址（不含凭据）以及 cleanup 范围。源数据在 dry-run 后发生任何变化，后续阶段都会拒绝执行，必须重新 dry-run。

仅当旧 v2 已经完成 `execute`、因平台 ORM 时间列被 MySQL `DATETIME(0)` 量化而未通过 verify，且本地源尚未 cleanup 时，才使用受控重建参数：

```powershell
python -m scripts.migrate_local_to_services dry-run `
  --supersede-manifest .\migration-manifests\local-to-services-v2.json
```

旧 manifest 必须保持其原始不可变部分和 `target_expected`，状态严格为 `executed=true`、`verified=false`、`cleaned=false`。脚本使用旧 `snapshot_time` 重建新快照，并要求 source 和 target 与旧 manifest 完全相同；生成的 v3 manifest 将固定恢复模式、旧 plan digest 和旧 expected SHA-256 写入不可变 `supersedes` 描述符，新的 execute 确认令牌因此只对这次重建有效。不要手工把远端控制行改成 `running` 或清空 `expected_json`。

### 2. execute

进入 `execute` 前必须停止所有 API 进程、定时任务和 worker，并一直保持停机到 `cleanup` 完成。不要在 verify 与 cleanup 之间启动应用做 HTTP 测试：合法的 bootstrap、会话、outbox 或文件写入也会改变精确迁移哈希。

```powershell
python -m scripts.migrate_local_to_services execute `
  --confirm-execute MIGRATE_<manifest输出的令牌> `
  --batch-size 1000
```

执行顺序为：预检 Oracle MySQL >= 8.0 且已开启 strict SQL mode，检查源快照、准备 InnoDB schema、创建/检查 MinIO bucket 并复核 dry-run 记录的 versioning 能力、上传并复核 MinIO、批量导入平台与业务表、导入后创建索引和视图、创建两个最小权限只读账号和 `ontology_app` 运行账号，最后写入目标控制状态。脚本从不调用 `set_bucket_versioning`；不实现相关 API 的 S3 网关直接进入无版本兼容路径。`.env` 只在后续 verify 全部通过后切换。

目标库无迁移控制记录时，只允许自动清理由固定迁移契约覆盖且全部为空的半成品表。存在任一非空表、契约外对象或预先存在的专用账号都会拒绝接管。相同 manifest 留下 `running` 控制记录时，脚本才可重建该迁移自己拥有的半成品 schema。所有管理连接显式设置 `default_storage_engine=InnoDB`，平台、业务和控制表均显式创建为 InnoDB。

迁移 manifest 固定 `mysql_datetime_precision=6` 和 `business_view_target_semantics=mysql-fixed-select-unordered-v1`。所有当前 ORM `DateTime` 列均以 MySQL `DATETIME(6)` 创建；导入前会受控升级托管空表中已有的 `DATETIME(0)` 并复读确认，expected/readback 哈希保留完整微秒。业务视图以脚本内固定 MySQL SELECT 的无序结果哈希为目标语义，不把 SQLite 在不同排序规则下得到的预览哈希当作 MySQL 真值。上述契约变更后必须重新运行 dry-run；普通新 manifest 不会接管 `executed`/`verified` 旧目标，也不会仅靠 ALTER 冒充数据恢复。唯一例外是上述显式 v2 supersede 通道，它会从 SQLite 完整重导。

`execute` / `verify` / `cleanup` 全阶段同时持有 manifest 文件独占锁和 MySQL `GET_LOCK`。第二个迁移进程会立即失败，不能在导入或 cleanup 中途交错删表/删文件；进程中断后锁由 OS/MySQL 连接释放。

约 96 万条医保数据使用分批 `executemany` 导入。医保无类型源列按用途转换：映射/查询用 ID 和名称列为有界 `VARCHAR`，数值列为 `DECIMAL(30,8)`，其余为 `LONGTEXT`。项目明细使用迁移代理主键，不对记账流水号加唯一约束。要求的联合索引在数据导入后创建。

MinIO 对象键固定为：

```text
{prefix}/tenants/{tenant_id}/scenarios/{scenario_id}/data-sources/{ds_id}/files/{file_id}/{filename}
```

数据库保存 `storage_provider`、`bucket_name`、`object_key`、`object_version_id`、`etag`、`object_url` 和 `content_sha256`。`object_url` 是稳定的 `minio://bucket/percent-encoded-key`，同时镜像到 `stored_path`；不会保存临时预签名 URL。数据源配置使用 `storage_backend=minio`、`bucket_name` 和 `prefix`。

对象键中的 `file_id` 是 `bucket_files` 全局唯一主键，因此该键不会被另一文件复用。相同 manifest 重试会复用已验证的同字节对象而不覆盖；不同内容占用同 key、重复 file_id、key/file_id 不一致或场景前缀下出现清单外对象都会立即失败。这个 canonical key 同时是运行时 `datasource_service.build_bucket_object_key` 的完整性契约，不能额外插入计划摘要路径段。

若旧 plan digest 已在目标控制表留下失败状态，普通新 manifest 只允许接管 `status=running` 且 `expected_json={}` 的同名迁移。显式 v2 supersede 会在任何 DDL 前验证：旧 expected 的精确对象集合和 InnoDB、平台列契约、全部业务基础表哈希、索引和外键，以及 41 个 MinIO 对象的精确集合、版本和实际字节 SHA-256。旧业务视图不再与 SQLite 预览哈希比较，而必须与脚本内固定 MySQL SELECT 的独立执行结果完全相同。

平台恢复先从远端 `DataSource.config` 取回旧只读凭据，并从旧 expected 重建已上传文件映射。脚本检查两个迁移 SQL binding 的 `checked_at` / `updated_at`（脚本生成的 binding 还包括 `created_at`）均为同一个远端 DATETIME(0) 秒。旧控制行的 `created_at` 可能是更早失败计划保留下来的 crash anchor，因此只要求它不晚于 `updated_at`；候选时间窗口以上界 `control.updated_at`（旧 v2 写入 executed expected 的时间）为准，并要求 `updated_at >= snapshot_time`。随后在量化秒相邻的精确 100 万个微秒候选中，以旧 `connector_bindings` 哈希唯一恢复原 `executed_at`。使用该时间完整重建全部平台行后，每张表都必须先重现旧 expected 哈希，再逐主键逐列与远端比较。非时间字段必须严格相等；ORM `DateTime` 字段唯一允许 MySQL DATETIME(0) 的 round-half-up 量化，即微秒 `<500000` 保留本秒，`>=500000` 进位到下一秒，且远端微秒必须为 0。每张哈希不匹配表至少要有一处这种可解释的时间差异。任一候选不唯一、控制时间窗口无效、非时间变化或无法重建都会拒绝接管。

上述数据库与 MinIO preflight 全部通过后，脚本才以旧 digest、`executed` 状态和 canonical expected JSON 为条件原子 CAS 到新 digest 的 `running/{}`，提交后再删除托管对象；CAS 后或删表中途崩溃均由现有同 digest running 路径继续。`verified`、错误 digest/hash、业务字段变化、MinIO 变化或额外对象始终拒绝。

### 3. verify

```powershell
python -m scripts.migrate_local_to_services verify
```

`verify` 期间应用必须保持停机，不允许产生新业务行、文件版本或 outbox 任务。所有检查通过后，脚本才原子切换 `.env` 到 `ontology_app`。

验证包括：

- 目标对象集合恰好符合迁移契约，平台和业务基础表全部为 InnoDB；
- 平台列集合、顺序、类型、长度和 nullable 与当前 ORM 一致；
- 所有 ORM 时间列的 `information_schema.COLUMNS.DATETIME_PRECISION` 均为 `6`；
- 所有平台表和业务基础表的行数及规范化内容哈希一致；每个业务视图同时等于 manifest 记录的 MySQL 目标哈希和脚本内固定 SELECT 的独立执行结果；
- 平台外键无孤儿，业务要求的索引存在且列顺序正确；
- 两个只读账号只能 `SELECT` 自己场景的业务表/视图，不能读取平台表或另一个场景；
- `ontology_app` 具备目标库 CRUD 和应用 DDL 的固定最小权限，但不能读 `mysql.user`、跨库或 `CREATE USER`；
- MinIO versioning 能力与 dry-run 完全一致；`Enabled`/`Supported` 按记录的精确版本（允许未启用桶的空版本）下载并核对全版本及删除标记集合，`Unsupported` 按精确 key 下载并核对当前对象集合；41 个对象均验证 key、大小、ETag 和实际字节 SHA-256，不信任客户端写入的 SHA 元数据；
- DataSource、adapter、binding、revision 和 BucketFile 对象字段已由平台表哈希覆盖。

任一检查失败时不会写入 `verified`，也不允许 cleanup。

### 4. cleanup

```powershell
python -m scripts.migrate_local_to_services cleanup `
  --confirm-cleanup CLEANUP_<manifest输出的令牌>
```

cleanup 在同一次 manifest 文件锁和 MySQL `GET_LOCK` 持有期间先重新执行完整端到端 verify，再按 manifest 的精确文件清单逐项复核路径、大小和 SHA-256；不会通过公开 verify 入口二次获取锁。fresh verify 失败时不会删除任何本地文件。它不使用 glob 删除，不递归删除未知内容，也不会删除新出现或被修改的文件。删除进度写入 manifest journal，数据库文件最后删除；非空目录会保留。

`cleanup` 完成前应用仍必须保持停机；否则精确行数、MinIO 全版本/删除标记集合（或无版本网关的当前对象集合）或 outbox 空表契约改变时，脚本会拒绝 cleanup。

cleanup 是正式切换的最后一步。执行前应保留经审核的离线备份，并确认 `.env` 已由 verify 切换为 MySQL/MinIO 运行配置，但此时仍不启动应用。不要在 `execute` 尚未完成或 `verify` 未通过时手工删除 `backend/data`。cleanup 成功后再启动 API/worker 并执行 HTTP 和 4 个医保策略的端到端验证。

## 本地验证

迁移实现的离线单元测试不会访问远端服务：

```powershell
python -m pytest tests/test_migrate_local_to_services.py -q
python -m py_compile scripts/migrate_local_to_services.py
```

代码合入阶段只运行上述本地检查。正式 `execute`、`verify` 和 `cleanup` 必须在审核 manifest 及确认令牌后由运维窗口执行。
