"""Agent 引擎：基于工具调用（ReAct）循环，让 LLM 在业务场景下自主完成需求。

可用工具：
- list_data_sources   列出 Agent 绑定的数据源
- list_tables         列出某数据源的表结构
- run_sql             在数据源上执行只读 SQL
- search_documents    在文件桶中检索相关文档片段（RAG）
- read_document       读取某个已解析文档的全文
- execute_skill       执行已安装的技能（如 ocr-parser 解析文件）
- mcp_*               调用已安装的 MCP 工具
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Agent,
    BucketFile,
    DataSource,
    LLMConfig,
    MCPConfig,
    OntologyAction,
    OntologyEvent,
    OntologyRule,
    OntologyWorkflow,
    Skill,
)
from . import datasource_service, doc_parser, llm_service, mcp_service, rag_service, skill_service, workflow_service

ToolFn = Callable[[dict[str, Any]], str]


class AgentContext:
    """一次 Agent 会话的运行时上下文。"""

    def __init__(self, db: Session, agent: Agent, llm: LLMConfig):
        self.db = db
        self.agent = agent
        self.llm = llm
        self.data_sources: list[DataSource] = []
        self.skills: list[Skill] = []
        self.mcps: list[MCPConfig] = []
        self._load_bindings()

    def _load_bindings(self) -> None:
        ds_ids = self.agent.data_source_ids or []
        if ds_ids:
            self.data_sources = list(
                self.db.execute(select(DataSource).where(DataSource.id.in_(ds_ids))).scalars().all()
            )
        sk_ids = self.agent.skill_ids or []
        if sk_ids:
            self.skills = list(
                self.db.execute(select(Skill).where(Skill.id.in_(sk_ids), Skill.enabled == True)).scalars().all()  # noqa: E712
            )
        mcp_ids = self.agent.mcp_ids or []
        if mcp_ids:
            self.mcps = list(
                self.db.execute(select(MCPConfig).where(MCPConfig.id.in_(mcp_ids), MCPConfig.enabled == True)).scalars().all()  # noqa: E712
            )
        # 本体扩展：操作 / 规则 / 事件 / 工作流（按场景加载）
        self.actions: list[OntologyAction] = []
        self.rules: list[OntologyRule] = []
        self.events: list[OntologyEvent] = []
        self.workflows: list[OntologyWorkflow] = []
        sid = self.agent.scenario_id
        if sid:
            self.actions = list(
                self.db.execute(select(OntologyAction).where(OntologyAction.scenario_id == sid, OntologyAction.enabled == True)).scalars().all()  # noqa: E712
            )
            self.rules = list(
                self.db.execute(select(OntologyRule).where(OntologyRule.scenario_id == sid, OntologyRule.enabled == True)).scalars().all()  # noqa: E712
            )
            self.events = list(
                self.db.execute(select(OntologyEvent).where(OntologyEvent.scenario_id == sid, OntologyEvent.enabled == True)).scalars().all()  # noqa: E712
            )
            self.workflows = list(
                self.db.execute(select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == sid, OntologyWorkflow.enabled == True)).scalars().all()  # noqa: E712
            )

    # ── 工具定义 ──────────────────────────────
    def build_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if self.data_sources:
            tools += [
                _tool(
                    "list_data_sources",
                    "列出当前 Agent 绑定的所有数据源（数据库与文件桶），返回 id、名称、类型。",
                    {},
                ),
                _tool(
                    "list_tables",
                    "列出指定数据库数据源中的表及其列结构。参数 data_source_id 为数据源 id。",
                    {"data_source_id": {"type": "string", "description": "数据源 id"}},
                ),
                _tool(
                    "run_sql",
                    "在指定数据库数据源上执行只读 SQL 查询（SELECT）。参数 data_source_id、sql。",
                    {
                        "data_source_id": {"type": "string", "description": "数据源 id"},
                        "sql": {"type": "string", "description": "只读 SQL 语句"},
                    },
                ),
                _tool(
                    "search_documents",
                    "在文件桶数据源中按关键词检索相关文档片段（RAG）。参数 query 为检索词。",
                    {"query": {"type": "string", "description": "检索关键词"}},
                ),
                _tool(
                    "read_document",
                    "读取某个已解析文档的全文。参数 filename 为文件名。",
                    {"filename": {"type": "string", "description": "文件名"}},
                ),
                _tool(
                    "save_deliverable",
                    "把业务产出物（审计报告、财务报表、附注、月度报告、函证、分析结果等）保存为可下载附件，"
                    "返回附件的预览/下载链接。参数 filename 为文件名（建议 .md 或 .txt 结尾），content 为附件全文内容。"
                    "凡是生成正式业务文档/报表/报告时都应调用本工具，并在回答中附上返回的链接。",
                    {
                        "filename": {"type": "string", "description": "附件文件名，如 审计报告-AP001.md"},
                        "content": {"type": "string", "description": "附件全文内容（Markdown 或纯文本）"},
                    },
                ),
            ]
        if self.skills:
            tools.append(
                _tool(
                    "execute_skill",
                    "执行已安装的技能。参数 skill_name 为技能名，args 为命令行参数数组。"
                    "例如用 ocr-parser 解析文件：skill_name=ocr-parser, args=[\"--path\",\"/path/file.pdf\",\"--format\",\"json\"]。",
                    {
                        "skill_name": {"type": "string", "description": "技能名"},
                        "args": {"type": "array", "items": {"type": "string"}, "description": "命令行参数"},
                    },
                )
            )
        for mcp in self.mcps:
            try:
                for t in mcp_service.list_tools(mcp):
                    tools.append(
                        _tool(
                            f"mcp_{mcp.name}_{t['name']}",
                            f"[MCP:{mcp.name}] {t['description'] or t['name']}",
                            t.get("input_schema", {}).get("properties", {}),
                        )
                    )
            except Exception:  # noqa: BLE001
                continue
        # 本体扩展工具：操作 / 规则 / 工作流
        if self.actions:
            tools.append(
                _tool(
                    "list_actions",
                    "列出当前业务场景中定义的所有可执行操作（Actions），返回 id、名称、所属实体、执行方式。",
                    {},
                )
            )
            tools.append(
                _tool(
                    "execute_action",
                    "执行场景中定义的某个操作（Action）。参数 action_id 为操作 id，params 为输入参数对象。",
                    {
                        "action_id": {"type": "string", "description": "操作 id"},
                        "params": {"type": "object", "description": "输入参数"},
                    },
                )
            )
        if self.rules:
            tools.append(
                _tool(
                    "list_rules",
                    "列出当前业务场景中定义的所有业务规则（Rules），返回 id、名称、条件表达式、严重级别。",
                    {},
                )
            )
            tools.append(
                _tool(
                    "evaluate_rule",
                    "用给定数据记录评估某条业务规则是否命中。参数 rule_id 为规则 id，record 为数据记录对象。",
                    {
                        "rule_id": {"type": "string", "description": "规则 id"},
                        "record": {"type": "object", "description": "待评估的数据记录"},
                    },
                )
            )
        if self.workflows:
            tools.append(
                _tool(
                    "list_workflows",
                    "列出当前业务场景中定义的所有工作流（Workflows），返回 id、名称、触发方式、步骤数。",
                    {},
                )
            )
            tools.append(
                _tool(
                    "execute_workflow",
                    "执行场景中定义的某个工作流（按步骤顺序执行操作/规则/事件）。参数 workflow_id 为工作流 id，params 为输入参数。",
                    {
                        "workflow_id": {"type": "string", "description": "工作流 id"},
                        "params": {"type": "object", "description": "输入参数"},
                    },
                )
            )
        return tools

    # ── 工具执行 ──────────────────────────────
    def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name == "list_data_sources":
                return _dump(
                    [
                        {"id": d.id, "name": d.name, "type": d.type, "status": d.status}
                        for d in self.data_sources
                    ]
                )
            if name == "list_tables":
                ds = self._ds(args.get("data_source_id"))
                if not ds:
                    return "未找到该数据源"
                return _dump(datasource_service.list_tables(ds))
            if name == "run_sql":
                ds = self._ds(args.get("data_source_id"))
                if not ds:
                    return "未找到该数据源"
                limit = get_settings().max_query_rows
                try:
                    return _dump(datasource_service.run_query(ds, args.get("sql", ""), limit=limit))
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    if "no such table" in msg:
                        try:
                            tables = [t["name"] for t in datasource_service.list_tables(ds)]
                            msg += f"。该数据源可用表: {', '.join(tables)}"
                        except Exception:  # noqa: BLE001
                            pass
                    return f"SQL 执行出错: {msg}"
            if name == "search_documents":
                results = rag_service.search(self.db, [d.id for d in self.data_sources if d.type == "file_bucket"], args.get("query", ""))
                return _dump(results) if results else "未检索到相关文档内容"
            if name == "read_document":
                return self._read_doc(args.get("filename", ""))
            if name == "save_deliverable":
                return self._save_deliverable(args.get("filename", ""), args.get("content", ""))
            if name == "execute_skill":
                return self._exec_skill(args.get("skill_name", ""), args.get("args", []))
            if name.startswith("mcp_"):
                return self._exec_mcp(name, args)
            # 本体扩展工具
            if name == "list_actions":
                return _dump(
                    [
                        {
                            "id": a.id,
                            "name": a.name,
                            "entity": a.entity.name if a.entity else "",
                            "executor_type": a.executor_type,
                            "description": a.description[:120],
                        }
                        for a in self.actions
                    ]
                )
            if name == "execute_action":
                key = args.get("action_id") or ""
                a = next((x for x in self.actions if x.id == key), None)
                if not a:
                    a = next((x for x in self.actions if x.name == key), None)
                if not a:
                    names = "、".join(x.name for x in self.actions)
                    return f"未找到操作: {key}（可用操作: {names}，请用 list_actions 查看 id）"
                r = workflow_service.execute_action(self.db, a, args.get("params", {}))
                return _dump(r)
            if name == "list_rules":
                return _dump(
                    [
                        {
                            "id": r.id,
                            "name": r.name,
                            "entity": r.entity.name if r.entity else "",
                            "severity": r.severity,
                            "condition": r.condition,
                        }
                        for r in self.rules
                    ]
                )
            if name == "evaluate_rule":
                r = next((x for x in self.rules if x.id == args.get("rule_id")), None)
                if not r:
                    return f"未找到规则: {args.get('rule_id')}"
                return _dump(workflow_service.evaluate_rule(r, args.get("record", {})))
            if name == "list_workflows":
                return _dump(
                    [
                        {
                            "id": w.id,
                            "name": w.name,
                            "trigger_type": w.trigger_type,
                            "steps_count": len(w.steps or []),
                            "description": w.description[:120],
                        }
                        for w in self.workflows
                    ]
                )
            if name == "execute_workflow":
                w = next((x for x in self.workflows if x.id == args.get("workflow_id")), None)
                if not w:
                    return f"未找到工作流: {args.get('workflow_id')}"
                r = workflow_service.execute_workflow(self.db, w, args.get("params", {}))
                return _dump(r)
            return f"未知工具: {name}"
        except Exception as exc:  # noqa: BLE001
            return f"工具执行出错: {exc}"

    def _ds(self, ds_id: str | None) -> DataSource | None:
        for d in self.data_sources:
            if d.id == ds_id:
                return d
        return None

    def _read_doc(self, filename: str) -> str:
        stmt = select(BucketFile).where(
            BucketFile.data_source_id.in_([d.id for d in self.data_sources]),
            BucketFile.filename == filename,
        )
        f = self.db.execute(stmt).scalars().first()
        if not f:
            return f"未找到文件: {filename}"
        return f.parsed_text or f"文件 {filename} 暂无解析内容"

    def _save_deliverable(self, filename: str, content: str) -> str:
        """把产出物保存为文件桶附件，返回预览/下载链接。"""
        bucket = next((d for d in self.data_sources if d.type == "file_bucket"), None)
        if not bucket:
            return "未绑定文件桶数据源，无法保存附件"
        if not content or not content.strip():
            return "附件内容为空，未保存"
        safe = Path(filename).name or f"deliverable_{uuid.uuid4().hex[:8]}.md"
        if "." not in safe:
            safe += ".md"
        # 同名文件加时间戳避免覆盖
        existing = self.db.execute(
            select(BucketFile).where(
                BucketFile.data_source_id == bucket.id, BucketFile.filename == safe
            )
        ).scalars().first()
        if existing:
            stem, dot, ext = safe.rpartition(".")
            safe = f"{stem}_{uuid.uuid4().hex[:6]}.{ext}" if dot else f"{safe}_{uuid.uuid4().hex[:6]}"
        data = content.encode("utf-8")
        bf = datasource_service.save_bucket_file(bucket, safe, data)
        self.db.add(bf)
        # 解析（md/txt 直接取原文，便于预览）
        try:
            r = doc_parser.parse_file(bf.stored_path, bf.filename)
            bf.status = "parsed" if r["status"] == "success" else "error"
            bf.parsed_text = r.get("text", "") or content
            bf.error = "" if r["status"] == "success" else r.get("message", "")
        except Exception:  # noqa: BLE001
            bf.status = "parsed"
            bf.parsed_text = content
        self.db.commit()
        self.db.refresh(bf)
        return _dump(
            {
                "saved": True,
                "file_id": bf.id,
                "filename": bf.filename,
                "size": bf.size,
                "preview_url": f"/api/data-sources/files/{bf.id}/text",
                "download_url": f"/api/data-sources/files/{bf.id}/download",
                "提示": "请在回答中以 Markdown 链接形式附上该附件，例如 [📎 审计报告-AP001.md](/api/data-sources/files/" + bf.id + "/download)",
            }
        )

    def _exec_skill(self, skill_name: str, args: list[str]) -> str:
        skill = next((s for s in self.skills if s.name == skill_name), None)
        if not skill:
            return f"未找到技能: {skill_name}（已安装: {[s.name for s in self.skills]}）"
        r = skill_service.execute_skill(skill, args)
        out = r.get("stdout", "")
        if r.get("stderr"):
            out += f"\n[stderr] {r['stderr']}"
        return out or f"技能执行结束（exit={r.get('exit_code')}）"

    def _exec_mcp(self, tool_name: str, args: dict[str, Any]) -> str:
        # tool_name 形如 mcp_<mcpname>_<toolname>
        parts = tool_name.split("_", 2)
        if len(parts) < 3:
            return "MCP 工具名格式错误"
        mcp_name, mcp_tool = parts[1], parts[2]
        mcp = next((m for m in self.mcps if m.name == mcp_name), None)
        if not mcp:
            return f"未找到 MCP: {mcp_name}"
        r = mcp_service.call_tool(mcp, mcp_tool, args)
        return r.get("text", "") or json.dumps(r, ensure_ascii=False)


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    required = [k for k, v in properties.items() if v.get("required")]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or list(properties.keys())[:1] if properties else [],
            },
        },
    }


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _table_map(ctx: AgentContext) -> str:
    """从 SQL 操作的 executor_config 中提取真实表名，映射到实体名，供 run_sql 参考。"""
    import re

    mapping: dict[str, str] = {}
    for a in ctx.actions:
        cfg = a.executor_config or {}
        sql = cfg.get("sql", "") if isinstance(cfg, dict) else ""
        if not sql:
            continue
        ent = a.entity.name if a.entity else ""
        for m in re.findall(r"(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.IGNORECASE):
            if m.lower() in ("select", "where", "and", "or", "set", "values"):
                continue
            mapping.setdefault(m, ent)
    if not mapping:
        return ""
    return "\n".join(f"- 实体「{ent}」→ 表 `{tbl}`" for tbl, ent in sorted(mapping.items()))


# ──────────────────────────────────────────────
# 系统提示词构建
# ──────────────────────────────────────────────
def build_system_prompt(ctx: AgentContext, scenario_name: str, ontology_summary: str) -> str:
    base = ctx.agent.system_prompt or "你是一名专业的业务智能助手。"
    parts = [base, ""]
    if scenario_name:
        parts.append(f"【当前业务场景】{scenario_name}")
    if ontology_summary:
        parts.append("\n【业务本体（领域模型）】\n" + ontology_summary)
    if ctx.data_sources:
        ds_lines = [f"- {d.name}（{d.type}，id={d.id}）" for d in ctx.data_sources]
        parts.append("\n【可用数据源】\n" + "\n".join(ds_lines))
        parts.append("你可以用 list_tables 查看表结构，用 run_sql 查询数据库，用 search_documents 检索文件桶中的文档。")
        parts.append(
            "当你生成正式业务产出物（审计报告、财务报表、报表附注、管理建议书、月度报告、函证、分析报告等）时，"
            "务必调用 save_deliverable 工具把它保存为附件，并在最终回答中以 Markdown 链接形式附上返回的 download_url，"
            "例如 [📎 审计报告-AP001.md](/api/data-sources/files/<file_id>/download)，让用户可以点击下载或预览。"
        )
        table_map = _table_map(ctx)
        if table_map:
            parts.append("\n【实体 → 数据库表名映射】（run_sql 时请使用下列真实表名，不要臆造）\n" + table_map)
    if ctx.skills:
        parts.append("\n【已安装技能】\n" + "\n".join(f"- {s.name}: {s.description[:80]}" for s in ctx.skills))
        parts.append("你可以用 execute_skill 调用技能（如用 ocr-parser 解析 PDF/图片）。")
    if ctx.mcps:
        parts.append("\n【已安装 MCP 服务】\n" + "\n".join(f"- {m.name}（{m.transport}）" for m in ctx.mcps))
    if ctx.actions:
        parts.append(
            "\n【可执行操作（Actions）】\n"
            + "\n".join(f"- {a.name}（id={a.id}，{a.entity.name if a.entity else '?'}，{a.executor_type}）: {a.description[:60]}" for a in ctx.actions)
            + "\n你可以用 list_actions 查看操作列表，用 execute_action 执行操作（action_id 必须用上面的 id，不要用中文名）。"
        )
    if ctx.rules:
        parts.append(
            "\n【业务规则（Rules）】\n"
            + "\n".join(f"- {r.name}（{r.severity}）: {r.description[:60]}" for r in ctx.rules)
            + "\n你可以用 list_rules 查看规则列表，用 evaluate_rule 评估规则是否命中。"
        )
    if ctx.workflows:
        parts.append(
            "\n【工作流（Workflows）】\n"
            + "\n".join(f"- {w.name}（id={w.id}，{w.trigger_type}，{len(w.steps or [])}步）: {w.description[:60]}" for w in ctx.workflows)
            + "\n你可以用 list_workflows 查看工作流列表，用 execute_workflow 执行工作流（workflow_id 必须用上面的 id，不要用中文名）。"
        )
    parts.append(
        "\n【工作方式】请根据用户问题，自主调用合适的工具获取数据，然后给出准确、结构化的中文回答。"
        "涉及数据时务必基于工具返回的真实数据，不要编造。"
        "如果场景定义了操作/规则/工作流，优先使用这些业务抽象来完成任务，而非直接写 SQL。"
    )
    return "\n".join(parts)


def ontology_summary_for(scenario) -> str:
    """把本体（实体/属性/关系）序列化为给 LLM 的领域模型描述。"""
    if not scenario or not scenario.entities:
        return ""
    lines = []
    for e in scenario.entities:
        props = ", ".join(
            f"{p.name}:{p.data_type}" + ("(主键)" if p.is_key else "") for p in e.properties
        )
        lines.append(f"- 实体「{e.name}」: {props or '无属性'}")
        if e.description:
            lines.append(f"  说明: {e.description}")
    for r in scenario.relations:
        src = next((e.name for e in scenario.entities if e.id == r.source_entity_id), "?")
        tgt = next((e.name for e in scenario.entities if e.id == r.target_entity_id), "?")
        lines.append(f"- 关系: {src} --[{r.name}]--({r.relation_type})--> {tgt}")
    # 本体扩展维度
    if getattr(scenario, "actions", None):
        lines.append("\n【操作（Actions）】")
        for a in scenario.actions:
            ent = next((e.name for e in scenario.entities if e.id == a.entity_id), "?")
            lines.append(f"- 操作「{a.name}」(实体:{ent}, 执行:{a.executor_type}): {a.description[:60]}")
    if getattr(scenario, "rules", None):
        lines.append("\n【规则（Rules）】")
        for r in scenario.rules:
            lines.append(f"- 规则「{r.name}」({r.severity}): {r.description[:60]}")
    if getattr(scenario, "events", None):
        lines.append("\n【事件（Events）】")
        for e in scenario.events:
            lines.append(f"- 事件「{e.name}」: {e.description[:60]}")
    if getattr(scenario, "workflows", None):
        lines.append("\n【工作流（Workflows）】")
        for w in scenario.workflows:
            lines.append(f"- 工作流「{w.name}」({w.trigger_type}, {len(w.steps or [])}步): {w.description[:60]}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 主循环
# ──────────────────────────────────────────────
def run_agent(
    db: Session,
    agent: Agent,
    llm: LLMConfig,
    history: list[dict[str, Any]],
    user_message: str,
    scenario_name: str,
    ontology_summary: str,
) -> Iterator[dict[str, Any]]:
    """执行 Agent 工具循环，逐事件 yield。

    事件类型: status / tool_call / tool_result / token / done / error
    """
    ctx = AgentContext(db, agent, llm)
    system_prompt = build_system_prompt(ctx, scenario_name, ontology_summary)
    tools = ctx.build_tools()

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages += history
    messages.append({"role": "user", "content": user_message})

    max_rounds = get_settings().max_tool_rounds
    for _round in range(max_rounds):
        # 流式获取本轮 LLM 输出
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for ev in llm_service.chat_stream(llm, messages, tools=tools or None):
            if ev["type"] == "token":
                content_parts.append(ev["content"])
                yield {"type": "token", "data": ev["content"]}
            elif ev["type"] == "tool_calls":
                tool_calls = ev["tool_calls"]

        content = "".join(content_parts)

        if not tool_calls:
            # 最终回答
            yield {"type": "done", "data": content}
            return

        # 记录 assistant 的工具调用
        messages.append(
            {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": json.dumps(tc["function"]["arguments"], ensure_ascii=False)},
                    }
                    for tc in tool_calls
                ],
            }
        )

        # 执行每个工具
        for tc in tool_calls:
            fname = tc["function"]["name"]
            fargs = tc["function"]["arguments"] or {}
            yield {"type": "tool_call", "data": {"name": fname, "arguments": fargs}}
            result = ctx.execute_tool(fname, fargs)
            yield {"type": "tool_result", "data": {"name": fname, "result": result[:8000]}}
            messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "name": fname, "content": result[:8000]}
            )

    yield {"type": "done", "data": "（已达到最大工具调用轮数，回答可能不完整）"}
