# PostgreSQL / MinIO 通用数据资产架构

## 当前结论

平台已经从 MySQL 切换到 PostgreSQL。数据库只保存跨行业通用的控制面、数据目录、语义映射、血缘、推导运行和证据索引；业务数据文件及其不可变版本保存在 MinIO。Redis 只承载带 TTL、可随时重建的缓存。

医保违规审计和代理记账是两个普通业务场景，不拥有平台专用数据库结构。`规则表`、`结算表`、`项目明细表`、`就诊表` 以及代理记账关系均为数据集中的逻辑关系，不是 PostgreSQL 业务专表。

| 组件 | 职责 | 是否权威 |
| --- | --- | --- |
| PostgreSQL | 租户、场景、本体、映射、Agent、工作流、资产目录、数据集版本、血缘、推导和证据元数据 | 是，控制面权威 |
| MinIO | 原始上传、重建归档、Parquet 分片、manifest、生成文件和证据文件 | 是，文件与大体量业务数据权威 |
| Redis | Schema、连接探测和其他短期查询结果缓存 | 否，可清空、可降级 |
| DuckDB | 在 API 进程内只读执行已固定版本的 Parquet 查询与受控派生视图 | 否，无持久状态 |
| MySQL | 本次迁移的只读回退源 | 否，不再被运行时连接器使用 |

这套边界避免把 PostgreSQL 变成所有行业事实表的集合，也避免让 Redis 成为隐性数据库。未来需要更高并发分析时，可以通过 `serving_projections` 为某个数据集版本增加 ClickHouse、搜索或向量投影；投影始终可重建，不能取代 MinIO 数据集版本和 PostgreSQL Catalog。

## 通用模型

核心对象如下：

- `data_assets` / `data_asset_versions`：稳定资产身份及不可变内容版本，指向 `bucket_files` 中的 MinIO 对象身份。
- `logical_datasets`：与业务场景解耦的数据产品身份。
- `dataset_schemas` / `dataset_relations` / `dataset_fields`：版本化 Schema、逻辑关系和字段契约。
- `dataset_versions` / `dataset_fragments`：固定内容哈希的数据集版本及 Parquet 分片。
- `dataset_heads`：开发、预发、生产环境的原子版本指针。
- `scenario_dataset_bindings`：场景以 input、reference、rules 或 output 角色消费数据集。
- `ingestion_runs` / `dataset_lineage_edges`：可恢复导入与版本血缘。
- `semantic_mappings`：把数据集字段映射到本体属性，而不是让物理表决定本体。
- `derivation_runs` / `assertions` / `derivation_evidence`：业务逆向推导的输入固定、断言和证据链。
- `serving_projections`：面向高并发查询、搜索、向量或缓存的可重建加速层。

Catalog 在数据库层通过复合唯一键和复合外键闭合 tenant、scenario、dataset、schema、relation、field、asset source 和 MinIO source 范围，并强制版本与 Schema、父版本、Head、场景绑定、Fragment 与逻辑关系一致。语义映射不能跨场景或跨数据集拼接；数据型推导证据必须来自该 assertion 对应 derivation run 已固定的 dataset input，场景化 ontology term 不能跨场景复用。关键内容摘要必须是精确 64 位小写 SHA-256，不能依赖应用代码自行约定。

`ontology_app` 是无超级用户、无继承、无 RLS 绕过、无复制、无角色成员关系、无 `public` Schema CREATE 的运行账号。不可变版本、Schema、Fragment、断言和证据表只允许 `SELECT/INSERT`，迁移台账只允许 `SELECT`；`dataset_heads`、`ingestion_runs` 和 `derivation_runs` 等运行状态表保留必要的 `UPDATE`。Schema DDL 和迁移台账写入只能由迁移 owner/admin 执行。

## 文件与查询契约

MinIO 对象使用内容寻址路径，数据库保存 `bucket_name`、`object_key`、`object_version_id`、`etag`、`object_url`、字节数和 SHA-256。`object_url` 使用稳定的 `minio://` 身份，不保存会过期的预签名 URL。

一次数据集查询按以下顺序执行：

1. 从 PostgreSQL 按 `dataset_version_id` 读取 Catalog，并验证租户、Schema、字段、版本和 Fragment 范围。
2. 从 MinIO 流式下载指定 Parquet 到内容寻址临时缓存，复核字节数与 SHA-256。
3. 在隔离的内存 DuckDB 中注册基础关系。
4. 校验 manifest 中的派生 `SELECT`，拒绝 DDL、多语句、参数、循环依赖、目录外关系和外部扫描，再按依赖拓扑创建视图。
5. 执行只读、参数化、有限行数的业务查询；进程退出后 DuckDB 状态消失。

