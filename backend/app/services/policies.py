"""平台级安全与结构校验策略。

这些策略只描述平台能力边界，不包含任何行业或业务场景语义。
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any


class PolicyViolation(ValueError):
    """请求违反平台执行策略。"""


_READ_START_RE = re.compile(r"^(select|with|explain)\b", re.IGNORECASE)
_WRITE_RE = re.compile(
    r"\b(insert|update|delete|replace|merge|upsert|alter|drop|create|truncate|attach|detach|vacuum|reindex|grant|revoke)\b",
    re.IGNORECASE,
)
_SELECT_SIDE_EFFECT_RE = re.compile(
    r"\b(?:nextval|setval|set_config|pg_sleep|pg_notify|pg_advisory_(?:xact_)?(?:lock|unlock)|"
    r"pg_try_advisory_(?:xact_)?lock|pg_terminate_backend|pg_cancel_backend|"
    r"pg_reload_conf|pg_rotate_logfile|pg_create_restore_point|pg_switch_wal|"
    r"pg_wal_replay_(?:pause|resume)|pg_promote|lo_(?:import|export)|dblink_exec)\s*\(",
    re.IGNORECASE,
)
_QUOTED_SELECT_SIDE_EFFECT_RE = re.compile(
    r'"(?:nextval|setval|set_config|pg_sleep|pg_notify|pg_advisory_(?:xact_)?(?:lock|unlock)|'
    r'pg_try_advisory_(?:xact_)?lock|pg_terminate_backend|pg_cancel_backend|'
    r'pg_reload_conf|pg_rotate_logfile|pg_create_restore_point|pg_switch_wal|'
    r'pg_wal_replay_(?:pause|resume)|pg_promote|lo_(?:import|export)|dblink_exec)"\s*\(',
    re.IGNORECASE,
)
_DUCKDB_UNSAFE_FUNCTION_RE = re.compile(
    r"\b(?:"
    r"sleep_ms|write_log|setseed|current_setting|current_query|"
    r"read_[A-Za-z0-9_]*|glob|query|query_table|which_secret|"
    r"postgres_scan|iceberg_scan|delta_scan|"
    r"arrow_scan|pandas_scan|parquet_(?:scan|metadata|schema|file_metadata)|"
    r"pragma_[A-Za-z0-9_]*|duckdb_[A-Za-z0-9_]*"
    r")\s*\(",
    re.IGNORECASE,
)
_DUCKDB_QUOTED_UNSAFE_FUNCTION_RE = re.compile(
    r'"(?:sleep_ms|write_log|setseed|current_setting|current_query|'
    r'read_[A-Za-z0-9_]*|glob|query|query_table|which_secret|'
    r'postgres_scan|iceberg_scan|delta_scan|'
    r'arrow_scan|pandas_scan|parquet_(?:scan|metadata|schema|file_metadata)|'
    r'pragma_[A-Za-z0-9_]*|duckdb_[A-Za-z0-9_]*)"\s*\(',
    re.IGNORECASE,
)
_AGENT_SQL_TOKEN_RE = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<string>'(?:''|[^'])*')"
    r'|(?P<quoted>"(?:""|[^"])*"|`(?:``|[^`])*`|\[(?:\]\]|[^\]])*\])'
    r"|(?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"|(?P<word>[^\W\d]\w*)"
    r"|(?P<op><=|>=|<>|!=|\|\||[-+*/%=<>,.()])",
    re.UNICODE,
)
_AGENT_SAFE_FUNCTIONS = {
    "abs", "avg", "cast", "ceil", "ceiling", "coalesce", "concat", "count",
    "date", "datetime", "floor", "greatest", "least", "length", "lower", "ltrim",
    "max", "min", "nullif", "round", "rtrim", "substr", "substring",
    "sum", "trim", "upper",
}
_AGENT_SQL_KEYWORDS = {
    "all", "and", "as", "asc", "between", "by", "case", "desc", "distinct", "else",
    "end", "escape", "false", "first", "from", "group", "having", "in", "is", "last",
    "like", "limit", "not", "null", "nulls", "offset", "or", "order", "select", "then",
    "true", "when", "where",
    # CAST target types. More elaborate dialect-specific type syntax is deliberately rejected.
    "bigint", "boolean", "char", "date", "decimal", "float", "integer", "numeric", "real",
    "smallint", "text", "timestamp", "varchar",
}
_AGENT_FORBIDDEN_SQL_WORDS = {
    "attach", "call", "copy", "create", "delete", "detach", "do", "drop", "except",
    "execute", "explain", "insert", "intersect", "into", "join", "merge", "pragma",
    "replace", "returning", "table", "truncate", "union", "update", "upsert", "values", "with",
}
_WORKFLOW_NODE_TYPES = {"start", "end", "action", "rule", "llm", "event", "http", "script", "approval"}


def _strip_sql_literals(sql: str) -> str:
    """将字符串和标识符字面量替换为空格，避免关键字扫描误伤内容。"""
    out: list[str] = []
    i = 0
    quote = ""
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.extend((" ", " "))
                    i += 2
                    continue
                quote = ""
            out.append(" ")
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(" ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def validate_read_only_sql(sql: str, *, dialect: str | None = None) -> str:
    """校验并规范化只读 SQL，拒绝多语句和写操作。"""
    if not isinstance(sql, str) or not sql.strip():
        raise PolicyViolation("SQL 不能为空")

    # 平台查询工具只允许一条语句；末尾分号可以保留给用户，但不参与执行。
    statement = sql.strip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if ";" in statement:
        raise PolicyViolation("只读查询不允许执行多条 SQL 语句")

    normalized_dialect = str(dialect or "").strip().casefold()
    if normalized_dialect == "dataset":
        normalized_dialect = "duckdb"

    # 去除注释后检查首关键字和 CTE 中的写操作。
    without_comments = re.sub(r"--[^\n]*|/\*.*?\*/", " ", statement, flags=re.S)
    if not _READ_START_RE.match(without_comments.lstrip()):
        raise PolicyViolation("只允许 SELECT、WITH 或 EXPLAIN 查询")
    if _WRITE_RE.search(_strip_sql_literals(without_comments)):
        raise PolicyViolation("只读查询中检测到禁止的写操作或 DDL 关键字")
    scanned = _strip_sql_literals(without_comments)
    if re.search(r"\binto\b", scanned, flags=re.IGNORECASE):
        raise PolicyViolation("只读查询不允许使用 SELECT INTO")
    if re.search(
        r"\bfor\s+(?:update|share|no\s+key\s+update|key\s+share)\b",
        scanned,
        flags=re.IGNORECASE,
    ):
        raise PolicyViolation("只读查询不允许获取行锁")
    if re.match(r"^\s*explain\b", scanned, flags=re.IGNORECASE) and re.search(
        r"\banalyze\b", scanned, flags=re.IGNORECASE
    ):
        raise PolicyViolation("只读查询不允许执行 EXPLAIN ANALYZE")
    if _SELECT_SIDE_EFFECT_RE.search(scanned) or _QUOTED_SELECT_SIDE_EFFECT_RE.search(
        without_comments
    ):
        raise PolicyViolation("只读查询不允许调用有副作用或阻塞型数据库函数")
    if normalized_dialect == "duckdb" and (
        _DUCKDB_UNSAFE_FUNCTION_RE.search(scanned)
        or _DUCKDB_QUOTED_UNSAFE_FUNCTION_RE.search(without_comments)
    ):
        raise PolicyViolation("数据集查询不允许外部扫描、系统或副作用函数")
    return statement


def _agent_sql_tokens(statement: str) -> list[tuple[str, str]]:
    """Tokenize the intentionally small SQL subset accepted from an Agent."""
    tokens: list[tuple[str, str]] = []
    position = 0
    for match in _AGENT_SQL_TOKEN_RE.finditer(statement):
        if match.start() != position:
            raise PolicyViolation("Agent SQL 包含不支持的语法")
        position = match.end()
        kind = match.lastgroup or ""
        if kind != "space":
            value = match.group()
            if kind == "quoted":
                # Identifier folding differs by connector (notably PostgreSQL:
                # unquoted names fold to lowercase while quoted names are exact).
                # Treating both as case-insensitive could authorize a different
                # physical table/column, so the Agent subset rejects quoted
                # identifiers instead of guessing dialect semantics.
                raise PolicyViolation("Agent SQL 不允许使用带引号的标识符")
            tokens.append((kind, value))
    if position != len(statement):
        raise PolicyViolation("Agent SQL 包含不支持的语法")
    return tokens


def validate_agent_sql_scope(
    sql: str,
    allowed_columns_by_table: dict[str, set[str]],
) -> str:
    """Validate Agent SQL against governed DataMapping tables and physical columns.

    This is deliberately a small, single-table SELECT grammar. Queries involving CTEs,
    subqueries, joins, set operations, unknown functions, or syntax we cannot prove safe
    are rejected instead of being passed to a connector.
    """
    statement = validate_read_only_sql(sql)
    if re.search(r"--|/\*|\*/", statement):
        raise PolicyViolation("Agent SQL 不允许包含注释")
    tokens = _agent_sql_tokens(statement)
    if not tokens or tokens[0][0] != "word" or tokens[0][1].casefold() != "select":
        raise PolicyViolation("Agent SQL 只允许单条 SELECT 查询")

    depths: list[int] = []
    depth = 0
    from_indexes: list[int] = []
    for index, (kind, value) in enumerate(tokens):
        depths.append(depth)
        if kind == "op" and value == "(":
            depth += 1
        elif kind == "op" and value == ")":
            depth -= 1
            if depth < 0:
                raise PolicyViolation("Agent SQL 括号不匹配")
        elif kind == "word":
            word = value.casefold()
            if word == "select" and index != 0:
                raise PolicyViolation("Agent SQL 不允许子查询")
            if word in _AGENT_FORBIDDEN_SQL_WORDS:
                raise PolicyViolation(f"Agent SQL 不允许使用 {value} 语法")
            if word == "from" and depth == 0:
                from_indexes.append(index)
    if depth != 0:
        raise PolicyViolation("Agent SQL 括号不匹配")
    if len(from_indexes) != 1:
        raise PolicyViolation("Agent SQL 必须且只能查询一个映射表")

    from_index = from_indexes[0]
    cursor = from_index + 1
    table_indexes: set[int] = set()
    table_parts: list[str] = []
    if cursor >= len(tokens) or tokens[cursor][0] not in {"word", "identifier"}:
        raise PolicyViolation("Agent SQL 的 FROM 必须引用映射表")
    while cursor < len(tokens):
        kind, value = tokens[cursor]
        if kind not in {"word", "identifier"}:
            break
        table_parts.append(value)
        table_indexes.add(cursor)
        cursor += 1
        if cursor + 1 < len(tokens) and tokens[cursor] == ("op", "."):
            table_indexes.add(cursor)
            cursor += 1
            continue
        break
    table_name = ".".join(table_parts)
    allowed_table_lookup: dict[str, set[str]] = {}
    for name, columns in allowed_columns_by_table.items():
        raw_name = str(name)
        # Our accepted SQL subset uses unquoted identifiers. PostgreSQL folds
        # those to lowercase, so a mixed-case governed name would identify a
        # different physical object unless quoted. Fail closed for that mapping.
        if not raw_name or raw_name != raw_name.casefold() or not columns:
            continue
        canonical_columns = {
            str(column)
            for column in columns
            if str(column) and str(column) == str(column).casefold()
        }
        if canonical_columns:
            allowed_table_lookup[raw_name] = canonical_columns
    allowed_columns = allowed_table_lookup.get(table_name.casefold())
    if not allowed_columns:
        raise PolicyViolation(f"Agent SQL 无权访问表 {table_name or '(空)'}")

    clause_words = {"where", "group", "having", "order", "limit", "offset"}
    alias = ""
    alias_index: int | None = None
    if cursor < len(tokens) and tokens[cursor][0] == "word" and tokens[cursor][1].casefold() == "as":
        cursor += 1
        if cursor >= len(tokens) or tokens[cursor][0] not in {"word", "identifier"}:
            raise PolicyViolation("Agent SQL 表别名无效")
        alias, alias_index = tokens[cursor][1], cursor
        cursor += 1
    elif (
        cursor < len(tokens)
        and tokens[cursor][0] in {"word", "identifier"}
        and tokens[cursor][1].casefold() not in clause_words
    ):
        alias, alias_index = tokens[cursor][1], cursor
        cursor += 1
    if cursor < len(tokens):
        if tokens[cursor][0] != "word" or tokens[cursor][1].casefold() not in clause_words:
            raise PolicyViolation("Agent SQL 只允许查询一个映射表，且不允许 JOIN")

    output_alias_indexes: set[int] = set()
    for index in range(1, from_index - 1):
        if (
            depths[index] == 0
            and tokens[index][0] == "word"
            and tokens[index][1].casefold() == "as"
            and tokens[index + 1][0] in {"word", "identifier"}
        ):
            output_alias_indexes.add(index + 1)

    qualified_indexes: set[int] = set()
    valid_qualifiers = {table_name.casefold(), table_parts[-1].casefold()}
    if alias:
        valid_qualifiers.add(alias.casefold())
    for index in range(len(tokens) - 2):
        if (
            tokens[index][0] in {"word", "identifier"}
            and tokens[index + 1] == ("op", ".")
            and tokens[index + 2][0] in {"word", "identifier"}
        ):
            if {index, index + 1, index + 2}.issubset(table_indexes):
                continue
            qualifier = tokens[index][1].casefold()
            column = tokens[index + 2][1].casefold()
            if qualifier not in valid_qualifiers or column not in allowed_columns:
                raise PolicyViolation("Agent SQL 引用了未映射的表或列")
            qualified_indexes.update({index, index + 1, index + 2})

    for index, (kind, value) in enumerate(tokens):
        if kind == "op" and value == "*":
            is_count_star = (
                index >= 2
                and index + 1 < len(tokens)
                and tokens[index - 1] == ("op", "(")
                and tokens[index - 2][0] == "word"
                and tokens[index - 2][1].casefold() == "count"
                and tokens[index + 1] == ("op", ")")
            )
            if not is_count_star:
                raise PolicyViolation("Agent SQL 不允许使用 * 读取未映射列")
        if kind not in {"word", "identifier"}:
            continue
        word = value.casefold()
        if index in table_indexes or index == alias_index or index in output_alias_indexes:
            continue
        if index in qualified_indexes:
            continue
        if index + 1 < len(tokens) and tokens[index + 1] == ("op", "("):
            if word not in _AGENT_SAFE_FUNCTIONS:
                raise PolicyViolation(f"Agent SQL 不允许调用函数 {value}")
            continue
        if kind == "word" and word in _AGENT_SQL_KEYWORDS:
            continue
        if word not in allowed_columns:
            raise PolicyViolation(f"Agent SQL 无权访问列 {value}")
    return statement


def _action_schema_root(schema: Any) -> dict[str, Any]:
    """兼容完整 JSON Schema 和早期的扁平字段 Schema。"""
    if schema in (None, {}):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    if not isinstance(schema, dict):
        raise PolicyViolation("Action 输入 Schema 必须是 JSON 对象")
    if "properties" in schema or "required" in schema or schema.get("type") == "object":
        root = dict(schema)
    else:
        root = {"type": "object", "properties": schema, "additionalProperties": False}
    if root.get("type", "object") != "object":
        raise PolicyViolation("Action 输入 Schema 顶层必须是 object")
    if not isinstance(root.get("properties", {}), dict):
        raise PolicyViolation("Action 输入 Schema 的 properties 必须是对象")
    return root


def _validate_action_value(path: str, value: Any, schema: dict[str, Any]) -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise PolicyViolation(f"参数 {path} 必须是枚举值: {schema['enum']}")

    expected = schema.get("type")
    if isinstance(expected, list):
        expected_types = expected
    elif expected:
        expected_types = [expected]
    else:
        expected_types = []
    if expected_types and not any(
        (kind == "string" and isinstance(value, str))
        or (kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
        or (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (kind == "boolean" and isinstance(value, bool))
        or (kind == "array" and isinstance(value, list))
        or (kind == "object" and isinstance(value, dict))
        or (kind == "null" and value is None)
        for kind in expected_types
    ):
        raise PolicyViolation(f"参数 {path} 类型错误，期望 {expected}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise PolicyViolation(f"参数 {path} 长度不能少于 {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise PolicyViolation(f"参数 {path} 长度不能超过 {schema['maxLength']}")
        if schema.get("pattern"):
            try:
                matched = re.search(str(schema["pattern"]), value)
            except re.error as exc:
                raise PolicyViolation(f"参数 {path} 的 pattern 无效") from exc
            if not matched:
                raise PolicyViolation(f"参数 {path} 不符合格式要求")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise PolicyViolation(f"参数 {path} 不能小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise PolicyViolation(f"参数 {path} 不能大于 {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise PolicyViolation(f"参数 {path} 至少需要 {schema['minItems']} 项")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise PolicyViolation(f"参数 {path} 最多允许 {schema['maxItems']} 项")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                _validate_action_value(f"{path}[{index}]", item, schema["items"])
    if isinstance(value, dict) and schema.get("properties"):
        properties = schema["properties"]
        required = set(schema.get("required", []))
        for key, child_schema in properties.items():
            if not isinstance(child_schema, dict):
                raise PolicyViolation(f"参数 {path}.{key} 的 Schema 无效")
            if key not in value:
                if key in required or child_schema.get("required") is True:
                    if "default" not in child_schema:
                        raise PolicyViolation(f"缺少必填参数: {path}.{key}")
                continue
            _validate_action_value(f"{path}.{key}", value[key], child_schema)
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise PolicyViolation(f"存在未声明参数: {path}.{sorted(unknown)[0]}")


def validate_action_params(schema: Any, params: Any) -> dict[str, Any]:
    """校验 Action 参数类型、必填项和约束，并返回可安全使用的参数副本。"""
    if not isinstance(params, dict):
        raise PolicyViolation("Action 参数必须是 JSON 对象")
    root = _action_schema_root(schema)
    normalized = dict(params)
    properties = root.get("properties", {})
    for key, child_schema in properties.items():
        if key not in normalized and isinstance(child_schema, dict) and "default" in child_schema:
            normalized[key] = child_schema["default"]
    _validate_action_value("params", normalized, root)
    return normalized


def validate_workflow_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """校验工作流 DAG 的节点、连线、可达性和分支完整性。"""
    if not nodes:
        raise PolicyViolation("工作流至少需要一个节点")

    node_map = {str(n.get("id")): n for n in nodes if n.get("id")}
    if len(node_map) != len([n for n in nodes if n.get("id")]):
        raise PolicyViolation("工作流节点 ID 必须唯一且不能为空")
    starts = [nid for nid, n in node_map.items() if n.get("type") == "start"]
    ends = [nid for nid, n in node_map.items() if n.get("type") == "end"]
    if len(starts) != 1:
        raise PolicyViolation("工作流必须且只能有一个开始节点")
    if len(ends) != 1:
        raise PolicyViolation("工作流必须且只能有一个结束节点")

    outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    incoming: dict[str, int] = defaultdict(int)
    for edge in edges:
        source, target = str(edge.get("source", "")), str(edge.get("target", ""))
        if source not in node_map or target not in node_map:
            raise PolicyViolation("工作流存在指向不存在节点的连线")
        if source == target:
            raise PolicyViolation("工作流不允许自环连线")
        label = str(edge.get("label", ""))
        outgoing[source].append((target, label))
        incoming[target] += 1

    for nid, node in node_map.items():
        if node.get("type") not in _WORKFLOW_NODE_TYPES:
            raise PolicyViolation(f"工作流包含不支持的节点类型: {node.get('type')}")
        if nid in starts and incoming.get(nid, 0) > 0:
            raise PolicyViolation("开始节点不能有入边")
        if nid in ends and outgoing.get(nid):
            raise PolicyViolation("结束节点不能有出边")
        if node.get("type") != "rule":
            continue
        labels = {label for _, label in outgoing.get(nid, [])}
        if not {"true", "false"}.issubset(labels):
            raise PolicyViolation(f"规则节点 {nid} 必须同时配置 true 和 false 分支")

    # 从开始节点可达。
    reachable: set[str] = set()
    queue: deque[str] = deque(starts)
    while queue:
        current = queue.popleft()
        if current in reachable:
            continue
        reachable.add(current)
        queue.extend(target for target, _ in outgoing.get(current, []))
    if reachable != set(node_map):
        missing = ", ".join(sorted(set(node_map) - reachable))
        raise PolicyViolation(f"工作流存在从开始节点不可达的节点: {missing}")

    # 结束节点可达，且 Kahn 排序保证无环。
    reverse: dict[str, list[str]] = defaultdict(list)
    for source, targets in outgoing.items():
        for target, _ in targets:
            reverse[target].append(source)
    can_reach_end: set[str] = set(ends)
    queue = deque(ends)
    while queue:
        current = queue.popleft()
        for source in reverse.get(current, []):
            if source not in can_reach_end:
                can_reach_end.add(source)
                queue.append(source)
    if can_reach_end != set(node_map):
        missing = ", ".join(sorted(set(node_map) - can_reach_end))
        raise PolicyViolation(f"工作流存在无法到达结束节点的分支: {missing}")

    indegree = {nid: incoming.get(nid, 0) for nid in node_map}
    queue = deque(nid for nid, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for target, _ in outgoing.get(current, []):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(node_map):
        raise PolicyViolation("工作流连线形成环，无法按 DAG 执行")
