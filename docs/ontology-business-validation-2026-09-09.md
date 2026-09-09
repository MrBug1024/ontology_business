# 本体对业务能力的作用与表达边界验证

日期：2026-09-09。状态：本轮验证完成；本文件是本次验证记录，不改变平台架构约定。

## 结论

1. **当前本体已经为部分业务能力提供了真实服务。** 受控修改规则、属性类型和关系约束，会改变业务判断、输入是否被接受及关系是否能够写入；语义查询确实可以按业务属性执行到真实 Parquet/DuckDB 并返回结果。本体并非纯展示。
2. **当前平台尚不能承诺任意业务场景都能被完整、明确并一致执行地描述。** 已复现业务主键不能防止重复实例、属性约束跨入口继承不完整等问题；状态枚举、类公理、同类型多角色查询也有明确能力边界。具有行业无关的扩展框架，不等于已有任意场景的完整表达和执行能力。
3. **仓库“项目全生命周期协同”的两条参考工作流，尚不能证明其本体是业务分析的执行依据。** 在隔离库重建参考的 8 类对象、6 类关系，再分别运行原始两条工作流：修改本体业务含义及属性类型，LLM 收到的消息没有变化；模拟 LLM 返回与业务契约无关的 JSON，工作流仍记为成功。离线包存在独立业务校验，但它的通过不能证明平台工作流消费了平台本体。

因此，问题同时涉及语义契约、执行依赖与产品呈现。重命名或合并 Tab 无法单独解决本轮发现的问题；也没有证据支持推倒已有全部实现。

## 需求账本

| ID | 原文目标 | 非目标 | 可观察验收 | 影响层/文件 | 保持不变项 | 测试证据 |
| --- | --- | --- | --- | --- | --- | --- |
| OV-01 | 验证本体是否为业务场景能力提供服务 | 不以模型/字段存在或哈希变化代替业务效果 | 同一业务输入下，相关定义的受控变化导致可解释的结果变化；检查对象/属性/关系/规则的真实消费者 | 本体、语义查询、规则、统一执行；新增 `backend/tests/test_ontology_business_validation.py` | 生产代码、数据边界、权限及协议不变 | 4 项正向行为实验，见 P1-P4 |
| OV-02 | 验证是否能够对任意业务场景描述清楚 | 不以单个成功案例证明任意场景；不新增建模功能 | 对基础结构、身份、约束、关系、规则、生命周期及自然语言含义分别给出支持证据和反例 | DTO、编译器、验证服务、已有场景参考 | 不放宽校验，不把描述文本等同可执行契约 | 9 项缺口/边界观察用例及既有类公理拒绝测试，见 G1-G8 |
| OV-03 | 验证现有业务场景的本体与执行依赖 | 不把历史文档的通过声明当成本轮证据 | 检查仓库场景参考中的对象/关系与工作流输入、提示词、节点及输出的依赖；区分平台内与离线包 | `examples/project-lifecycle-collaboration/` 只读；工作流服务及测试 | 保留既有定义和用户未提交文档 | 隔离重建参考模型；两条原始流程的消息与结果对照；离线 17 项测试 |
| OV-04 | 提供可复现、明确边界的结论 | 不顺手修复缺口或改前端 | 记录实际命令、通过/失败/跳过、真实 PG 与替代测试区别、未验证项及后续建议 | 本报告、验证测试 | 不读出密钥、不调用真实 LLM/邮件/MCP、不改当前业务库 | 共 145 项最终通过；依赖健康及真实迁移检查；未验证项列于末尾 |

## 影响图与执行边界

用户目标 → 本体/场景 DTO 与编译入口 → 本体校验、语义查询、规则与工作流 → 统一 CapabilityInvoker → 受管输入、发布快照与结果 → 验证测试和本报告。

