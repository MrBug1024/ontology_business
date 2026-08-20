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
_WORKFLOW_NODE_TYPES = {"start", "end", "action", "rule", "llm", "event", "http", "script"}


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


def validate_read_only_sql(sql: str) -> str:
    """校验并规范化只读 SQL，拒绝多语句和写操作。"""
    if not isinstance(sql, str) or not sql.strip():
        raise PolicyViolation("SQL 不能为空")

    # 平台查询工具只允许一条语句；末尾分号可以保留给用户，但不参与执行。
    statement = sql.strip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if ";" in statement:
        raise PolicyViolation("只读查询不允许执行多条 SQL 语句")

    # 去除注释后检查首关键字和 CTE 中的写操作。
    without_comments = re.sub(r"--[^\n]*|/\*.*?\*/", " ", statement, flags=re.S)
    if not _READ_START_RE.match(without_comments.lstrip()):
        raise PolicyViolation("只允许 SELECT、WITH 或 EXPLAIN 查询")
    if _WRITE_RE.search(_strip_sql_literals(without_comments)):
        raise PolicyViolation("只读查询中检测到禁止的写操作或 DDL 关键字")
    return statement


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
