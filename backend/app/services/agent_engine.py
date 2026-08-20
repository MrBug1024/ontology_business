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
from typing import Any, Iterator

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Agent,
    BusinessScenario,
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
from . import datasource_service, llm_service, mcp_service, ontology_service, rag_service, skill_service, tenant_service, workflow_service

class AgentContext:
    """一次 Agent 会话的运行时上下文。"""

    def __init__(self, db: Session, agent: Agent, llm: LLMConfig):
        self.db = db
        self.agent = agent
        self.llm = llm
        # Agent 工具始终以当前租户运行；缺少上下文时拒绝，而不是隐式获得全库访问。
        self.tenant_id = tenant_service.current_tenant_id(db)
        self.scenario = (
            tenant_service.get_visible(db, BusinessScenario, agent.scenario_id)
            if agent.scenario_id else None
        )
        self.data_sources: list[DataSource] = []
        # 一次回答内的引用编号必须稳定、全局唯一；不能把每次检索各自的 C1 混在一起。
        self.citations: list[dict[str, Any]] = []
        self._citations_by_key: dict[tuple[str, str, int, int], dict[str, Any]] = {}
        self.skills: list[Skill] = []
        self.mcps: list[MCPConfig] = []
        self._load_bindings()

    def _load_bindings(self) -> None:
        ds_ids = self.agent.data_source_ids or []
        if ds_ids:
            ds_scope = tenant_service.visible_clause(DataSource, self.db)
            self.data_sources = list(
                self.db.execute(
                    select(DataSource).where(
                        DataSource.id.in_(ds_ids),
                        ds_scope,
                        or_(DataSource.scenario_id.is_(None), DataSource.scenario_id == self.agent.scenario_id),
                    )
                ).scalars().all()
            )
        sk_ids = self.agent.skill_ids or []
        if sk_ids:
            skill_scope = tenant_service.visible_clause(Skill, self.db)
            self.skills = list(
                self.db.execute(select(Skill).where(Skill.id.in_(sk_ids), Skill.enabled == True, skill_scope)).scalars().all()  # noqa: E712
            )
        mcp_ids = self.agent.mcp_ids or []
        if mcp_ids:
            mcp_scope = tenant_service.visible_clause(MCPConfig, self.db)
            self.mcps = list(
                self.db.execute(select(MCPConfig).where(MCPConfig.id.in_(mcp_ids), MCPConfig.enabled == True, mcp_scope)).scalars().all()  # noqa: E712
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

    def _writable_file_buckets(self) -> list[DataSource]:
        """公开绑定资源可读不可写；交付物只允许保存到当前租户自有桶。"""
        return [
            source for source in self.data_sources
            if source.type == "file_bucket" and source.tenant_id == self.tenant_id
        ]

    def _record_citations(self, raw_citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """登记 RAG 命中并生成本轮对话唯一的引用编号。

        ``rag_service`` 已按租户可见性过滤，这里再限制为 Agent 当前绑定的数据源，
        防止未来工具实现误把未绑定或未授权资料写入消息审计记录。
        """
        allowed_source_ids = {source.id for source in self.data_sources if source.type == "file_bucket"}
        normalized: list[dict[str, Any]] = []
        for raw in raw_citations:
            if not isinstance(raw, dict):
                continue
            source_id = str(raw.get("data_source_id") or "")
            file_id = str(raw.get("file_id") or "")
            chunk_id = str(raw.get("chunk_id") or "")
            if not source_id or source_id not in allowed_source_ids or not file_id or not chunk_id:
                continue
            try:
                char_start = int(raw.get("char_start"))
                char_end = int(raw.get("char_end"))
            except (TypeError, ValueError):
                continue
            if char_start < 0 or char_end <= char_start:
                continue
            key = (file_id, chunk_id, char_start, char_end)
            citation = self._citations_by_key.get(key)
            if citation is None:
                citation = {
                    "citation_id": f"C{len(self.citations) + 1}",
                    "chunk_id": chunk_id,
                    "file_id": file_id,
                    "filename": str(raw.get("filename") or "资料文件"),
                    "data_source_id": source_id,
                    "data_source_name": str(raw.get("data_source_name") or "资料库"),
                    "char_start": char_start,
                    "char_end": char_end,
                    "chunk_ordinal": int(raw.get("chunk_ordinal") or 0),
                    "content_hash": str(raw.get("content_hash") or ""),
                    "embedding_model": str(raw.get("embedding_model") or ""),
                    "index_version": str(raw.get("index_version") or ""),
                    "score": float(raw.get("score") or 0),
                    "vector_score": float(raw.get("vector_score") or 0),
                    "keyword_score": float(raw.get("keyword_score") or 0),
                    "text": str(raw.get("text") or ""),
                }
                self.citations.append(citation)
                self._citations_by_key[key] = citation
            normalized.append(dict(citation))
        return normalized

    def citation_snapshot(self) -> list[dict[str, Any]]:
        """返回可安全发送到 SSE / 持久化消息的独立副本。"""
        return [dict(citation) for citation in self.citations]

    # ── 工具定义 ──────────────────────────────
    def build_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if self.scenario and self.scenario.entities:
            tools.append(
                _tool(
                    "search_ontology",
                    "按实体类型和关键词检索当前业务场景中的本体实例及属性。"
                    "用于先确认业务对象，再决定是否需要查询外部数据源。",
                    {
                        "entity": {"type": "string", "description": "实体名称，可留空查看所有实体"},
                        "query": {"type": "string", "description": "实例名称或属性关键词，可留空"},
                    },
                )
            )
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
                    "在已绑定且有权限的文件桶中执行混合向量检索，返回可引用的资料片段。"
                    "回答引用事实时必须使用结果的 citation_id（如【C1】）。",
                    {
                        "query": {"type": "string", "description": "检索问题或关键词"},
                        "top_k": {"type": "integer", "description": "返回片段数，默认 5，最大 10"},
                    },
                ),
                _tool(
                    "read_document",
                    "读取已绑定且有权限的已解析文档。优先使用 search_documents 返回的 file_id，"
                    "避免仅按文件名读取同名资料。",
                    {
                        "file_id": {"type": "string", "description": "检索结果中的文件 id"},
                        "filename": {"type": "string", "description": "兼容旧会话的文件名，重名时会拒绝"},
                    },
                ),
            ]
            if self._writable_file_buckets():
                tools.append(
                    _tool(
                        "save_deliverable",
                        "把正式业务产出物（报告、清单、分析结果或其他交付文件）保存为可下载附件，"
                        "返回附件的预览/下载链接。参数 filename 为文件名（建议 .md 或 .txt 结尾），content 为附件全文内容。"
                        "生成正式交付文件时应调用本工具，并在回答中附上返回的链接。",
                        {
                            "filename": {"type": "string", "description": "附件文件名，如 业务分析报告.md"},
                            "content": {"type": "string", "description": "附件全文内容（Markdown 或纯文本）"},
                        },
                    )
                )
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
            if name == "search_ontology":
                return _dump(
                    ontology_service.search_instances(
                        self.db,
                        self.scenario,
                        entity_name=args.get("entity", ""),
                        query=args.get("query", ""),
                        limit=get_settings().max_query_rows,
                    )
                )
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
                results = rag_service.search(
                    self.db,
                    [d.id for d in self.data_sources if d.type == "file_bucket"],
                    args.get("query", ""),
                    top_k=max(1, min(int(args.get("top_k") or 5), 10)),
                )
                citations = self._record_citations(results)
                if not citations:
                    return "未检索到可引用的文档内容；请检查资料库绑定、文档解析和检索词。"
                return _dump(
                    {
                        "retrieval_mode": "hybrid-vector-keyword",
                        "citations": citations,
                        "instruction": "最终回答涉及资料事实时，请标注对应的 citation_id，例如【C1】；不要编造引用。",
                    }
                )
            if name == "read_document":
                return self._read_doc(args.get("file_id", ""), args.get("filename", ""))
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
                # 助手工具调用也必须经过显式确认；对话模型不能绕过页面的预演/幂等执行链路。
                r = workflow_service.execute_action(
                    self.db,
                    a,
                    args.get("params", {}),
                    confirm=False,
                    enforce_policy=True,
                )
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

    def _read_doc(self, file_id: str = "", filename: str = "") -> str:
        """读取资料库全文时复用绑定范围与租户可见性，避免绕过检索边界。"""
        bound_ids = [d.id for d in self.data_sources if d.type == "file_bucket"]
        if not bound_ids:
            return "当前 Agent 未绑定可检索资料库"
        stmt = select(BucketFile).where(
            BucketFile.data_source_id.in_(bound_ids),
        )
        if file_id:
            stmt = stmt.where(BucketFile.id == file_id)
        elif filename:
            stmt = stmt.where(BucketFile.filename == filename)
        else:
            return "请提供 search_documents 返回的 file_id"
        stmt = stmt.join(DataSource, DataSource.id == BucketFile.data_source_id).where(
            tenant_service.visible_clause(DataSource, self.db)
        )
        matches = self.db.execute(stmt).scalars().all()
        if filename and len(matches) > 1:
            return "存在同名资料，请先使用 search_documents 返回的 file_id 精确读取"
        f = matches[0] if matches else None
        if not f:
            return f"未找到或无权读取文件: {file_id}"
        return (f.parsed_text or f"文件 {f.filename} 暂无解析内容")[:24_000]

    def _save_deliverable(self, filename: str, content: str) -> str:
        """把产出物保存为文件桶附件，返回预览/下载链接。"""
        bucket = next(iter(self._writable_file_buckets()), None)
        if not bucket:
            if any(d.type == "file_bucket" for d in self.data_sources):
                return "绑定的文件桶均为只读公开资源，无法保存附件；请绑定当前租户自有文件桶"
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
        self.db.flush()
        rag_service.enqueue_document_index(self.db, bf, parse_document=True)
        self.db.commit()
        self.db.refresh(bf)
        return _dump(
            {
                "saved": True,
                "file_id": bf.id,
                "filename": bf.filename,
                "size": bf.size,
                "index_status": bf.index_status,
                "preview_url": f"/api/data-sources/files/{bf.id}/text",
                "download_url": f"/api/data-sources/files/{bf.id}/download",
                "提示": "附件已排队解析和建立检索索引；请在回答中以 Markdown 链接形式附上下载地址，例如 [📎 业务交付物.md](/api/data-sources/files/" + bf.id + "/download)",
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
        parts.append(
            "你可以用 list_tables 查看表结构，用 run_sql 查询数据库，用 search_documents 检索文件桶中的文档。"
            "凡引用检索到的事实，必须在答案中标出工具结果提供的【C#】；未检索到依据时明确说明。"
        )
        if ctx._writable_file_buckets():
            parts.append(
                "当你生成正式业务交付物时，务必调用 save_deliverable 工具把它保存为附件，"
                "并在最终回答中以 Markdown 链接形式附上返回的 download_url。"
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
        "\n【工作方式】请根据用户问题，自主调用合适的工具获取数据，然后给出准确、结构化的回答。"
        "涉及数据时务必基于工具返回的真实数据，不要编造；无法确认时明确说明数据缺口。"
        "如果场景定义了本体、操作、规则或工作流，优先使用这些业务抽象来完成任务。"
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

    事件类型: status / tool_call / tool_result / token / citations / done / error
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
        for ev in llm_service.chat_stream(
            llm,
            messages,
            tools=tools or None,
            temperature=agent.temperature,
            max_tokens=agent.max_tokens,
            db=db,
        ):
            if ev["type"] == "token":
                content_parts.append(ev["content"])
                yield {"type": "token", "data": ev["content"]}
            elif ev["type"] == "tool_calls":
                tool_calls = ev["tool_calls"]

        content = "".join(content_parts)

        if not tool_calls:
            # 最终回答
            if ctx.citations:
                yield {"type": "citations", "data": ctx.citation_snapshot()}
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
            yield {"type": "tool_call", "data": {"id": tc["id"], "name": fname, "arguments": fargs}}
            result = ctx.execute_tool(fname, fargs)
            yield {"type": "tool_result", "data": {"id": tc["id"], "name": fname, "result": result[:8000]}}
            messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "name": fname, "content": result[:8000]}
            )

    if ctx.citations:
        yield {"type": "citations", "data": ctx.citation_snapshot()}
    yield {"type": "done", "data": "（已达到最大工具调用轮数，回答可能不完整）"}