只新增验证用例和报告，不修改生产代码、迁移、前端、依赖清单或锁文件。数据库写入只允许发生在 `tests.access_postgresql.isolated_access_database` 自行创建并清理的隔离 PostgreSQL 数据库。现有单元测试的 SQLite/Mock 结果单独标注，不能作为真实 PostgreSQL 并发或持久约束的证明。

初始 Git 状态只有用户既有未跟踪文件 `docs/scenario-capability-platform-optimization-analysis.md`，保留不动。现有 Python 环境为 3.12.0；缺少 pytest，验证工具安装到系统临时目录，不更改应用环境和项目依赖。

## 实验设计与证据强度

新增测试：[test_ontology_business_validation.py](../backend/tests/test_ontology_business_validation.py)。为保持仓库内链接可移动，下文代码位置使用仓库相对路径和明确行号；它们是定位证据，不是架构权威。

- 13 项用例：4 项正向行为验证，9 项缺口或表达边界观察。名称含 `gap` 的测试通过，表示成功复现观察，不表示相应业务要求验收通过。修复后应该更新为要求正确行为的回归测试。
- 12 项使用自建、自动清理的真实 PostgreSQL 数据库；通过 Alembic 迁移到执行时实际 head，随后用运行角色操作。1 项仅验证 Pydantic DTO 行为。
- 实例与关系实验直接调用路由函数，使用隔离租户、用户和会话身份，保留服务端权限检查；没有模拟 HTTP 登录、Cookie 或网络传输。
- 规则实验使用真实 `resolve_authoring/resolve_active -> build_resolved_deployment -> CapabilityInvoker -> rule provider -> evaluate_rule`，不替换规则执行器或发布快照。
- 查询实验使用真实本体属性解析、权限检查、查询编译、SQL 只读验证、参数绑定、Parquet/DuckDB 和结果投影；目录加载及对象文件获取使用受控测试替身。它证明语义查询实际计算，不是完整 MinIO/Catalog 导入链路验收。
- 工作流实验保留原始节点、边、提示词，运行真实 readiness、权限、模板渲染、JSON 解析和 PG 审计。只替换 LLM 配置解析及 LLM 网络调用，故能确定比较消息依赖，不能评价真实模型的分析质量。
- 现有场景依据仓库参考重建，不读取或修改当前业务库中的客户场景，也不把历史文档里的部署状态当成现状。

## 已证明的作用

| 编号 | 同一输入下的对照 | 实际结果 | 证明了什么 |
| --- | --- | --- | --- |
| P1 | 输入 `amount=15`；规则阈值由 `>10` 改为 `>20`；再读取原启用发布 | 当前定义由命中并返回审核意图，变为不命中；原发布仍命中；无副作用执行 | 规则是可执行业务定义，发布快照能够保持原业务行为 |
| P2 | 输入 `amount=15.5`；属性从 integer 改为 number，其他规则含义保持一致 | 原输入被 `input_schema_invalid` 拒绝；修改后规则执行成功并命中 | 规则输入契约确实消费关联对象的属性类型 |
| P3 | 已存在 A→B，尝试写入 B→A；关系无环约束由启用改为关闭 | 启用时返回 409；关闭后相同关系写入成功 | 关系约束会决定哪些业务关联事实允许写入 |
| P4 | 两条真实 Parquet 记录 amount 为 5、25；语义查询 `amount > 10.5`；属性 integer 改为 number | integer 时拒绝小数过滤值；number 时真实查询返回 `case-b, amount=25`，输出为业务属性名 | 本体属性参与数据解释和实际查询，不只是图谱上的字段 |

定位：新增测试 P1/P2/P3/P4 分别位于 123、147、207、241 行。关键实现包括 `builtin_capability_providers.py:141`、`ontology_service.py:507`、`business_query_service.py:697` 与 `business_query_service.py:1030`。

## 已复现的问题与边界

