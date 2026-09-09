# 本体业务契约 API

本轮只调整服务端。原有页面保持不变；下列新增配置由现有场景定义 API 提交。它们不是新的执行入口：正式调用仍解析人工启用的不可变 Release，并经 CapabilityInvoker 执行。

## 对象身份与状态

手工实例以对象类型及其声明的主键值确定唯一业务身份。重复创建、改键造成冲突时返回 409；没有主键的旧对象不会被猜测合并。受管导入实例沿用原有来源和版本身份，不能用手工创建接口伪装为导入实例或更换来源。已有手工事实时修改、删除或重命名主键需要显式数据迁移。

数值主键按数值规范化，`1` 与 `1.0`、`0` 与 `-0.0` 视为相同身份；原始属性内容不因主键哈希规范化而改写。

`EntityIn.state_policy` 声明生命周期策略，`state_property` 必须指向当前对象的枚举属性：

```json
{
  "state_property": "status",
  "state_policy": {
    "enabled": true,
    "initial_states": ["draft"],
    "transitions": [{"from_state": "draft", "to_state": "approved"}]
  }
}
```

未启用策略时不替业务指定状态机。启用后，手工实例创建与状态变化受服务端和 PostgreSQL 约束。完整对象更新仍需同时提交原有必需字段；省略 `state_policy` 的旧编辑请求保留当前策略。

实例创建、更新和对象详情响应中的 `integrity` 是服务端对当前定义的检查结果：`valid`、`incomplete` 或 `invalid`，并包含 `issues`。最小关联数量尚未满足时可以先建立对象，结果为 `incomplete`；建立关联后重新读取详情会重新检查。该结果不等于人工确认业务事实真实，也不等于发布 readiness。

## 规则输入

`RuleIn.input_validation` 支持：

- `object`：遵循关联对象属性的类型、枚举及声明约束。新建和迁移后的当前规则使用此模式。
- `record`：保留原有类型及枚举校验，可用于明确选择旧输入语义的规则。

规则只要求条件中实际消费的字段，不要求提交整个对象。未包含此字段的历史发布保持旧行为，编辑请求省略字段时保留当前模式。试算、工作流规则节点与统一能力调用采用同一规则属性契约。

## 工作流本体绑定

在 `WorkflowIn.trigger_config.ontology_contract` 中配置：

```json
{
  "version": 1,
  "entity_ids": ["<当前场景对象类型 ID>"],
  "input_bindings": [{
    "path": "request",
    "entity_id": "<当前场景对象类型 ID>",
    "many": false,
    "partial": false
  }],
  "output_node_id": "analysis",
  "output_schema": {
    "type": "object",
    "properties": {"accepted": {"type": "boolean"}},
    "required": ["accepted"],
    "additionalProperties": false
  }
}
```

输入绑定按对象属性的稳定 `api_name` 校验，路径可用点号访问嵌套对象。`many` 表示最多 1000 项的对象集合；`partial` 显式表示仅检查已提供属性，允许包含其他上下文字段，不宣称它是完整对象。完整绑定要求主键和必填属性，并拒绝未声明字段。若同时声明 `input_schema`，它与本体绑定取交集，不会被绑定配置放宽。

大模型接收当前已解析定义中的对象含义、属性契约、关联关系及状态策略。仅注入选定对象范围，不补入实例、建模资料或客户数据；属性读取仍受权限约束。上下文超过 96 KB 时拒绝执行。

结果节点必须是当前图中的 `llm` 或 `end`。大模型输出须能解析为满足 `output_schema` 的 JSON；结束节点可用 `data.output` 表达结构化结果，未配置时仍返回原来的 `summary`。契约校验失败或流程未到达指定结果节点时不得成功。声明本体契约且包含大模型节点的启用工作流必须配置业务输出。草稿可以暂缺输出配置，但不能据此执行。

输出 Schema 使用平台现有的有界、无外部引用 JSON Schema 子集。类型正确只能证明结果满足结构约束，不能证明模型输出的业务事实真实。没有显式声明本体契约的历史工作流保留原行为；本轮不会自动替既有工作流选择对象或编造业务输出要求。

## 查询角色

语义查询中的 `base_entity`、`related_entities`、`group_by`、`aggregations` 和 `sort` 可声明 `role`（1 至 80 位小写字母、数字及下划线，小写字母开头）。同一对象类型多次出现时使用不同角色；后续排序、分组、聚合必须消除角色歧义。

```json
{
  "base_entity": {"entity_name": "Work item", "role": "current"},
  "base_properties": ["item_id"],
  "related_entities": [{
    "entity_name": "Work item",
    "role": "parent",
    "relation_id": "<已配置受管映射的关系 ID>",
    "direction": "outgoing",
    "properties": ["item_id"]
  }]
}
```

`outgoing` 从主对象沿关系指向目标；`incoming` 反向。自关联必须显式指定关系和方向，且该关系有受管映射；不能借角色传 SQL 或物理字段。关联对象列带角色前缀，SQL 别名仍由服务端分配。

## 边界与兼容

模型 DTO 拒绝未支持的语义字段（例如 `unit`、`subclass_of`），不再静默丢弃；仅兼容原编辑器回传的已知只读元数据。单位等业务含义仍可用显式属性建模，不代表平台实现了单位换算或类公理推理。

结构评估计入关系约束，报告描述差异，并明确返回 `business_meaning_verified=false`。结构 F1 分数不证明业务需求满足。本轮没有增加 OWL 类继承推理，也没有自动改写、启用或发布现有业务场景。
