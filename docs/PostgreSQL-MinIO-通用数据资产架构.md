# PostgreSQL / MinIO / Redis 存储架构

## 存储边界

平台正式存储边界只有 PostgreSQL、MinIO 和 Redis：

| 组件 | 职责 | 是否权威 |
| --- | --- | --- |
| PostgreSQL | 租户、场景、本体、映射、Agent、工作流、资产目录、数据集版本、血缘、推导和证据元数据 | 控制面权威 |
| MinIO | 原始上传、Parquet 分片、manifest、生成文件和证据文件 | 文件与大体量业务数据权威 |
| Redis | Schema、连接探测和其他短期查询结果缓存 | 可清空、可降级 |

DuckDB 只在 API 进程内读取已校验的 MinIO Parquet，作为无状态查询引擎，不持久化业务数据。

PostgreSQL 不保存上传表格的业务行、Excel 单元格或 Parquet 内容，只保存租户、逻辑资产、版本、Schema、行数、摘要、血缘、任务状态和 MinIO 对象身份。文档检索的兼容索引属于另一条明确的数据产品边界；表格上传不会进入 `parsed_text` 或 `document_chunks`。

医保审计和代理记账是普通业务场景，不拥有平台专用数据库结构。业务关系、字段和规则均通过数据集目录与语义映射描述，由 PostgreSQL 保存目录和映射元数据，由 MinIO 保存数据资产。

## 通用模型

- `data_assets` / `data_asset_versions`：稳定资产身份及不可变内容版本，指向 MinIO 对象身份。
- `logical_datasets`：与业务场景解耦的数据产品身份。
- `dataset_schemas` / `dataset_relations` / `dataset_fields`：版本化 Schema、逻辑关系和字段契约。
- `dataset_versions` / `dataset_fragments`：固定内容哈希的数据集版本及 Parquet 分片。
- `dataset_heads`：开发、预发、生产环境的原子版本指针。
- `scenario_dataset_bindings`：场景按环境以 modeling_evidence、test_fixture、invocation_input、reference、rules 或 output 角色消费数据集。建模证据和测试夹具不会自动进入生产调用上下文。
- `ingestion_runs` / `dataset_lineage_edges`：可恢复导入与版本血缘。
- `semantic_mappings`：把数据集字段映射到本体属性。
- `derivation_runs` / `assertions` / `derivation_evidence`：业务推导的输入固定、断言和证据链。
- `serving_projections`：面向查询、搜索、向量或缓存的可重建加速层。

关键目录、版本、映射和证据均由 PostgreSQL 的复合唯一键、复合外键和摘要校验闭合。不可变数据版本与证据对象保存在 MinIO，Redis 中的数据必须可以从 PostgreSQL 和 MinIO 重建。

业务场景的能力定义只保存数据端口、Schema、语义映射和逻辑 binding key，不保存某一批客户数据。每次调用通过 `RunInputBinding` 固定最终 `DatasetVersion` 或经过验签的实时 `ConnectorBinding`；Definition hash 与数据绑定 fingerprint 分开计算，因此更换数据批次不会改变能力版本。

## 文件与查询契约

MinIO 对象使用内容寻址路径，数据库保存 `bucket_name`、`object_key`、`object_version_id`、`etag`、`object_url`、字节数和 SHA-256。`object_url` 使用稳定的 `minio://` 身份，不保存会过期的预签名 URL。

目录、建模资料和验证中心默认支持单文件 2 GiB，采用 4 MiB 网络读取块和私有暂存文件；小于 8 MiB 的兼容路径才允许一次性读取。Office 压缩容器默认最多展开 8 GiB。CSV、TSV、XLS、XLSX 和 XLSM 在后台转成 zstd Parquet，单个 Parquet 目标大小约 256 MiB，避免大对象超过查询节点缓存上限。原始文件和派生 Parquet 都在 MinIO，转换失败不会留下可见的半成品版本。

验证中心有两种生命周期：`validation_asset` 默认持久保留并可跨对话复用；`invocation_attachment` 只用于一次性调用并按 TTL 清理。用户显式删除资产时，PostgreSQL 先退役逻辑版本并登记删除 Outbox，再删除 MinIO 原始对象和依赖分片。场景 Definition/Release 只保存数据端口契约而不保存验证批次，因此该删除不会破坏已经发布的能力。

一次数据集查询按以下顺序执行：

1. 从 PostgreSQL 按 `dataset_version_id` 读取 Catalog，并验证租户、Schema、字段、版本和 Fragment 范围。
2. 从 MinIO 流式下载指定 Parquet 到内容寻址临时缓存，复核字节数与 SHA-256。
3. 在隔离的内存 DuckDB 中注册基础关系。
4. 校验 manifest 中的派生 `SELECT`，拒绝 DDL、多语句、参数、循环依赖、目录外关系和外部扫描，再按依赖拓扑创建视图。
5. 执行只读、参数化、有限行数的业务查询；进程退出后 DuckDB 状态消失。

查询默认限制为单查询 30 秒、512 MiB 内存、2 个线程、1 GiB 临时空间和每进程 4 个并发查询。超时会覆盖执行与取数阶段。Parquet 临时缓存使用内容寻址、对象/总量/年龄上限、跨进程锁和连接生命周期 lease，在用文件不会被淘汰。

## 部署与验证

1. 复制 `backend/.env.example` 为 `backend/.env`，填写 PostgreSQL、MinIO 和 Redis 配置。
2. 使用数据库 owner 执行 `alembic upgrade head`。
3. 使用运行账号启动 API；启动时只核验当前迁移版本，不隐式执行 DDL。
4. 执行 `python backend/scripts/verify_postgresql_runtime.py`，只读验证 PostgreSQL Schema/运行角色权限、MinIO 和可选 Redis 健康；业务能力另按 Definition 与调用契约验收。

运行账号不得拥有超级用户、建库、建角色或 Schema DDL 权限。Schema 迁移只能由迁移 owner/admin 执行；Redis 不可用时，业务正确性必须仍由 PostgreSQL 和 MinIO 提供。