| 编号 | 实际观察 | 对业务描述的影响 | 性质 |
| --- | --- | --- | --- |
| G1 | amount 声明 `minimum=0`；实例写入拒绝 -1，但统一规则调用接受 -1 并返回不命中；规则输入 Schema 只有 number | “对象属性的约束”尚不能自动理解为所有关联能力都执行的统一约束 | 已复现的跨入口契约差异 |
| G2 | case_id 标记为 key；通过实例创建入口两次提交相同值，PG 持久保存两个不同 UUID 的对象 | 仅声明业务主键，还不能保证“这两条记录是不是同一个业务对象”的一致答案 | 已复现的身份完整性缺口；不是 UUID 重复问题 |
| G3 | 关系要求每个源对象至少关联 1 个目标；创建一个无关联源实例仍成功 | 最小关系基数不等于完整对象在创建时必然满足该关系 | 完整性检查边界；若允许草稿暂不完整，需要明确何时才算完整有效 |
| G4 | 状态枚举包含 draft/approved/closed；实例从 closed 更新回 draft 成功 | 状态取值集合不能独立描述允许的迁移、前置条件及迁移责任 | 能力边界，不据此认定所有场景都应禁止回退 |
| G5 | 本体可以建立 Case→Case 依赖关系；业务查询将同类型对象作为第二个角色关联时明确拒绝 | 图谱能够表达的关系，并不代表当前通用查询能按两个角色展开查询 | 查询能力边界，不代表本体不能表示该关系 |
| G6 | 修改业务描述，并将关系约束改为 `source_max_cardinality=1`；评估器 micro F1 和 constraint F1 仍为 1 | 当前结构匹配分数不能作为业务含义清晰或关系约束完整的验收证明 | 评估覆盖边界 |
| G7 | 在 PropertyIn 中传入 unit、EntityIn 中传入 subclass_of，不报错但输出模型丢弃这些字段 | 不能把未定义的输入字段误认为平台已经承载该语义 | DTO 接收边界；可用显式属性表达币种/分类，不等于已提供单位运算或继承推理 |
| G8 | 两条参考 LLM 流程在本体修改前后消息不变；返回 `{"unrelated":true}` 时都记为 success | 工作流成功尚不证明分析遵守本体定义，也不证明业务输出符合提示词中的契约 | 已复现的参考流程依赖与输出验收缺口，共 2 项测试 |

G1 需要明确规则的输入语义：某些规则应允许评估待清洗原始记录；另一些规则应只接受满足对象约束的记录。当前实验说明，不能仅凭规则关联了对象类型，就宣称其继承了对象的全部约束。它不证明所有规则都应直接复用实例的全部必填要求。

G3 当前最小基数确实用于关系删除检查，但未约束无关系对象的创建。G4 当前枚举和有效期校验存在，业务迁移可以另行编排；本轮没有发现单凭状态属性就能声明并执行完整状态机的证据。

此外，原有 `test_relation_constraints.py:441` 已执行并通过：类互斥公理得到 `unsupported_class_axiom`，没有被冒充为普通记录规则。这是有价值的明确拒绝，也意味着“六类组件都存在”不能推导出类等价、互斥、继承等语义均已实现。

定位：G1-G7 新增测试分别为 160、175、185、198、271、308、375 行；G8 为 325 行的两组参数。对应生产实现为 `ontology_service.py:961`、`routers/scenarios.py:3623`、`ontology_service.py:618`、`business_query_service.py:734`、`scenario_model_evaluator.py:109`、`schemas.py:132`、`workflow_service.py:2621`。

## 现有参考场景的判断

对象和关系的文字说明能够帮助人理解“项目上下文、成员身份、证据、需求、评审与交付”等概念，场景描述也明确了第三方持有事实与权限、本平台输出建议的边界。这些内容有实际建模价值。

但当前两条参考流程是 start → llm → llm → end。运行时的主要业务依据来自每次提供的 `params.context` 和各节点手写提示词。测试分别重建参考中的 8 类对象、6 类关系及对应流程，使用仓库 kickoff-context 示例，在修改“项目上下文”的定义、“上下文版本”的类型与约束以及关系描述后，捕获到的两轮 LLM 消息和步骤结果完全一致。没有通过仅比较 definition_hash 得出结论。

