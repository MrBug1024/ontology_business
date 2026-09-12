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

任意行业或客户场景都不拥有平台专用数据库结构。业务关系、字段和规则均通过数据集目录、语义映射、Provider 配置或用户定义描述，由 PostgreSQL 保存受治理元数据，由 MinIO 保存数据资产。

## 通用模型

- `data_assets` / `data_asset_versions`：稳定资产身份及不可变内容版本，指向 MinIO 对象身份。
- `logical_datasets`：与业务场景解耦的数据产品身份；与 `data_assets` 一样带权威 `usage_plane`。
- `dataset_schemas` / `dataset_relations` / `dataset_fields`：版本化 Schema、逻辑关系和字段契约。
- `dataset_versions` / `dataset_fragments`：固定内容哈希的数据集版本及 Parquet 分片。
- `dataset_heads`：开发、预发、生产环境的原子版本指针。
- `scenario_dataset_bindings`：场景按环境以 modeling_evidence、test_fixture、invocation_input、reference、rules 或 output 角色消费数据集。建模证据和测试夹具不会自动进入生产调用上下文。
- `ingestion_runs` / `dataset_lineage_edges`：可恢复导入与版本血缘。
- `semantic_mappings`：把数据集字段映射到本体属性。
- `derivation_runs` / `assertions` / `derivation_evidence`：业务推导的输入固定、断言和证据链。
- `serving_projections`：面向查询、搜索、向量或缓存的可重建加速层。
- `agent_turn_runs` / `agent_turn_events`：异步 Agent Turn 的请求台账、lease/fencing 状态和单调 revision 事件；API 进程重启后仍可恢复。
- `assistant_request_runs`：带附件全局顾问请求的持久执行台账；用户消息、助手占位消息与请求 intent 原子写入，后台按 tenant/user 恢复 principal，并以 revision、lease generation/token 和 fencing 防止重复或迟到结果。
- `managed_upload_runs`：第一方大文件登记、内容接收和后台解析的持久台账，保存 tenant/user 归属、请求指纹、声明/实际字节、内容摘要、revision、状态和 lease/fencing；不保存文件正文。
- `connector_bindings`：按 tenant/scenario/environment 保存可移植 `binding_key`、健康状态和连接器签名；显式健康检查还保存有界、无凭据的 `structure_profile` 与 SHA-256 `structure_fingerprint`。
- `object_deletion_jobs`：除删除 Outbox 外也承载 MinIO PUT 前先提交的持久上传 intent，以 generation/token 和 heartbeat 关闭对象已写入、目录元数据尚未提交的崩溃窗口。

关键目录、版本、映射和证据均由 PostgreSQL 的复合唯一键、复合外键和摘要校验闭合。不可变数据版本与证据对象保存在 MinIO，Redis 中的数据必须可以从 PostgreSQL 和 MinIO 重建。

业务场景的能力定义只保存数据端口、Schema、语义映射和逻辑 binding key，不保存某一批客户数据。每次调用通过 `RunInputBinding` 固定最终 `DatasetVersion` 或经过验签的实时 `ConnectorBinding`；Definition hash 与数据绑定 fingerprint 分开计算，因此更换数据批次不会改变能力版本。

`data_assets` 与 `logical_datasets` 的用途只允许 `modeling_material`、`invocation_input` 或 `generated_output`，数据库 check constraint 与服务端 DTO 同时约束。无法由既有持久关系证明用途的存量对象保守回填为 `generated_output`，不会按名称或数量猜测。建模证据绑定、语义映射、能力内容契约和建模下拉选项只能从 `modeling_material` Dataset Schema 产生；验证中心只把 `invocation_input` 资产转换为同平面的数据集，其解析结果和运行数据包不能反向成为建模元数据，Agent Turn 也拒绝把 `modeling_material` 附件当作正式运行输入。

## 文件与查询契约

MinIO 对象使用内容寻址路径，数据库保存 `bucket_name`、`object_key`、`object_version_id`、`etag`、`object_url`、字节数和 SHA-256。`object_url` 使用稳定的 `minio://` 身份，不保存会过期的预签名 URL。

目录、建模资料和验证中心默认支持单文件 2 GiB，采用 4 MiB 网络读取块和私有暂存文件；小于 8 MiB 的兼容路径才允许一次性读取。Office 压缩容器默认最多展开 8 GiB。第一方验证附件先用 `POST /api/catalog/upload-runs` 登记持久任务，再向固定的 `POST /api/catalog/upload-runs/content` 提交任务 ID、expected revision 和文件；内容接收只负责校验声明/实际字节、SHA-256、租户所有权和流式写入，解析由后台 worker 完成。CSV、TSV、XLS、XLSX 和 XLSM 随后可转成 zstd Parquet，单个 Parquet 目标大小约 256 MiB，避免大对象超过查询节点缓存上限。原始文件和派生 Parquet 都在 MinIO，转换失败不会留下可见的半成品目录版本。

