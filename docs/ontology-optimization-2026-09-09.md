# 本体业务语义与场景工作台优化

状态：实施中。依据：本轮用户批准按验证报告整体优化，并以真实前端端到端操作作为主要验收方式。

范围修订：用户明确要求停止前端优化、恢复原来的界面。本轮全部前端改动已撤回，OO-07 取消；OO-01 至 OO-06 仅保留服务端修复与契约。状态策略、工作流本体绑定和查询角色本轮作为 API 契约交付，不新增页面配置入口，不声称完成前端产品闭环。已有页面负责验证对象编辑、实例和规则路径；仅 API 可达部分使用必要的真实 PG 和执行器验证。

## 需求账本

| ID | 目标及验收 | 影响范围 | 保持不变 / 非目标 | 验证 |
| --- | --- | --- | --- | --- |
| OO-01 | 对象主键在明确的数据范围内保证唯一，重复手工创建或改键被拒绝；已有记录不擅自合并 | 实例写入服务、ORM、Alembic、实例表单 | 导入数据的版本/来源身份、历史数据与租户隔离不变 | 浏览器创建/编辑冲突；必要的真实 PG 并发与迁移验证 |
| OO-02 | 规则明确选择是否遵循对象属性约束；选用对象约束时负数、长度等违规输入被拒绝 | 共享属性 Schema、规则 DTO/ORM/Provider、规则表单 | 规则仅提交所需字段；历史发布缺省保留已有输入语义 | 浏览器规则试算；共享内核必要回归 |
| OO-03 | 明确实例的关系完整性；可配置初始状态与允许的状态迁移，违规写入被拒绝 | 生命周期/完整性服务、对象 DTO/ORM/迁移、对象/实例编辑器 | 默认不替业务指定状态机；允许先建实例再补关系，不声称不完整实例已经有效 | 浏览器状态变更、关系补齐与刷新 |
| OO-04 | 工作流可显式绑定本体上下文和输入对象，可声明输出契约；不合规输入/输出不能成功 | 工作流契约服务、编排器/readiness/发布快照消费者、工作流编辑器 | 不注入客户数据、不自动替旧发布改义、不复制执行内核 | 浏览器配置/执行/查看失败与成功证据；必要执行器回归 |
| OO-05 | 同类型对象可按不同角色进行受管关系查询，引用歧义明确拒绝 | 业务查询契约/编译、语义 Provider、查询入口 | SQL/物理字段不可由调用方指定；不增加无界查询 | 真实页面查询入口与必要 Parquet 查询验证 |
| OO-06 | 模型评估覆盖关系约束并明确结构评估边界；未知模型字段不被静默丢弃 | DTO、编译器、评估器及消费者 | 不宣称实现完整 OWL 推理；明确拒绝不支持的类公理 | 相关编辑流程与必要契约回归 |
| OO-07（取消） | 前端恢复原样；不继续设计导航、概览或新控件 | 前端本轮改动全部撤回 | 原路由、Tab、表单、权限与交互不变 | git diff frontend 为空；实际浏览器刷新确认 |

影响图：业务需求 → 场景工作台/编辑表单 → API/DTO → 共享本体契约与实例写入、工作流、语义查询 → PG 约束与发布定义 → 统一能力执行 → 页面结果与审计。

仅扩展必要的字段和服务，不对遗留巨型文件进行整体重写。使用现有 Vue、Element Plus、图谱与结构化 Schema 编辑器。前端是服务器事实的展示和操作入口，不自行裁决权限、完整性或执行成功。

## 验收与边界

主要验收通过实际浏览器页面执行。数据库迁移、唯一约束竞争、发布兼容、共享执行器以及 TypeScript 构建无法仅凭页面点击证明，按必要性补充专项验证；不会以脚本通过替代浏览器验收。

初始工作区有上一轮生成的诊断测试/报告，以及用户既有的 `docs/scenario-capability-platform-optimization-analysis.md`，均保留。上一轮缺口观察在修复后转为正确行为回归，不删除失败证据。生产配置及密钥不直接读取或输出。

## 进度