查询策略按方言拒绝可执行注释、外部扫描、文件输出、动态查询、锁等待和资源滥用函数。DuckDB 默认限制为单查询 30 秒、512 MiB 内存、2 个线程、1 GiB 临时空间和每进程 4 个并发查询；超时会调用 `interrupt()` 覆盖执行与取数阶段。Parquet 临时缓存使用内容寻址、对象/总量/年龄上限、跨进程锁和连接生命周期 lease，在用文件不会被淘汰。多 worker 部署必须按 worker 数量核算总内存、临时盘和并发预算。

## 本次迁移结果

- 只保留代理记账业务和医保违规审计两个场景。
- PostgreSQL 导入 59 张平台控制面表、3,471 条既有记录。
- 代理记账归档 15 个基础关系、100 行，并注册 1 个受控派生视图。
- 医保审计归档 4 个基础关系、966,499 行，并注册 2 个受控派生视图。
- MinIO 保存 19 个不可变 Parquet 和 2 个 dataset manifest。
- 两个原 SQL 数据源原位转换为无凭据的 `dataset` 连接器；21 条对象映射、12 条关系映射改绑通用关系 ID。
- MySQL 源未删除、未改表、未写入，保留为回退依据。

原始 SQLite/XLSX 已不完整，因此本次 MinIO 资产明确标记为 `legacy_mysql_reconstruction`：它是从保留 MySQL 关系重建的 Parquet，不冒充已经丢失的原始上传文件。

## 验证与运维

迁移器为 `backend/scripts/migrate_mysql_to_postgresql.py`，阶段依次为：

```powershell
python backend/scripts/migrate_mysql_to_postgresql.py plan
python backend/scripts/migrate_mysql_to_postgresql.py bootstrap --confirm <phase-token>
python backend/scripts/migrate_mysql_to_postgresql.py archive --confirm <phase-token>
python backend/scripts/migrate_mysql_to_postgresql.py import --confirm <phase-token>
python backend/scripts/migrate_mysql_to_postgresql.py verify
python backend/scripts/migrate_mysql_to_postgresql.py cutover --confirm <phase-token>
```

`plan` 不产生远程写入；`verify` 不改业务数据，但会把确定性的验收结果写入 PostgreSQL 迁移台账。其余阶段需要 manifest 中各自独立的确认令牌。所有 MySQL 反射与扫描都在同一条已验证 `TRANSACTION READ ONLY` 的一致性快照连接内完成；迁移器不依赖源账号本身一定是只读账号。PostgreSQL 的版本化 migration checkpoint 是 import、verify、cutover 的恢复权威，本地 manifest 即使在远端提交后丢写，也可通过重跑同一命令恢复。

应用层验收命令：

```powershell
python backend/scripts/verify_postgresql_runtime.py
```

它验证实际 `current_user` 就是配置的 PostgreSQL runtime 账号，并拒绝超级用户、建库/建角色、继承、RLS 绕过、复制、任何角色成员关系或 `public` Schema CREATE 权限；同时验证两个无凭据数据集连接器、MinIO/Parquet、代理记账派生视图、医保规则审计和 Redis TTL 往返。Redis 失败时正常业务查询必须回退到 PostgreSQL/MinIO，不能把缓存命中作为正确性前提。

## 回退边界

- `backend/.env.pre-mysql-rollback.<run-id>.bak` 是迁移器生成并校验的独立 MySQL 配置回退点：它明确设置 `DATABASE_BACKEND=mysql`、移除会覆盖选择器的 `DATABASE_URL`，包含密钥且已被 Git 忽略。旧版 `.env.pre-postgresql-cutover.*.bak` 不能视为可用的 MySQL 回退点。
- `backend/migration-manifests/` 保存本机迁移检查点和确认令牌，已被 Git 忽略。
- `backend/migration-backups/` 保存升级前 PostgreSQL custom-format dump，包含平台数据且已被 Git 忽略；恢复前应先用 `pg_restore --list` 校验归档。
- 回退前必须停止 API/worker，核对 PostgreSQL 迁移台账和 MySQL 源指纹；不得同时让 PostgreSQL 与 MySQL 接受平台写入。
- PostgreSQL 已切换后产生的新平台写入不会自动反向同步到 MySQL。MySQL 只用于短期灾难回退，不是双写副本。
- 未完成独立备份和保留期评审前，不删除 MySQL 源或 MinIO 迁移前对象。