独立离线包的 `contracts.py` 从自身 `references/contract.json` 读取 Schema，`validate_analysis.py` 对项目版本、证据引用、角色匹配、已保存评审内容及依赖环等做确定性校验。本轮该包 17 项测试通过。上述校验有价值，但两条平台工作流并未调用这些脚本；离线包也没有读取平台本体定义。

应把“客户的身份、事实与状态由第三方掌握”与“平台的能力是否受版本化业务语义约束”分开。前者符合现有架构边界；后者仍需要明确的契约绑定和实际验收。并非所有能力都必须读全图谱，但声称依赖某项业务定义的能力，应当能够指出消费位置，并通过相关定义变化时的行为对照。

参考：[场景定义](../examples/project-lifecycle-collaboration/references/scenario-definition.json)、[离线契约校验](../examples/project-lifecycle-collaboration/scripts/contracts.py)、[离线分析校验](../examples/project-lifecycle-collaboration/scripts/validate_analysis.py)。

## “描述清楚”的可观察标准

建议后续按一个具体场景逐项验收，不能用对象数量或模型得分代替这些答案：

| 业务问题 | 应有的模型或契约 | 验收方式 |
| --- | --- | --- |
| 这是什么，同一个对象如何识别？ | 定义、边界、稳定业务身份及身份作用域 | 同一身份不会产生无法解释的重复对象；同名不同身份不被合并 |
| 属性和关系具体是什么意思？ | 类型、单位/口径、端点角色、基数、事实来源及时间含义 | 不同口径不被直接混算；关系方向和角色可以被查询与解释 |
| 哪些业务状态或事实有效？ | 不变量、约束适用范围、允许不完整的阶段 | 非法输入在应拒绝的入口一致拒绝，例外明确可查 |
| 为什么会得出这个判断？ | 有版本的规则/函数与输入证据 | 同一版本同一输入可以解释；受控修改规则会改变相关判断 |
| 谁可以在何时做什么，做后会怎样？ | Action 的身份、前后条件、状态变化与副作用契约 | 不满足条件则拒绝；满足条件的结果及审计可核验 |
| 本体如何帮助完成场景任务？ | 需求→对象/属性/关系→规则/能力→输入输出→执行证据的依赖 | 每项关键业务要求能找到运行中的消费者和成功/拒绝用例 |

当前平台对这些问题的覆盖不均衡。应先让一个代表性场景形成可验证的完整链路，再用不同场景检验通用性；没有理由假定一次验证可以证明对任意业务场景都充分。

## 实际执行记录

环境：应用 Python 3.12.0，pytest 8.4.2 安装于系统临时目录。没有更新应用依赖、lockfile 或生产配置。Alembic 执行时实际 single head 为 `20260908_28`，这里仅记录观测值，复现仍使用 `head`。

```powershell
# 从仓库根执行；本轮使用的已存在应用解释器
$verificationPython = 'D:\miniconda3\envs\ontology_platform_env\python.exe'
$env:PYTHONPATH="$env:TEMP\codex-ontology-verification-tools;E:\work\test\backend"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'

& $verificationPython -X utf8 -m pytest `
  backend/tests/test_ontology_runtime_metadata.py `
  backend/tests/test_ontology_entity_lifecycle.py `
  backend/tests/test_relation_constraints.py `
  backend/tests/test_semantic_mapping_contract_service.py `
  backend/tests/test_semantic_audit_rule_service.py `
  backend/tests/test_builtin_capability_providers.py `
  backend/tests/test_capability_protocol_consistency.py `
  backend/tests/test_scenario_model_evaluator.py `
  backend/tests/test_scenario_model_compiler_regressions.py `
  backend/tests/test_runtime_definition_deep_freeze.py `
  backend/tests/test_capability_release_contract.py -q --tb=short
# 105 passed in 47.87s