- 已完成：OO-01 至 OO-06 的服务端实现、API 说明和必要迁移；OO-07 按用户要求撤回，前端 diff 为空。
- 已完成：原有页面只读浏览和刷新，恢复原 Tab/图谱；未修改既有业务场景定义或创建验收记录。
- 待授权：原页面写入端到端。自动审批拒绝创建验收场景，已明确请求允许创建、仅在该场景模拟写入、验证后退役；尚未收到该项授权，不绕过审批改用 API 写业务数据。
- 运行状态补查：迁移后的自动重载一度未完成，HTTP 健康检查超时，页面返回登录入口。触发后端重新加载后 `/api/health` 返回三项依赖正常，原登录会话可再次打开“项目全生命周期协同”；最终读取 DOM 确认原十项 Tab 恢复，无须再次登录。该只读确认不等同写入端到端验收。

## 实现与验证记录

1. 手工主键唯一性由 PG 唯一约束与触发器保证，包括两个并发事务争抢同一主键。等价数值 `1` / `1.0` 的失败复现促成独立 revision 30，未改写已应用 revision 29。
2. 规则对象模式投影属性约束，并在执行时复用属性值校验。旧发布缺省保留原模式，原表单回传已知只读元数据可以保存，省略新增字段不抹掉当前策略。
3. 当前对象详情给出服务端完整性；允许先建立关系不完整的对象，补齐关联后再检查。状态策略在服务端及 PG 执行，不把枚举误称为状态机。
4. 显式工作流本体绑定进入输入、LLM 上下文、输出及发布依赖校验。最终输出契约只作用于指定结果节点，中间分析节点不会被强制要求相同输出。输出未产生或校验失败不能成功；保留执行证据。
5. 语义查询支持同类型对象不同 role，通过明确的受管 relation_id 与方向关联；分组、排序和聚合消除角色歧义。查询仍只使用受管映射和参数化 SQL。
6. 结构评估计入关系约束、报告描述差异，明确不证明业务语义真实。未知本体语义字段拒绝，未实现完整 OWL 或自动单位换算。

必要验证使用 Python 3.12.0、pytest 8.4.2。命令均使用项目环境解释器，测试工具目录通过 PYTHONPATH 注入；未修改依赖清单。

| 命令 / 操作 | 实际结果 |
| --- | --- |
| `python -m pytest backend/tests -q --tb=short` | 1061 passed、23 skipped、1 failed，耗时 37:09。唯一失败是 `test_relation_data_mappings.py` 中旧序列化替身未接收新增参数；保留原断言并适配该明确参数后重跑对应文件通过。未将这次全量结果写成全绿 |
| `python -m pytest backend/tests/test_ontology_business_validation.py backend/tests/test_scenario_model_compiler.py backend/tests/test_scenario_releases.py -q --tb=short` | 当轮 77 passed |
| `python -m pytest backend/tests/test_ontology_business_validation.py backend/tests/test_scenario_model_evaluator.py backend/tests/test_capability_release_contract.py -q --tb=short` | 当轮 36 passed |
| `python -m pytest backend/tests/test_ontology_business_validation.py backend/tests/test_relation_data_mappings.py -q --tb=short` | 最终数值主键修复后 33 passed，含 17 项本体业务用例 |
| `python -m pytest backend/tests/test_ontology_business_validation.py -k semantic_query_executes -q --tb=short` | 补强真实 Parquet / DuckDB 的同类型对象正向及反向角色关联后 1 passed、16 deselected；结果分别验证子项到父项及父项到子项，而非只检查 SQL 字符串 |
| `python -m alembic -c backend/alembic.ini -x use_admin=1 upgrade head` | 当前工作环境已迁移到 20260909_30 |
| `python -m alembic -c backend/alembic.ini heads` | single head：20260909_30 |
| `python backend/scripts/verify_alembic_roundtrip.py` | 自建隔离数据库往返通过，最终恢复 20260909_30 并清理 |
| `python backend/scripts/verify_postgresql_runtime.py` | 当前 schema、最小运行角色权限、MinIO 和 Redis 健康检查通过 |
| `git diff --check`；`git diff --stat -- frontend` | 前者通过，后者无内容差异。未提交代码 |
| 浏览器 `http://127.0.0.1:5173/scenarios/...?...stage=ontology` | 已登录查看项目协同场景，原 Tab 与图谱恢复。8001 后端确认启用自动重载 |

前端改动撤回前曾通过 TypeScript 构建检查；撤回后不把它算作最终前端改动验收，也未保留新控件。各专项存在重叠，不累加为测试总数。

未运行 / 未完成：真实 LLM 业务正确性、客户系统副作用、完整浏览器写入链路，以及受管文件上传到多角色查询的完整页面链路。零数据和确定性执行路径使用真实执行器及隔离 PG，外部 LLM 传输使用替身。相关服务端契约见 `ontology-business-contract-api.md`。