上传任务按 `awaiting_upload -> uploading -> stored -> processing -> ready/failed` 推进。上传和解析各自使用持久 lease generation/token；最终目录写入再次核对 lease，迟到 worker 不能覆盖新持有者。API 启动会把已过期且尚无对象的 `uploading` 任务恢复为 `awaiting_upload`，worker 可接管 lease 已过期的 `processing` 任务；失败重试携带 expected revision，并根据原始对象是否已存在恢复到 `stored` 或 `awaiting_upload`。worker 从台账恢复原 tenant/user principal 后重新校验，不把 Redis 或进程内队列当权威。

Agent 消息可以引用同一用户仍在上传或解析的任务：Turn 与首个事件先在 PostgreSQL 持久化并立即返回 `202 Accepted`，worker 进入 `preparing_inputs` 等待上传 ready，必要时再异步构建表格数据包，最后固定内容签名和资产/数据集版本后才做契约校验与规划。HTTP 请求、页面 SSE 或浏览器断开都不承担后台任务权威。

全局顾问的带附件请求采用相同边界：HTTP/SSE 只返回已持久化的 `AssistantRequestRun`，不在请求线程等待上传或调用 LLM。worker 只在附件 ready 后通过有界检索服务构建提示上下文；取消和重试携带 expected revision，任务终态、消息顺序与最终回复均从 PostgreSQL 恢复。

MinIO PUT 开始前还必须先独立提交持久上传 intent；上传过程中同时续租任务 lease 和对象 intent，提交目录行前再次校验两者及对象身份。若进程在 PUT 后崩溃，清理 worker 可根据 intent 回收未被目录元数据接纳的孤立对象；迟到上传者不能把已失去 lease 的对象发布为可见版本。

输入内容契约是端口 Schema 的可选扩展。未声明时只执行基础 selector、必填性与基数检查；声明后，文件按内容解析得到的表头、字段别名、逻辑类型和最低数据行数匹配，文件名、工作表显示名和对象路径不参与裁决。契约允许额外字段和额外关系，未命中端口的额外文件仍作为本次附件保留；`cardinality=many` 保留全部不同输入，`one` 的多重命中或一份输入匹配多个端口会拒绝歧义。Connector 端口使用最近一次显式健康检查持久化的结构轮廓执行同一校验，连接目标变化时该轮廓失效。

验证中心有两种生命周期：`validation_asset` 默认持久保留并仅在同一 Agent 的对话间复用；`invocation_attachment` 只用于一次性调用并按 TTL 清理。Agent 归属是服务端隔离边界，其他 Agent 不得列出、读取或删除该附件数据源。用户显式删除资产时，PostgreSQL 先退役逻辑版本并登记删除 Outbox，再删除 MinIO 原始对象和依赖分片。删除 Agent 时向下清理其对话和附件，但不向上删除绑定的场景能力。场景 Definition/Release 只保存数据端口契约而不保存验证批次，因此该删除不会破坏已经发布的能力。

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

部署时必须运行 `alembic heads` 与 `alembic current`，从当前迁移目录读取 single head 并核对目标库，不能把本文的 revision 记录代替事实。`20260904_20` 只依据不可变 Snapshot 中存在但缺少不可变来源证明的 catalog-derived 依赖保守撤回历史 Release，再退役/禁用当前污染映射、端口和绑定；它不 JOIN 可变目录行，保留 Snapshot，且 downgrade 主动拒绝。`20260904_21` 在发现既有 Turn/Message/Conversation 或上传任务/资产版本跨归属时 fail closed，随后增加复合唯一键/外键和 Connector 结构轮廓字段。`20260904_22` 增加 `assistant_request_runs`、会话/消息复合归属约束和持久全局顾问 worker。

运行账号不得拥有超级用户、建库、建角色或 Schema DDL 权限。Revision 22 后，`ontology_app` 对 `agent_turn_runs`、`managed_upload_runs` 和 `assistant_request_runs` 只有 `SELECT/INSERT/UPDATE`，对 append-only `agent_turn_events` 只有 `SELECT/INSERT`，不具备这些持久台账的 `DELETE` 权限。Schema 迁移只能由迁移 owner/admin 执行；Redis 不可用时，业务正确性必须仍由 PostgreSQL 和 MinIO 提供。