& $verificationPython -X utf8 -m pytest backend/tests/test_catalog_api.py -q --tb=short
# 10 passed in 13.74s

& $verificationPython -X utf8 -m pytest examples/project-lifecycle-collaboration/tests/test_contracts.py -q --tb=short
# 17 passed in 1.24s

$env:RUN_ONTOLOGY_BUSINESS_POSTGRESQL_TESTS='1'
& $verificationPython -X utf8 -m pytest backend/tests/test_ontology_business_validation.py -q --tb=short
# 最终版本：13 passed in 16.49s；隔离库迁移与清理由 fixture 完成

& $verificationPython -m alembic -c backend/alembic.ini heads
# 20260908_28 (head)，单一 head

& $verificationPython -X utf8 backend/scripts/verify_postgresql_runtime.py
# 验证前后均通过：PG schema current、运行角色无 DDL 权限、受治理函数可执行；MinIO/Redis healthy

git diff --check
```

新增测试编写过程曾因误用模型类名在收集阶段失败，修正为 `RelationInstance`；首次行为运行 9 过、1 失败，是测试把输入校验异常误当作失败 Receipt，随后按既有错误契约断言 `CapabilityInvocationError(input_schema_invalid)`。这些均为验证代码修正，未修改生产行为或放宽其校验。扩充后的 13 项先以 20.39s 通过；最终将类型变更对照收紧为两种合法数值类型后，再次全部通过。

收尾只读检查：`pg_database` 中 `ontology_access_verify_%` 隔离验证库剩余数量为 0。`git diff --check` 无错误，新增两份文件无行尾空白，文档相对文件链接均存在。Git 最终仅新增本报告和验证测试，另有开始时已存在的用户未跟踪分析文档；没有修改生产文件。

## 未运行项与结论边界

- 未运行全量后端、前端测试/构建或浏览器 E2E：本次只新增诊断测试和报告，没有修改生产实现或 UI，不将本轮结果视为整个平台产品验收。
- 未调用真实 LLM、第三方 MCP、邮件或其他副作用系统；没有检验真实 AI 分析质量、正式对外工作流调用或客户系统落地效果。
- 未运行实际 MinIO 上传到语义 Provider 的完整链路；运行时健康不等于该业务链路通过。已有 Catalog 测试对查询结果使用替身，新查询实验明确补了真实 Parquet/DuckDB 计算，但保留目录/对象获取边界替身。
- 新增 PG 实验验证顺序写入和实际持久约束，未做并发压力、故障恢复或多协议全路径等价性验证。原有协议一致性测试的通过不扩大此结论。
- 未审查所有现存场景，不把仓库参考等同当前已启用发布；没有以一个场景替代“任意场景”的证明。

## 后续顺序建议

先围绕一个代表性场景固定业务问题及验收，再闭合本轮确认的关键缺口：明确业务身份、约束的执行范围、本体与能力的显式依赖，以及业务输出契约和证据。使“修改相关定义会影响相应能力，修改无关定义不会干扰它”成为验收要求。

在此基础上调整前端，将对象/属性/关系、约束、规则与行为呈现为场景定义的不同视角，把实例事实、输入连接、发布和执行结果的边界显式呈现。当前 `ScenarioDetail.vue:49` 将对象图单独命名“本体模型”，与实例、规则等平级，确实可能使用户缩小对本体边界的理解；这是代码层的信息结构观察，本轮未进行浏览器可用性测试。

## 后续修复记录

本文件保留验证时的历史观察。后续实现与实际检查记录见 [本体优化记录](ontology-optimization-2026-09-09.md)，新增服务端契约见 [API 说明](ontology-business-contract-api.md)。用户随后明确要求停止前端优化并恢复原界面，本轮前端改动已撤回；该要求优先于上述顺序建议。原诊断用例已将修复的缺口改为行为回归，未显式绑定契约的历史工作流仍保留旧语义，不宣称已经自动完成场景改造。
