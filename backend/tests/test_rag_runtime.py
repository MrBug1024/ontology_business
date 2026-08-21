from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Agent,
    BucketFile,
    Conversation,
    DataSource,
    DocumentChunk,
    DocumentIndexJob,
    LLMConfig,
    MCPConfig,
    Message,
    Skill,
    Tenant,
)
from app.services.agent_engine import AgentContext, run_agent
from app.services import rag_service
from app.schemas import MessageOut


class RagRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant_a = Tenant(id="tenant-a", name="甲方")
        self.tenant_b = Tenant(id="tenant-b", name="乙方")
        self.db.add_all([self.tenant_a, self.tenant_b])
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant_a.id

        self.private_source = DataSource(
            id="source-a",
            tenant_id=self.tenant_a.id,
            name="甲方资料库",
            type="file_bucket",
            config={},
        )
        self.foreign_source = DataSource(
            id="source-b",
            tenant_id=self.tenant_b.id,
            name="乙方私有资料库",
            type="file_bucket",
            config={},
        )
        self.public_source = DataSource(
            id="source-public",
            tenant_id=self.tenant_b.id,
            is_public=True,
            name="公开资料库",
            type="file_bucket",
            config={},
        )
        self.db.add_all([self.private_source, self.foreign_source, self.public_source])
        self.db.flush()
        self.private_file = self._file(
            "file-a",
            self.private_source.id,
            "费用治理.md",
            "费用控制应在每月 5 日前完成复核。成本异常必须进入人工审批流程。",
        )
        self.foreign_file = self._file(
            "file-b",
            self.foreign_source.id,
            "乙方机密.md",
            "乙方私有机密：绝不能被其他租户检索到。",
        )
        self.public_file = self._file(
            "file-public",
            self.public_source.id,
            "公开规则.md",
            "公开资料：合同审批需要记录核准意见。",
        )
        self.db.add_all([self.private_file, self.foreign_file, self.public_file])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _file(file_id: str, source_id: str, filename: str, text: str) -> BucketFile:
        return BucketFile(
            id=file_id,
            data_source_id=source_id,
            filename=filename,
            stored_path=f"/tmp/{file_id}.md",
            status="parsed",
            parsed_text=text,
        )

    def test_incremental_index_persists_chunks_and_offsets(self) -> None:
        first = rag_service.index_file(self.db, self.private_file)
        self.db.commit()
        self.assertTrue(first["indexed"])
        self.assertEqual(self.private_file.index_status, "indexed")
        chunks = self.db.scalars(
            select(DocumentChunk).where(DocumentChunk.bucket_file_id == self.private_file.id)
        ).all()
        self.assertEqual(len(chunks), self.private_file.chunk_count)
        self.assertTrue(chunks[0].text)
        self.assertEqual(
            self.private_file.parsed_text[chunks[0].char_start:chunks[0].char_end],
            chunks[0].text,
        )
        self.assertEqual(len(chunks[0].embedding), rag_service.EMBEDDING_DIMENSIONS)

        unchanged = rag_service.index_file(self.db, self.private_file)
        self.assertFalse(unchanged["indexed"])
        self.private_file.parsed_text = "更新后的预算成本规则：每周复核一次。"
        updated = rag_service.index_file(self.db, self.private_file)
        self.db.commit()
        self.assertTrue(updated["indexed"])
        refreshed = self.db.scalars(
            select(DocumentChunk).where(DocumentChunk.bucket_file_id == self.private_file.id)
        ).all()
        self.assertTrue(all("更新后的预算" in chunk.text for chunk in refreshed))

    def test_search_returns_citations_and_applies_tenant_filter_before_ranking(self) -> None:
        rag_service.index_file(self.db, self.private_file)
        self.db.commit()
        results = rag_service.search(
            self.db,
            [self.private_source.id, self.foreign_source.id, self.public_source.id],
            "费用审批",
            top_k=5,
        )
        self.assertTrue(results)
        self.assertEqual(results[0]["file_id"], self.private_file.id)
        self.assertNotIn(self.foreign_source.id, {item["data_source_id"] for item in results})
        self.assertTrue({item["data_source_id"] for item in results}.issubset({self.private_source.id, self.public_source.id}))
        citation = results[0]
        self.assertTrue(citation["chunk_id"])
        self.assertEqual(citation["citation_id"], "C1")
        self.assertEqual(citation["embedding_model"], rag_service.EMBEDDING_MODEL)
        self.assertGreaterEqual(citation["char_start"], 0)
        self.assertGreater(citation["char_end"], citation["char_start"])
        self.assertIn("【C1】", rag_service.build_context(results))

        denied = rag_service.search(self.db, [self.foreign_source.id], "机密")
        self.assertEqual(denied, [])

    def test_document_index_job_runs_async_and_unblocks_search(self) -> None:
        job, created = rag_service.enqueue_document_index(
            self.db,
            self.private_file,
            parse_document=False,
        )
        self.assertTrue(created)
        self.assertEqual(self.private_file.index_status, "queued")
        self.db.commit()

        # 入队后搜索严格只读；未索引前不会在请求中做同步 embedding 计算。
        self.assertEqual(
            rag_service.search(self.db, [self.private_source.id], "费用审批"),
            [],
        )
        processed = rag_service.process_document_index_jobs(self.db)
        self.assertEqual([item.id for item in processed], [job.id])
        stored = self.db.get(DocumentIndexJob, job.id)
        self.assertEqual(stored.status, "succeeded")
        self.assertIsNone(stored.active_key)
        self.assertEqual(self.private_file.index_status, "indexed")
        self.assertTrue(rag_service.search(self.db, [self.private_source.id], "费用审批"))

        # 终态必须释放活跃键，之后的显式重建才可以正常入队。
        rebuilt, created = rag_service.enqueue_document_index(
            self.db,
            self.private_file,
            parse_document=False,
            force=True,
        )
        self.assertTrue(created)
        self.assertNotEqual(rebuilt.id, job.id)

    def test_document_index_job_records_terminal_parse_failure(self) -> None:
        job, _ = rag_service.enqueue_document_index(
            self.db,
            self.private_file,
            parse_document=True,
        )
        job.max_attempts = 1
        self.db.commit()
        with patch(
            "app.services.doc_parser.parse_file",
            return_value={"status": "error", "text": "", "message": "不支持的示例格式"},
        ):
            rag_service.process_document_index_jobs(self.db)
        stored = self.db.get(DocumentIndexJob, job.id)
        self.assertEqual(stored.status, "failed")
        self.assertEqual(self.private_file.index_status, "error")
        self.assertIn("不支持的示例格式", self.private_file.index_error)

    def test_search_fails_closed_and_does_not_rebuild_foreign_public_index(self) -> None:
        # 公开资料由所属租户先建立索引。
        self.db.info["tenant_id"] = self.tenant_b.id
        indexed = rag_service.index_file(self.db, self.public_file)
        self.assertTrue(indexed["indexed"])
        self.db.commit()
        old_hash = self.public_file.indexed_content_hash
        old_chunks = self.db.scalars(
            select(DocumentChunk).where(DocumentChunk.bucket_file_id == self.public_file.id)
        ).all()
        self.assertTrue(old_chunks)

        # 所有者改了原文但尚未索引；其他租户只能等其重建，不得以搜索触发写入。
        self.public_file.parsed_text = "公开资料更新：合同审批应由授权人核准。"
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant_a.id
        self.assertEqual(rag_service.search(self.db, [self.public_source.id], "合同核准"), [])
        self.assertEqual(self.public_file.index_status, "indexed")
        self.assertEqual(self.public_file.indexed_content_hash, old_hash)
        unchanged_chunks = self.db.scalars(
            select(DocumentChunk).where(DocumentChunk.bucket_file_id == self.public_file.id)
        ).all()
        self.assertEqual([chunk.id for chunk in unchanged_chunks], [chunk.id for chunk in old_chunks])

        # 服务层没有 tenant context 时必须拒绝，而不是按调用者传入的 source id 读取。
        self.db.info.pop("tenant_id")
        with self.assertRaises(HTTPException):
            rag_service.search(self.db, [self.foreign_source.id], "机密")

    def test_agent_treats_foreign_public_bucket_as_read_only(self) -> None:
        agent = Agent(
            tenant_id=self.tenant_a.id,
            name="公开资料阅读助手",
            data_source_ids=[self.public_source.id],
        )
        context = AgentContext(self.db, agent, LLMConfig(name="测试模型"))
        tool_names = {item["function"]["name"] for item in context.build_tools()}
        self.assertIn("search_documents", tool_names)
        self.assertNotIn("save_deliverable", tool_names)
        self.assertIn(
            "不直接执行",
            context.execute_tool("save_deliverable", {"filename": "result.md", "content": "不应写入"}),
        )

    def test_agent_never_exposes_or_executes_direct_skill_or_mcp_side_effects(self) -> None:
        skill = Skill(id="skill-direct", tenant_id=self.tenant_a.id, name="危险技能", path="/tmp/skill")
        mcp = MCPConfig(id="mcp-direct", tenant_id=self.tenant_a.id, name="危险MCP", enabled=True)
        self.db.add_all([skill, mcp])
        self.db.commit()
        agent = Agent(
            tenant_id=self.tenant_a.id,
            name="受控执行助手",
            skill_ids=[skill.id],
            mcp_ids=[mcp.id],
        )
        context = AgentContext(self.db, agent, LLMConfig(name="测试模型"))
        tool_names = {item["function"]["name"] for item in context.build_tools()}
        self.assertNotIn("execute_skill", tool_names)
        self.assertFalse(any(name.startswith("mcp_") for name in tool_names))
        with patch("app.services.agent_engine.skill_service.execute_skill") as execute_skill, patch(
            "app.services.agent_engine.mcp_service.call_tool"
        ) as call_mcp:
            skill_result = context.execute_tool("execute_skill", {"skill_name": skill.name, "args": []})
            mcp_result = context.execute_tool("mcp_危险MCP_写入", {})
        self.assertIn("不直接执行", skill_result)
        self.assertIn("不直接执行", mcp_result)
        execute_skill.assert_not_called()
        call_mcp.assert_not_called()

    def test_agent_search_emits_and_persists_stable_visible_citations(self) -> None:
        rag_service.index_file(self.db, self.private_file)
        self.db.commit()
        agent = Agent(
            id="agent-citation",
            tenant_id=self.tenant_a.id,
            name="资料引用助手",
            data_source_ids=[self.private_source.id, self.foreign_source.id],
        )
        conversation = Conversation(id="conversation-citation", agent_id=agent.id)
        self.db.add_all([agent, conversation])
        self.db.commit()

        responses = iter(
            [
                iter(
                    [
                        {
                            "type": "tool_calls",
                            "tool_calls": [
                                {
                                    "id": "search-1",
                                    "function": {
                                        "name": "search_documents",
                                        "arguments": {"query": "费用审批"},
                                    },
                                }
                            ],
                        }
                    ]
                ),
                iter([{"type": "token", "content": "请在每月 5 日前完成费用复核【C1】。"}]),
            ]
        )
        with patch(
            "app.services.agent_engine.llm_service.chat_stream",
            side_effect=lambda *args, **kwargs: next(responses),
        ):
            events = list(
                run_agent(
                    self.db,
                    agent,
                    LLMConfig(name="测试模型"),
                    [],
                    "费用审批有什么要求？",
                    "",
                    "",
                )
            )

        citations = next(event["data"] for event in events if event["type"] == "citations")
        self.assertEqual(len(citations), 1)
        citation = citations[0]
        self.assertEqual(citation["citation_id"], "C1")
        self.assertEqual(citation["file_id"], self.private_file.id)
        self.assertEqual(citation["data_source_id"], self.private_source.id)
        self.assertNotEqual(citation["data_source_id"], self.foreign_source.id)
        self.assertTrue(citation["chunk_id"])
        self.assertGreater(citation["char_end"], citation["char_start"])

        stored_message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="请在每月 5 日前完成费用复核【C1】。",
            citations=citations,
        )
        self.db.add(stored_message)
        self.db.commit()
        self.db.expire_all()
        restored = self.db.get(Message, stored_message.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.citations, citations)
        self.assertEqual(MessageOut.model_validate(restored).citations, citations)

    def test_read_document_emits_a_file_versioned_citation(self) -> None:
        rag_service.index_file(self.db, self.private_file)
        self.db.commit()
        agent = Agent(
            id="agent-read-document",
            tenant_id=self.tenant_a.id,
            name="资料阅读助手",
            data_source_ids=[self.private_source.id],
        )
        context = AgentContext(self.db, agent, LLMConfig(name="测试模型"))
        payload = json.loads(context.execute_tool("read_document", {"file_id": self.private_file.id}))
        citation = payload["citation"]
        self.assertEqual(citation["file_id"], self.private_file.id)
        self.assertTrue(citation["citation_id"])
        self.assertEqual(citation["file_content_hash"], self.private_file.indexed_content_hash)
        self.assertIn("费用控制", payload["content"])
        self.assertEqual(context.citation_snapshot()[0]["citation_id"], citation["citation_id"])

    def test_index_uses_configured_embedding_runtime_when_available(self) -> None:
        runtime_config = LLMConfig(
            id="embedding-runtime",
            tenant_id=self.tenant_a.id,
            name="专用向量模型",
            model="embed-v1",
            capabilities=["embedding"],
            enabled=True,
        )
        self.db.add(runtime_config)
        self.db.commit()
        with patch(
            "app.services.rag_service.llm_service.embed",
            return_value=[[0.25, 0.75]],
        ) as runtime_embed:
            result = rag_service.index_file(self.db, self.private_file)
        self.assertTrue(result["indexed"])
        runtime_embed.assert_called_once()
        chunk = self.db.scalars(
            select(DocumentChunk).where(DocumentChunk.bucket_file_id == self.private_file.id)
        ).one()
        self.assertEqual(chunk.embedding, [0.25, 0.75])
        self.assertEqual(chunk.embedding_model, "llm:embedding-runtime")

    def test_reindex_reuses_unchanged_content_and_handles_public_sources(self) -> None:
        self.db.info["tenant_id"] = self.tenant_b.id
        first = rag_service.reindex_data_source(self.db, self.public_source)
        self.assertEqual(first["files_indexed"], 1)
        second = rag_service.reindex_data_source(self.db, self.public_source, force=False)
        self.assertEqual(second["files_indexed"], 1)
        self.assertFalse(second["items"][0]["indexed"])
        self.db.info["tenant_id"] = self.tenant_a.id
        results = rag_service.search(self.db, [self.public_source.id], "合同核准")
        self.assertEqual(results[0]["file_id"], self.public_file.id)


if __name__ == "__main__":
    unittest.main()
