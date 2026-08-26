from __future__ import annotations

import copy
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, inspect as sa_inspect, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Agent,
    ArtifactTemplate,
    ArtifactTemplateVersion,
    BucketFile,
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyAction,
    OntologyEntity,
    OntologyProperty,
    OntologyRelation,
    OntologyWorkflow,
    RelationDataMapping,
    Tenant,
)
from app.services import datasource_service
from examples import upgrade_bookkeeping_audit


class BookkeepingUpgradeSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp_dir.name)
        self.source_path = root / "bookkeeping-source.sqlite3"
        self.platform_path = root / "platform.sqlite3"
        self.bucket_root = root / "buckets"
        self._create_business_source(self.source_path)

        self.engine = create_engine(
            f"sqlite:///{self.platform_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def _enable_foreign_keys(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.bucket_patch = patch.object(
            datasource_service,
            "BUCKETS_DIR",
            self.bucket_root,
        )
        self.bucket_patch.start()

        self.tenant = Tenant(id="tenant-bookkeeping-safe", name="代理记账安全测试租户")
        self.scenario = BusinessScenario(
            id="scenario-bookkeeping-safe",
            tenant_id=self.tenant.id,
            name=upgrade_bookkeeping_audit.SCENARIO_NAME,
            description="原始场景说明",
            namespace="bookkeeping",
            status="draft",
        )
        self.source = DataSource(
            id="source-bookkeeping-safe",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="代理记账业务库",
            type="sqlite",
            config={"path": str(self.source_path)},
            status="ok",
        )
        self.bucket = DataSource(
            id="bucket-bookkeeping-safe",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="代理记账文档桶",
            type="file_bucket",
            config={"owner": "existing-user-bucket"},
            status="ok",
        )
        self.customer = OntologyEntity(
            id="entity-bookkeeping-customer",
            scenario_id=self.scenario.id,
            name="客户",
            api_name="base_customer",
            namespace="bookkeeping",
            description="基础客户对象不得被恢复包改写",
        )
        self.customer_id = OntologyProperty(
            id="property-bookkeeping-customer-id",
            entity_id=self.customer.id,
            name="客户ID",
            api_name="base_customer_id",
            data_type="string",
            description="基础主键说明",
            is_key=True,
            is_required=True,
        )
        self.customer_name = OntologyProperty(
            id="property-bookkeeping-customer-name",
            entity_id=self.customer.id,
            name="客户名称",
            api_name="base_customer_name",
            data_type="string",
            description="基础名称说明",
            is_title=False,
        )
        self.customer_mapping = DataMapping(
            id="mapping-bookkeeping-customer",
            scenario_id=self.scenario.id,
            entity_id=self.customer.id,
            data_source_id=self.source.id,
            table_name="customers",
            column_map={"客户ID": "customer_id", "客户名称": "company_name"},
            transform_rules={},
            status="ok",
        )
        self.workflow = OntologyWorkflow(
            id="workflow-bookkeeping-user",
            scenario_id=self.scenario.id,
            name="年度审计流程",
            description="用户已有流程，恢复包不得按名称改写",
            trigger_type="manual",
            trigger_config={"user_contract": True},
            steps=[],
            nodes=[
                {"id": "start", "type": "start", "data": {"label": "开始"}},
                {"id": "end", "type": "end", "data": {"label": "结束"}},
            ],
            edges=[{"id": "e1", "source": "start", "target": "end"}],
            status="active",
            enabled=True,
        )
        self.agent = Agent(
            id="agent-bookkeeping-safe",
            tenant_id=self.tenant.id,
            name=upgrade_bookkeeping_audit.AGENT_NAME,
            description="原始 Agent 说明",
            scenario_id=self.scenario.id,
            system_prompt="原始 Agent 提示",
            data_source_ids=[self.source.id, self.bucket.id],
            capability_scope={
                "functions": {"mode": "explicit", "selected_ids": []},
                "actions": {"mode": "explicit", "selected_ids": []},
                "rules": {"mode": "explicit", "selected_ids": []},
                "events": {"mode": "explicit", "selected_ids": []},
                "workflows": {"mode": "explicit", "selected_ids": []},
            },
            max_tokens=4096,
        )
        self.db.add_all([
            self.tenant,
            self.scenario,
            self.source,
            self.bucket,
            self.customer,
            self.customer_id,
            self.customer_name,
            self.customer_mapping,
            self.workflow,
            self.agent,
        ])
        self.db.commit()
        self.db.expire_all()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.bucket_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _quoted(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    @classmethod
    def _create_business_source(cls, path: Path) -> None:
        table_columns: dict[str, set[str]] = {
            "customers": {"customer_id", "company_name"},
            "audit_projects": set(),
        }
        for spec in upgrade_bookkeeping_audit.AUDIT_OBJECT_SPECS:
            table = str(spec["table"])
            if table == "audit_project_view":
                table = "audit_projects"
            columns = table_columns.setdefault(table, set())
            for _name, _api_name, _type, column, _key, _title in spec["properties"]:
                if table == "audit_projects" and column == "company_name":
                    continue
                columns.add(column)
        with sqlite3.connect(path) as connection:
            for table, columns in table_columns.items():
                definition = ", ".join(
                    f"{cls._quoted(column)} TEXT" for column in sorted(columns)
                )
                connection.execute(
                    f"CREATE TABLE {cls._quoted(table)} ({definition})"
                )

    @staticmethod
    def _snapshot(resource) -> dict:
        return {
            attribute.key: copy.deepcopy(getattr(resource, attribute.key))
            for attribute in sa_inspect(resource).mapper.column_attrs
        }

    def _counts(self) -> dict[str, int]:
        models = {
            "agents": Agent,
            "scenarios": BusinessScenario,
            "sources": DataSource,
            "entities": OntologyEntity,
            "properties": OntologyProperty,
            "mappings": DataMapping,
            "relations": OntologyRelation,
            "relation_mappings": RelationDataMapping,
            "actions": OntologyAction,
            "templates": ArtifactTemplate,
            "template_versions": ArtifactTemplateVersion,
            "files": BucketFile,
            "workflows": OntologyWorkflow,
        }
        return {
            name: int(self.db.scalar(select(func.count()).select_from(model)) or 0)
            for name, model in models.items()
        }

    def _upgrade(self, **overrides):
        values = {
            "agent_id": self.agent.id,
            "scenario_id": self.scenario.id,
            "source_id": self.source.id,
            "file_bucket_id": self.bucket.id,
        }
        values.update(overrides)
        return upgrade_bookkeeping_audit.upgrade(self.db, **values)

    def _source_has_project_view(self) -> bool:
        with sqlite3.connect(
            f"file:{self.source_path.resolve().as_posix()}?mode=ro",
            uri=True,
        ) as connection:
            return connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='view' AND name='audit_project_view'"
            ).fetchone() is not None

    def test_explicit_upgrade_is_owned_non_destructive_and_idempotent(self) -> None:
        customer_before = self._snapshot(self.db.get(OntologyEntity, self.customer.id))
        customer_id_before = self._snapshot(
            self.db.get(OntologyProperty, self.customer_id.id)
        )
        customer_name_before = self._snapshot(
            self.db.get(OntologyProperty, self.customer_name.id)
        )
        customer_mapping_before = self._snapshot(
            self.db.get(DataMapping, self.customer_mapping.id)
        )
        workflow_before = self._snapshot(
            self.db.get(OntologyWorkflow, self.workflow.id)
        )

        first = self._upgrade()
        counts_after_first = self._counts()
        ids_after_first = {
            "entities": set(self.db.scalars(select(OntologyEntity.id).where(
                OntologyEntity.scenario_id == self.scenario.id,
                OntologyEntity.namespace == "bookkeeping_audit",
            ))),
            "relations": set(self.db.scalars(select(OntologyRelation.id).where(
                OntologyRelation.scenario_id == self.scenario.id,
                OntologyRelation.namespace == "bookkeeping_audit",
            ))),
            "actions": set(self.db.scalars(select(OntologyAction.id).where(
                OntologyAction.scenario_id == self.scenario.id,
                OntologyAction.name.in_([
                    spec["name"]
                    for spec in upgrade_bookkeeping_audit.TEMPLATE_ACTION_SPECS
                ]),
            ))),
        }
        second = self._upgrade()

        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(
            {key: value for key, value in first.items() if key != "attempt_id"},
            {key: value for key, value in second.items() if key != "attempt_id"},
        )
        self.assertEqual(self._counts(), counts_after_first)
        self.assertEqual(
            ids_after_first["entities"],
            set(self.db.scalars(select(OntologyEntity.id).where(
                OntologyEntity.scenario_id == self.scenario.id,
                OntologyEntity.namespace == "bookkeeping_audit",
            ))),
        )
        self.assertEqual(
            ids_after_first["relations"],
            set(self.db.scalars(select(OntologyRelation.id).where(
                OntologyRelation.scenario_id == self.scenario.id,
                OntologyRelation.namespace == "bookkeeping_audit",
            ))),
        )
        self.assertEqual(
            ids_after_first["actions"],
            set(self.db.scalars(select(OntologyAction.id).where(
                OntologyAction.scenario_id == self.scenario.id,
                OntologyAction.name.in_([
                    spec["name"]
                    for spec in upgrade_bookkeeping_audit.TEMPLATE_ACTION_SPECS
                ]),
            ))),
        )
        self.assertEqual(len(ids_after_first["entities"]), 8)
        self.assertEqual(len(ids_after_first["relations"]), 8)
        self.assertEqual(len(ids_after_first["actions"]), 3)

        owned_entities = self.db.scalars(select(OntologyEntity).where(
            OntologyEntity.id.in_(ids_after_first["entities"])
        )).all()
        self.assertTrue(all(
            upgrade_bookkeeping_audit._has_marker(item) for item in owned_entities
        ))
        self.assertTrue(all(
            upgrade_bookkeeping_audit._has_marker(prop)
            for entity in owned_entities
            for prop in entity.properties
            if any(
                prop.name == property_spec[0]
                for spec in upgrade_bookkeeping_audit.AUDIT_OBJECT_SPECS
                if spec["name"] == entity.name
                for property_spec in spec["properties"]
            )
        ))
        mappings = self.db.scalars(select(DataMapping).where(
            DataMapping.entity_id.in_(ids_after_first["entities"])
        )).all()
        self.assertTrue(all(
            item.environment_status.get(
                upgrade_bookkeeping_audit.MAPPING_MARKER_KEY
            ) == upgrade_bookkeeping_audit.RECOVERY_PACK_ID
            for item in mappings
        ))
        relations = self.db.scalars(select(OntologyRelation).where(
            OntologyRelation.id.in_(ids_after_first["relations"])
        )).all()
        actions = self.db.scalars(select(OntologyAction).where(
            OntologyAction.id.in_(ids_after_first["actions"])
        )).all()
        templates = self.db.scalars(select(ArtifactTemplate).where(
            ArtifactTemplate.scenario_id == self.scenario.id
        )).all()
        self.assertTrue(all(
            upgrade_bookkeeping_audit._has_marker(item)
            for item in [*relations, *actions, *templates]
        ))
        self.assertTrue(all(
            action.executor_type == "template"
            and action.enabled
            and action.executor_config["target_data_source_id"] == self.bucket.id
            for action in actions
        ))
        self.assertEqual(len(templates), 3)
        self.assertEqual(counts_after_first["files"], 3)
        self.assertTrue(self._source_has_project_view())

        self.assertEqual(
            self._snapshot(self.db.get(OntologyEntity, self.customer.id)),
            customer_before,
        )
        self.assertEqual(
            self._snapshot(self.db.get(OntologyProperty, self.customer_id.id)),
            customer_id_before,
        )
        self.assertEqual(
            self._snapshot(self.db.get(OntologyProperty, self.customer_name.id)),
            customer_name_before,
        )
        self.assertEqual(
            self._snapshot(self.db.get(DataMapping, self.customer_mapping.id)),
            customer_mapping_before,
        )
        self.assertEqual(
            self._snapshot(self.db.get(OntologyWorkflow, self.workflow.id)),
            workflow_before,
        )
        self.assertEqual(
            self.db.get(DataSource, self.bucket.id).config,
            {"owner": "existing-user-bucket"},
        )

    def test_target_and_customer_mapping_ownership_is_global_per_entity(self) -> None:
        other_source = DataSource(
            id="source-bookkeeping-other",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="用户其他业务库",
            type="sqlite",
            config={"path": str(self.source_path)},
            status="ok",
        )
        spec = upgrade_bookkeeping_audit.AUDIT_OBJECT_SPECS[0]
        target = OntologyEntity(
            id="entity-bookkeeping-cross-source-target",
            scenario_id=self.scenario.id,
            name=spec["name"],
            api_name=spec["api_name"],
            namespace="bookkeeping_audit",
            description=upgrade_bookkeeping_audit._marked_description(
                spec["description"]
            ),
        )
        cross_source_mapping = DataMapping(
            id="mapping-bookkeeping-cross-source-target",
            scenario_id=self.scenario.id,
            entity_id=target.id,
            data_source_id=other_source.id,
            table_name=spec["table"],
            environment_status={
                upgrade_bookkeeping_audit.MAPPING_MARKER_KEY:
                    upgrade_bookkeeping_audit.RECOVERY_PACK_ID,
            },
        )
        self.db.add_all([other_source, target, cross_source_mapping])
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "指向其他数据源"):
            self._upgrade()
        self.assertFalse(self._source_has_project_view())
        self.assertFalse(self.bucket_root.exists())

        self.db.delete(cross_source_mapping)
        self.db.delete(target)
        self.db.commit()
        duplicate_customer_mapping = DataMapping(
            id="mapping-bookkeeping-customer-other-source",
            scenario_id=self.scenario.id,
            entity_id=self.customer.id,
            data_source_id=other_source.id,
            table_name="customers",
            column_map={"客户ID": "customer_id", "客户名称": "company_name"},
            transform_rules={},
            status="ok",
        )
        self.db.add(duplicate_customer_mapping)
        self.db.commit()
        customer_before = self._snapshot(self.customer_mapping)

        with self.assertRaisesRegex(RuntimeError, "客户.*全源唯一"):
            self._upgrade()
        self.assertEqual(
            self._snapshot(self.db.get(DataMapping, self.customer_mapping.id)),
            customer_before,
        )
        self.assertFalse(self._source_has_project_view())
        self.assertFalse(self.bucket_root.exists())

    def test_upgrade_mutex_uses_postgres_locks_and_rejects_unknown_dialect(self) -> None:
        statements: list[tuple[str, str]] = []

        class FakePostgresSession:
            @staticmethod
            def get_bind():
                return SimpleNamespace(
                    dialect=SimpleNamespace(name="postgresql")
                )

            @staticmethod
            def execute(statement, _params=None):
                statements.append(("execute", str(statement)))
                return SimpleNamespace(rowcount=1)

            @staticmethod
            def scalar(statement):
                statements.append(("scalar", str(statement)))
                return "locked"

        dialect = upgrade_bookkeeping_audit._acquire_upgrade_mutex(
            FakePostgresSession(),
            self.agent,
            self.scenario,
            self.source,
            self.bucket,
        )
        self.assertEqual(dialect, "postgresql")
        self.assertEqual(
            statements[0],
            (
                "execute",
                "LOCK TABLE data_mappings IN SHARE ROW EXCLUSIVE MODE",
            ),
        )
        row_locks = [sql for kind, sql in statements if kind == "scalar"]
        self.assertEqual(len(row_locks), 4)
        self.assertIn("FROM agents", row_locks[0])
        self.assertIn("FROM business_scenarios", row_locks[1])
        self.assertIn("FROM data_sources", row_locks[2])
        self.assertIn("FROM data_sources", row_locks[3])
        self.assertTrue(all("FOR UPDATE" in sql for sql in row_locks))

        class FakeMysqlSession:
            @staticmethod
            def get_bind():
                return SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

        with self.assertRaisesRegex(
            RuntimeError,
            "不支持平台数据库方言.*仅 SQLite 与 PostgreSQL",
        ):
            upgrade_bookkeeping_audit._acquire_upgrade_mutex(
                FakeMysqlSession(),
                self.agent,
                self.scenario,
                self.source,
                self.bucket,
            )

    def test_attempt_marker_and_commit_verifier_are_bound_to_current_attempt(self) -> None:
        self.assertEqual(
            upgrade_bookkeeping_audit._verify_platform_commit_after_error(
                self.db,
                agent_id=self.agent.id,
                scenario_id=self.scenario.id,
                source_id=self.source.id,
                bucket_id=self.bucket.id,
                attempt_id="attempt-that-was-never-written",
            ),
            upgrade_bookkeeping_audit.PLATFORM_COMMIT_NOT_COMMITTED,
        )
        real_commit = self.db.commit

        def commit_then_lose_acknowledgement() -> None:
            real_commit()
            raise RuntimeError("lost bookkeeping commit acknowledgement")

        with patch.object(
            self.db,
            "commit",
            side_effect=commit_then_lose_acknowledgement,
        ):
            result = self._upgrade()

        self.assertTrue(self._source_has_project_view())
        self.assertEqual(
            upgrade_bookkeeping_audit._verify_platform_commit_after_error(
                self.db,
                agent_id=self.agent.id,
                scenario_id=self.scenario.id,
                source_id=self.source.id,
                bucket_id=self.bucket.id,
                attempt_id=result["attempt_id"],
            ),
            upgrade_bookkeeping_audit.PLATFORM_COMMIT_CONFIRMED,
        )
        self.assertEqual(
            upgrade_bookkeeping_audit._verify_platform_commit_after_error(
                self.db,
                agent_id=self.agent.id,
                scenario_id=self.scenario.id,
                source_id=self.source.id,
                bucket_id=self.bucket.id,
                attempt_id="different-new-attempt",
            ),
            upgrade_bookkeeping_audit.PLATFORM_COMMIT_NOT_COMMITTED,
        )
        target_mappings = list(self.db.scalars(select(DataMapping).where(
            DataMapping.scenario_id == self.scenario.id,
            DataMapping.entity_id != self.customer.id,
        )))
        self.assertEqual(
            {
                dict(mapping.environment_status or {}).get(
                    upgrade_bookkeeping_audit.MAPPING_ATTEMPT_KEY
                )
                for mapping in target_mappings
            },
            {result["attempt_id"]},
        )
        self.assertNotIn(
            upgrade_bookkeeping_audit.MAPPING_ATTEMPT_KEY,
            self.db.get(DataMapping, self.customer_mapping.id).environment_status,
        )
        project = self.db.scalar(select(OntologyEntity).where(
            OntologyEntity.scenario_id == self.scenario.id,
            OntologyEntity.api_name == "audit_project",
        ))
        project.name = "并发改名后的首个对象"
        project.api_name = "concurrently_renamed_first_entity"
        self.db.commit()
        self.assertEqual(
            upgrade_bookkeeping_audit._verify_platform_commit_after_error(
                self.db,
                agent_id=self.agent.id,
                scenario_id=self.scenario.id,
                source_id=self.source.id,
                bucket_id=self.bucket.id,
                attempt_id=result["attempt_id"],
            ),
            upgrade_bookkeeping_audit.PLATFORM_COMMIT_UNKNOWN,
        )

    def test_commit_ack_loss_with_concurrent_identity_damage_is_unknown(self) -> None:
        real_commit = self.db.commit

        def commit_then_concurrently_rename_first_entity() -> None:
            real_commit()
            with Session(self.engine, expire_on_commit=False) as concurrent_db:
                project = concurrent_db.scalar(select(OntologyEntity).where(
                    OntologyEntity.scenario_id == self.scenario.id,
                    OntologyEntity.api_name == "audit_project",
                ))
                project.name = "回执丢失后并发改名的首个对象"
                project.api_name = "renamed_during_lost_ack"
                concurrent_db.commit()
            raise RuntimeError("lost ack after concurrent identity damage")

        with patch.object(
            self.db,
            "commit",
            side_effect=commit_then_concurrently_rename_first_entity,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "lost ack after concurrent identity damage",
            ) as raised:
                self._upgrade()

        self.assertTrue(self._source_has_project_view())
        self.assertEqual(
            len([path for path in self.bucket_root.rglob("*") if path.is_file()]),
            3,
        )
        self.assertTrue(any(
            "平台提交结果无法确认" in note
            for note in getattr(raised.exception, "__notes__", [])
        ))
        target_mappings = list(self.db.scalars(select(DataMapping).where(
            DataMapping.scenario_id == self.scenario.id,
            DataMapping.entity_id != self.customer.id,
        )))
        self.assertEqual(
            len({
                dict(mapping.environment_status or {}).get(
                    upgrade_bookkeeping_audit.MAPPING_ATTEMPT_KEY
                )
                for mapping in target_mappings
            }),
            1,
        )

    def test_unknown_commit_retains_additive_view_and_new_template_files(self) -> None:
        with (
            patch.object(
                upgrade_bookkeeping_audit,
                "_verify_platform_commit_after_error",
                return_value=upgrade_bookkeeping_audit.PLATFORM_COMMIT_UNKNOWN,
            ) as verifier,
            patch.object(
                self.db,
                "commit",
                side_effect=RuntimeError("ambiguous bookkeeping commit"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "ambiguous bookkeeping commit",
            ) as raised:
                self._upgrade()

        verifier.assert_called_once()
        self.assertTrue(self._source_has_project_view())
        retained_files = [
            path for path in self.bucket_root.rglob("*") if path.is_file()
        ]
        self.assertEqual(len(retained_files), 3)
        self.assertTrue(any(
            "已保留本次新增视图和模板文件" in note
            for note in getattr(raised.exception, "__notes__", [])
        ))
        # A later operator-confirmed retry adopts the same stable paths.
        result = self._upgrade()
        self.assertEqual(result["bucket_id"], self.bucket.id)
        self.assertEqual(
            [path for path in self.bucket_root.rglob("*") if path.is_file()],
            retained_files,
        )

    def test_two_sessions_serialize_source_adoption_until_compensation(self) -> None:
        session_a = Session(self.engine, expire_on_commit=False)
        session_b = Session(self.engine, expire_on_commit=False)
        verifier_entered = threading.Event()
        allow_compensation = threading.Event()
        cleanup_entered = threading.Event()
        allow_file_cleanup = threading.Event()
        results: dict[str, object] = {}
        real_cleanup = (
            upgrade_bookkeeping_audit._cleanup_created_template_files
        )

        def reject_commit() -> None:
            raise RuntimeError("worker A platform commit rejected")

        def blocked_verifier(*_args, **_kwargs) -> str:
            verifier_entered.set()
            if not allow_compensation.wait(10):
                return upgrade_bookkeeping_audit.PLATFORM_COMMIT_UNKNOWN
            return upgrade_bookkeeping_audit.PLATFORM_COMMIT_NOT_COMMITTED

        def blocked_cleanup(paths) -> None:
            cleanup_entered.set()
            if not allow_file_cleanup.wait(10):
                raise RuntimeError("timed out waiting to verify compensation lock")
            real_cleanup(paths)

        def run_a() -> None:
            try:
                with patch.object(session_a, "commit", side_effect=reject_commit):
                    upgrade_bookkeeping_audit.upgrade(
                        session_a,
                        agent_id=self.agent.id,
                        scenario_id=self.scenario.id,
                        source_id=self.source.id,
                        file_bucket_id=self.bucket.id,
                    )
            except Exception as exc:
                results["a_error"] = exc

        def run_b() -> None:
            try:
                results["b_result"] = upgrade_bookkeeping_audit.upgrade(
                    session_b,
                    agent_id=self.agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                    file_bucket_id=self.bucket.id,
                )
            except Exception as exc:
                results["b_error"] = exc

        try:
            with patch.object(
                upgrade_bookkeeping_audit,
                "_verify_platform_commit_after_error",
                side_effect=blocked_verifier,
            ), patch.object(
                upgrade_bookkeeping_audit,
                "_cleanup_created_template_files",
                side_effect=blocked_cleanup,
            ):
                worker_a = threading.Thread(target=run_a)
                worker_a.start()
                self.assertTrue(verifier_entered.wait(10))
                worker_b = threading.Thread(target=run_b)
                worker_b.start()
                time.sleep(0.3)
                self.assertTrue(worker_b.is_alive())
                allow_compensation.set()
                self.assertTrue(cleanup_entered.wait(10))
                time.sleep(0.3)
                self.assertTrue(worker_b.is_alive())
                self.assertNotIn("b_result", results)
                allow_file_cleanup.set()
                worker_a.join(15)
                worker_b.join(15)
                self.assertFalse(worker_a.is_alive())
                self.assertFalse(worker_b.is_alive())
        finally:
            allow_compensation.set()
            allow_file_cleanup.set()
            session_a.close()
            session_b.close()
        self.assertIsInstance(results.get("a_error"), RuntimeError)
        self.assertNotIn("b_error", results)
        self.assertIn("b_result", results)
        self.assertTrue(self._source_has_project_view())

    def test_ambiguous_agent_without_agent_id_fails_before_changes(self) -> None:
        duplicate = Agent(
            id="agent-bookkeeping-duplicate",
            tenant_id=self.tenant.id,
            name=upgrade_bookkeeping_audit.AGENT_NAME,
            scenario_id=self.scenario.id,
            data_source_ids=[self.source.id, self.bucket.id],
        )
        self.db.add(duplicate)
        self.db.commit()
        counts_before = self._counts()

        with self.assertRaisesRegex(RuntimeError, "存在多个候选"):
            upgrade_bookkeeping_audit.upgrade(
                self.db,
                scenario_id=self.scenario.id,
                source_id=self.source.id,
                file_bucket_id=self.bucket.id,
            )

        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self._source_has_project_view())

    def test_ambiguous_source_and_file_bucket_fail_closed_with_rollback(self) -> None:
        duplicate_source = DataSource(
            id="source-bookkeeping-duplicate",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="代理记账业务库副本",
            type="sqlite",
            config={"path": str(self.source_path)},
            status="ok",
        )
        duplicate_bucket = DataSource(
            id="bucket-bookkeeping-duplicate",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="代理记账文档桶副本",
            type="file_bucket",
            config={},
            status="ok",
        )
        self.db.add_all([duplicate_source, duplicate_bucket])
        self.agent.data_source_ids = [
            self.source.id,
            duplicate_source.id,
            self.bucket.id,
            duplicate_bucket.id,
        ]
        self.db.commit()
        counts_before = self._counts()

        with self.assertRaisesRegex(RuntimeError, "SQLite 数据源存在多个候选"):
            upgrade_bookkeeping_audit.upgrade(
                self.db,
                agent_id=self.agent.id,
                scenario_id=self.scenario.id,
                file_bucket_id=self.bucket.id,
            )
        self.assertEqual(self._counts(), counts_before)

        with self.assertRaisesRegex(RuntimeError, "文件桶存在多个候选"):
            upgrade_bookkeeping_audit.upgrade(
                self.db,
                agent_id=self.agent.id,
                scenario_id=self.scenario.id,
                source_id=self.source.id,
            )
        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self._source_has_project_view())

        duplicate_source_before = self._snapshot(duplicate_source)
        duplicate_bucket_before = self._snapshot(duplicate_bucket)
        result = self._upgrade()
        self.assertEqual(result["source_id"], self.source.id)
        self.assertEqual(result["bucket_id"], self.bucket.id)
        self.assertEqual(
            self._snapshot(self.db.get(DataSource, duplicate_source.id)),
            duplicate_source_before,
        )
        self.assertEqual(
            self._snapshot(self.db.get(DataSource, duplicate_bucket.id)),
            duplicate_bucket_before,
        )

    def test_explicit_unbound_source_and_bucket_are_rejected(self) -> None:
        self.agent.data_source_ids = [self.bucket.id]
        self.db.commit()
        counts_before = self._counts()
        with self.assertRaisesRegex(RuntimeError, "未绑定到所选 Agent"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self._source_has_project_view())

        self.agent.data_source_ids = [self.source.id]
        self.db.commit()
        counts_before = self._counts()
        with self.assertRaisesRegex(RuntimeError, "未绑定到所选 Agent"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self._source_has_project_view())

    def test_explicit_ids_must_cross_validate_scenario_tenant_and_type(self) -> None:
        other_scenario = BusinessScenario(
            id="scenario-bookkeeping-other",
            tenant_id=self.tenant.id,
            name=upgrade_bookkeeping_audit.SCENARIO_NAME,
            namespace="bookkeeping",
            status="draft",
        )
        self.db.add(other_scenario)
        self.db.commit()
        counts_before = self._counts()

        with self.assertRaisesRegex(RuntimeError, "与所选场景.*不一致"):
            self._upgrade(scenario_id=other_scenario.id)
        with self.assertRaisesRegex(RuntimeError, "类型必须为 sqlite"):
            self._upgrade(source_id=self.bucket.id)
        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self._source_has_project_view())

        other_tenant = Tenant(
            id="tenant-bookkeeping-other",
            name="其他租户",
        )
        self.db.add(other_tenant)
        self.source.tenant_id = other_tenant.id
        self.db.commit()
        counts_before = self._counts()
        with self.assertRaisesRegex(RuntimeError, "与场景租户不一致"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self._source_has_project_view())

    def test_unmarked_entity_relation_and_catalog_name_conflicts_fail_closed(self) -> None:
        entity_conflict = OntologyEntity(
            id="entity-bookkeeping-user-conflict",
            scenario_id=self.scenario.id,
            name=upgrade_bookkeeping_audit.AUDIT_OBJECT_SPECS[0]["name"],
            api_name="user_owned_audit_project",
            namespace="custom",
            description="用户自建同名对象",
        )
        self.db.add(entity_conflict)
        self.db.commit()
        self.db.expire_all()
        counts_before = self._counts()
        snapshot_before = self._snapshot(
            self.db.get(OntologyEntity, entity_conflict.id)
        )
        with self.assertRaisesRegex(RuntimeError, "对象.*未标记资源占用"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(
            self._snapshot(self.db.get(OntologyEntity, entity_conflict.id)),
            snapshot_before,
        )
        self.assertFalse(self._source_has_project_view())

        self.db.delete(entity_conflict)
        self.db.commit()
        relation_spec = upgrade_bookkeeping_audit.AUDIT_RELATION_SPECS[0]
        relation_conflict = OntologyRelation(
            id="relation-bookkeeping-user-conflict",
            scenario_id=self.scenario.id,
            name=relation_spec["name"],
            api_name="user_owned_relation",
            source_entity_id=self.customer.id,
            target_entity_id=self.customer.id,
            description="用户自建同名关系",
        )
        self.db.add(relation_conflict)
        self.db.commit()
        self.db.expire_all()
        counts_before = self._counts()
        snapshot_before = self._snapshot(
            self.db.get(OntologyRelation, relation_conflict.id)
        )
        with self.assertRaisesRegex(RuntimeError, "关系.*未标记资源占用"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(
            self._snapshot(self.db.get(OntologyRelation, relation_conflict.id)),
            snapshot_before,
        )
        self.assertFalse(self._source_has_project_view())

        self.db.delete(relation_conflict)
        self.db.commit()
        template_spec = upgrade_bookkeeping_audit.TEMPLATE_ACTION_SPECS[0]
        catalog_conflict = ArtifactTemplate(
            id="template-bookkeeping-user-conflict",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            key="user_owned_template_key",
            name=template_spec["template_name"],
            purpose="用户模板",
            description="用户自建同名模板目录",
            status="active",
        )
        self.db.add(catalog_conflict)
        self.db.commit()
        self.db.expire_all()
        counts_before = self._counts()
        snapshot_before = self._snapshot(
            self.db.get(ArtifactTemplate, catalog_conflict.id)
        )
        with self.assertRaisesRegex(RuntimeError, "模板目录标识.*未标记资源占用"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(
            self._snapshot(self.db.get(ArtifactTemplate, catalog_conflict.id)),
            snapshot_before,
        )
        self.assertFalse(self._source_has_project_view())
        self.assertFalse(self.bucket_root.exists())

        self.db.delete(catalog_conflict)
        other_scenario = BusinessScenario(
            id="scenario-bookkeeping-template-owner",
            tenant_id=self.tenant.id,
            name=upgrade_bookkeeping_audit.SCENARIO_NAME,
            namespace="bookkeeping_audit",
            status="draft",
        )
        self.db.add(other_scenario)
        self.db.flush()
        marked_cross_scenario = ArtifactTemplate(
            id="template-bookkeeping-cross-scenario",
            tenant_id=self.tenant.id,
            scenario_id=other_scenario.id,
            key=upgrade_bookkeeping_audit._catalog_key(template_spec),
            name=template_spec["template_name"],
            purpose=template_spec["name"],
            description=upgrade_bookkeeping_audit.RECOVERY_MARKER,
            status="active",
        )
        self.db.add(marked_cross_scenario)
        self.db.commit()
        counts_before = self._counts()
        with self.assertRaisesRegex(RuntimeError, "与所选场景绑定不一致"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self._source_has_project_view())
        self.assertFalse(self.bucket_root.exists())

    def test_invalid_base_customer_mapping_is_rejected_without_rewrite(self) -> None:
        self.customer_mapping.table_name = "user_customer_shadow"
        self.customer_mapping.column_map = {"客户ID": "wrong_customer_id"}
        self.db.commit()
        mapping_before = self._snapshot(self.customer_mapping)
        counts_before = self._counts()

        with self.assertRaisesRegex(RuntimeError, "客户.*只读年度审计契约"):
            self._upgrade()

        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(
            self._snapshot(self.db.get(DataMapping, self.customer_mapping.id)),
            mapping_before,
        )
        self.assertFalse(self._source_has_project_view())
        self.assertFalse(self.bucket_root.exists())

    def test_existing_project_view_must_match_exact_contract(self) -> None:
        with sqlite3.connect(self.source_path) as connection:
            connection.execute(
                "CREATE VIEW audit_project_view AS "
                "SELECT p.*, c.company_name FROM audit_projects p "
                "CROSS JOIN customers c"
            )
        counts_before = self._counts()
        with self.assertRaisesRegex(RuntimeError, "非恢复包契约对象占用"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertTrue(self._source_has_project_view())
        self.assertFalse(self.bucket_root.exists())

    def test_project_view_name_cannot_be_a_table(self) -> None:
        columns = {
            column
            for spec in upgrade_bookkeeping_audit.AUDIT_OBJECT_SPECS
            if spec["table"] == "audit_project_view"
            for _name, _api, _kind, column, _key, _title in spec["properties"]
        }
        definition = ", ".join(
            f"{self._quoted(column)} TEXT" for column in sorted(columns)
        )
        with sqlite3.connect(self.source_path) as connection:
            connection.execute(
                f"CREATE TABLE audit_project_view ({definition})"
            )
        counts_before = self._counts()
        with self.assertRaisesRegex(RuntimeError, "非恢复包契约对象占用"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self.bucket_root.exists())

    def test_unmarked_same_name_bucket_file_is_not_reused(self) -> None:
        asset = (
            upgrade_bookkeeping_audit.ASSET_DIR
            / upgrade_bookkeeping_audit.TEMPLATE_ACTION_SPECS[0]["asset"]
        )
        user_file = datasource_service.save_bucket_file(
            self.bucket,
            asset.name,
            asset.read_bytes(),
            mime="application/octet-stream",
        )
        user_file.status = "pending"
        user_file.error = "用户附件状态"
        self.db.add(user_file)
        self.db.commit()
        user_file_id = user_file.id
        self.db.expire_all()
        persisted_user_file = self.db.get(BucketFile, user_file_id)
        file_before = self._snapshot(persisted_user_file)
        bytes_before = Path(persisted_user_file.stored_path).read_bytes()
        counts_before = self._counts()

        with self.assertRaisesRegex(RuntimeError, "同名模板.*未标记附件占用"):
            self._upgrade()

        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(
            self._snapshot(self.db.get(BucketFile, user_file_id)),
            file_before,
        )
        self.assertEqual(
            Path(self.db.get(BucketFile, user_file_id).stored_path).read_bytes(),
            bytes_before,
        )
        self.assertFalse(self._source_has_project_view())

    def test_late_failures_compensate_files_view_and_platform_transaction(self) -> None:
        counts_before = self._counts()
        agent_before = self._snapshot(self.db.get(Agent, self.agent.id))
        scenario_before = self._snapshot(
            self.db.get(BusinessScenario, self.scenario.id)
        )

        with patch.object(
            upgrade_bookkeeping_audit,
            "_ensure_project_view",
            side_effect=RuntimeError("forced view failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced view failure"):
                self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self._source_has_project_view())
        self.assertEqual(
            [path for path in self.bucket_root.rglob("*") if path.is_file()]
            if self.bucket_root.exists()
            else [],
            [],
        )

        with patch.object(
            self.db,
            "commit",
            side_effect=RuntimeError("forced platform commit failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced platform commit failure"):
                self._upgrade()
        self.db.expire_all()
        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(
            self._snapshot(self.db.get(Agent, self.agent.id)),
            agent_before,
        )
        self.assertEqual(
            self._snapshot(self.db.get(BusinessScenario, self.scenario.id)),
            scenario_before,
        )
        self.assertFalse(self._source_has_project_view())
        self.assertEqual(
            [path for path in self.bucket_root.rglob("*") if path.is_file()]
            if self.bucket_root.exists()
            else [],
            [],
        )

    def test_unmarked_legacy_property_and_mapping_extensions_are_rejected(self) -> None:
        self._upgrade()
        project = self.db.scalar(select(OntologyEntity).where(
            OntologyEntity.scenario_id == self.scenario.id,
            OntologyEntity.api_name == "audit_project",
        ))
        project.description = upgrade_bookkeeping_audit.AUDIT_OBJECT_SPECS[0][
            "description"
        ]
        for prop in project.properties:
            prop.description = ""
        project.properties[0].is_sensitive = True
        self.db.commit()
        property_before = self._snapshot(project.properties[0])
        counts_before = self._counts()

        with self.assertRaisesRegex(RuntimeError, "对象.*未标记资源占用"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(
            self._snapshot(
                self.db.get(OntologyProperty, project.properties[0].id)
            ),
            property_before,
        )

        project.description = upgrade_bookkeeping_audit._marked_description(
            project.description
        )
        project.properties[0].is_sensitive = False
        project.properties[0].description = (
            upgrade_bookkeeping_audit.RECOVERY_MARKER
        )
        mapping = self.db.scalar(select(DataMapping).where(
            DataMapping.entity_id == project.id,
            DataMapping.data_source_id == self.source.id,
        ))
        states = dict(mapping.environment_status or {})
        states.pop(upgrade_bookkeeping_audit.MAPPING_MARKER_KEY, None)
        mapping.environment_status = states
        mapping.data_source_binding_ref = {"user_binding": True}
        self.db.commit()
        mapping_before = self._snapshot(mapping)
        counts_before = self._counts()

        with self.assertRaisesRegex(RuntimeError, "数据映射未标记"):
            self._upgrade()
        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(
            self._snapshot(self.db.get(DataMapping, mapping.id)),
            mapping_before,
        )

    def test_unmarked_same_name_action_conflict_rolls_back_everything(self) -> None:
        conflict = OntologyAction(
            id="action-bookkeeping-user-conflict",
            scenario_id=self.scenario.id,
            entity_id=self.customer.id,
            name=upgrade_bookkeeping_audit.TEMPLATE_ACTION_SPECS[0]["name"],
            description="用户自建的同名 SQL 操作，即便停用也不能认领",
            input_schema={"type": "object", "properties": {}},
            executor_type="sql",
            executor_config={"sql": "SELECT 1"},
            enabled=False,
            requires_confirmation=False,
            idempotency_required=False,
        )
        self.db.add(conflict)
        self.db.commit()
        self.db.expire_all()
        counts_before = self._counts()
        conflict_before = self._snapshot(conflict)
        agent_before = self._snapshot(self.db.get(Agent, self.agent.id))

        with self.assertRaisesRegex(RuntimeError, "未标记资源占用"):
            self._upgrade()

        self.db.expire_all()
        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(
            self._snapshot(self.db.get(OntologyAction, conflict.id)),
            conflict_before,
        )
        self.assertEqual(
            self._snapshot(self.db.get(Agent, self.agent.id)),
            agent_before,
        )
        self.assertFalse(self._source_has_project_view())
        self.assertFalse(self.bucket_root.exists())

    def test_exact_unmarked_legacy_pack_is_adopted_and_remains_stable(self) -> None:
        first = self._upgrade()
        workpaper = self.db.scalar(select(OntologyEntity).where(
            OntologyEntity.scenario_id == self.scenario.id,
            OntologyEntity.api_name == "audit_workpaper",
        ))
        extension = OntologyProperty(
            id="property-user-workpaper-extension",
            entity_id=workpaper.id,
            name="用户扩展字段",
            api_name="user_extension",
            data_type="string",
            description="旧模型上的用户扩展不得删除或改写",
            constraints={"maxLength": 64},
        )
        self.db.add(extension)
        self.db.commit()
        extension_before = self._snapshot(extension)
        counts_before_adoption = self._counts()
        stable_ids = {
            "entities": set(self.db.scalars(select(OntologyEntity.id).where(
                OntologyEntity.scenario_id == self.scenario.id,
                OntologyEntity.namespace == "bookkeeping_audit",
            ))),
            "relations": set(self.db.scalars(select(OntologyRelation.id).where(
                OntologyRelation.scenario_id == self.scenario.id,
                OntologyRelation.namespace == "bookkeeping_audit",
            ))),
            "actions": set(self.db.scalars(select(OntologyAction.id).where(
                OntologyAction.scenario_id == self.scenario.id,
                OntologyAction.name.in_([
                    spec["name"]
                    for spec in upgrade_bookkeeping_audit.TEMPLATE_ACTION_SPECS
                ]),
            ))),
        }
        specs_by_name = {
            spec["name"]: spec
            for spec in upgrade_bookkeeping_audit.AUDIT_OBJECT_SPECS
        }
        for entity in self.db.scalars(select(OntologyEntity).where(
            OntologyEntity.id.in_(stable_ids["entities"])
        )):
            entity.description = specs_by_name[entity.name]["description"]
            expected_names = {
                item[0] for item in specs_by_name[entity.name]["properties"]
            }
            for prop in entity.properties:
                if prop.name in expected_names:
                    prop.description = ""
            mapping = self.db.scalar(select(DataMapping).where(
                DataMapping.entity_id == entity.id,
                DataMapping.data_source_id == self.source.id,
            ))
            states = dict(mapping.environment_status or {})
            states.pop(upgrade_bookkeeping_audit.MAPPING_MARKER_KEY, None)
            mapping.environment_status = states
        relation_specs = {
            spec["name"]: spec
            for spec in upgrade_bookkeeping_audit.AUDIT_RELATION_SPECS
        }
        for relation in self.db.scalars(select(OntologyRelation).where(
            OntologyRelation.id.in_(stable_ids["relations"])
        )):
            spec = relation_specs[relation.name]
            relation.description = f"{spec['source']}到{spec['target']}的双向可导航链接。"
        action_specs = {
            spec["name"]: spec
            for spec in upgrade_bookkeeping_audit.TEMPLATE_ACTION_SPECS
        }
        for action in self.db.scalars(select(OntologyAction).where(
            OntologyAction.id.in_(stable_ids["actions"])
        )):
            action.description = action_specs[action.name]["description"]
        for template in self.db.scalars(select(ArtifactTemplate).where(
            ArtifactTemplate.scenario_id == self.scenario.id
        )):
            template.description = action_specs[template.purpose]["description"]
        self.db.commit()

        adopted = self._upgrade()
        counts_after_adoption = self._counts()
        repeated = self._upgrade()

        self.assertEqual(
            {key: value for key, value in first.items() if key != "attempt_id"},
            {key: value for key, value in adopted.items() if key != "attempt_id"},
        )
        self.assertEqual(
            {key: value for key, value in adopted.items() if key != "attempt_id"},
            {key: value for key, value in repeated.items() if key != "attempt_id"},
        )
        self.assertEqual(
            len({first["attempt_id"], adopted["attempt_id"], repeated["attempt_id"]}),
            3,
        )
        self.assertEqual(counts_before_adoption, counts_after_adoption)
        self.assertEqual(counts_after_adoption, self._counts())
        self.assertEqual(
            self._snapshot(self.db.get(OntologyProperty, extension.id)),
            extension_before,
        )
        self.assertEqual(
            stable_ids["entities"],
            set(self.db.scalars(select(OntologyEntity.id).where(
                OntologyEntity.scenario_id == self.scenario.id,
                OntologyEntity.namespace == "bookkeeping_audit",
            ))),
        )
        owned = [
            *self.db.scalars(select(OntologyEntity).where(
                OntologyEntity.id.in_(stable_ids["entities"])
            )).all(),
            *self.db.scalars(select(OntologyRelation).where(
                OntologyRelation.id.in_(stable_ids["relations"])
            )).all(),
            *self.db.scalars(select(OntologyAction).where(
                OntologyAction.id.in_(stable_ids["actions"])
            )).all(),
            *self.db.scalars(select(ArtifactTemplate).where(
                ArtifactTemplate.scenario_id == self.scenario.id
            )).all(),
        ]
        self.assertTrue(all(
            upgrade_bookkeeping_audit._has_marker(item) for item in owned
        ))
        self.assertTrue(all(
            item.description.splitlines().count(
                upgrade_bookkeeping_audit.RECOVERY_MARKER
            ) == 1
            for item in owned
        ))


if __name__ == "__main__":
    unittest.main()
