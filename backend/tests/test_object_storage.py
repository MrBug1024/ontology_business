from __future__ import annotations

import asyncio
from contextlib import nullcontext
import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import UploadFile
from starlette.datastructures import Headers

from app.models import AssistantAttachment, BucketFile, DataSource
from app.routers import assistant as assistant_router
from app.services import datasource_service, doc_parser, object_storage_service


class _ObjectResponse(BytesIO):
    def release_conn(self) -> None:
        return None


class _NotFoundError(Exception):
    code = "NoSuchKey"


class _NotImplementedError(Exception):
    code = "NotImplemented"
    status = 501


class _FakeMinio:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.versioning_status = ""
        self.versioning_not_implemented = False

    def bucket_exists(self, bucket_name: str) -> bool:
        return bucket_name in self.buckets

    def make_bucket(self, bucket_name: str) -> None:
        self.buckets.add(bucket_name)

    def get_bucket_versioning(self, _bucket_name: str):
        return SimpleNamespace(status=self.versioning_status)

    def set_bucket_versioning(self, _bucket_name: str, config) -> None:
        if self.versioning_not_implemented:
            raise _NotImplementedError()
        self.versioning_status = str(config.status or "")

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data,
        length: int,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ):
        content = data.read(length)
        versions = self.objects.setdefault((bucket_name, object_name), [])
        version_id = (
            f"v{len(versions) + 1}"
            if self.versioning_status == "Enabled"
            else ""
        )
        etag = hashlib.sha256(content).hexdigest()
        record = {
            "content": content,
            "content_type": content_type,
            "metadata": dict(metadata or {}),
            "version_id": version_id,
            "etag": etag,
        }
        if version_id:
            versions.append(record)
        else:
            self.objects[(bucket_name, object_name)] = [record]
        return SimpleNamespace(
            size=len(content),
            etag=etag,
            version_id=version_id,
            content_type=content_type,
        )

    def _record(self, bucket_name: str, object_name: str, version_id: str | None):
        versions = self.objects.get((bucket_name, object_name), [])
        if version_id:
            for record in versions:
                if record["version_id"] == version_id:
                    return record
        elif versions:
            return versions[-1]
        raise _NotFoundError()

    def stat_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        version_id: str | None = None,
    ):
        record = self._record(bucket_name, object_name, version_id)
        return SimpleNamespace(
            size=len(record["content"]),
            etag=record["etag"],
            version_id=record["version_id"],
            content_type=record["content_type"],
        )

    def get_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        version_id: str | None = None,
    ):
        record = self._record(bucket_name, object_name, version_id)
        return _ObjectResponse(record["content"])

    def remove_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        version_id: str | None = None,
    ) -> None:
        key = (bucket_name, object_name)
        versions = self.objects.get(key, [])
        if version_id:
            self.objects[key] = [
                record for record in versions if record["version_id"] != version_id
            ]
            if not self.objects[key]:
                self.objects.pop(key, None)
        else:
            self.objects.pop(key, None)

    def list_objects(
        self,
        bucket_name: str,
        *,
        prefix: str,
        recursive: bool,
        include_version: bool = False,
    ):
        del recursive
        if include_version and self.versioning_not_implemented:
            raise _NotImplementedError()
        result = []
        for (bucket, key), versions in self.objects.items():
            if bucket != bucket_name or not key.startswith(prefix):
                continue
            selected = versions if include_version else versions[-1:]
            result.extend(
                SimpleNamespace(
                    object_name=key,
                    size=len(record["content"]),
                    etag=record["etag"],
                    version_id=record["version_id"],
                    content_type=record["content_type"],
                    is_dir=False,
                    is_delete_marker=False,
                )
                for record in selected
            )
        return result

class ObjectStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeMinio()
        self.configuration = object_storage_service.MinioConfiguration(
            endpoint="minio.example.test",
            access_key="access",
            secret_key="secret",
            bucket_name="ontology",
            prefix="ontology-business",
        )
        self.configuration_patch = patch.object(
            object_storage_service,
            "configuration",
            return_value=self.configuration,
        )
        self.client_patch = patch.object(
            object_storage_service,
            "get_client",
            return_value=self.client,
        )
        self.configuration_patch.start()
        self.client_patch.start()
        self.addCleanup(self.configuration_patch.stop)
        self.addCleanup(self.client_patch.stop)
        self.source = DataSource(
            id="source-a",
            tenant_id="tenant-a",
            scenario_id="scenario-a",
            name="资料库",
            type="file_bucket",
            config={
                "storage_backend": "minio",
                "bucket_name": "ontology",
                "prefix": "ontology-business",
            },
        )

    def test_managed_upload_read_list_and_delete(self) -> None:
        content = "费用审批规则".encode()
        bucket_file = datasource_service.save_bucket_file(
            self.source,
            "规则.md",
            content,
        )

        expected_prefix = (
            "ontology-business/tenants/tenant-a/scenarios/scenario-a/"
            f"data-sources/source-a/files/{bucket_file.id}/"
        )
        self.assertEqual(bucket_file.storage_provider, "minio")
        self.assertEqual(bucket_file.bucket_name, "ontology")
        self.assertTrue(bucket_file.object_key.startswith(expected_prefix))
        self.assertEqual(bucket_file.stored_path, bucket_file.object_url)
        self.assertTrue(bucket_file.object_url.startswith("minio://ontology/"))
        self.assertEqual(bucket_file.content_sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(
            datasource_service.read_bucket_file(bucket_file, self.source),
            (content, len(content), "text/markdown; charset=utf-8"),
        )

        listed = object_storage_service.list_objects("ontology", expected_prefix.rstrip("/"))
        self.assertEqual([item.object_key for item in listed], [bucket_file.object_key])
        datasource_service.delete_bucket_file(bucket_file, self.source)
        datasource_service.delete_bucket_file(bucket_file, self.source)
        with self.assertRaises(FileNotFoundError):
            datasource_service.read_bucket_file(bucket_file, self.source)

    def test_bounded_stream_download_rejects_oversize_and_partial_reuse(self) -> None:
        uploaded = object_storage_service.put_object(
            "ontology",
            "ontology-business/dataset/fragment.parquet",
            b"parquet-bytes",
        )
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "fragment.partial"
            info = object_storage_service.download_object_to_file(
                "ontology",
                "ontology-business/dataset/fragment.parquet",
                destination,
                version_id=uploaded.version_id,
                max_bytes=len(b"parquet-bytes"),
            )
            self.assertEqual(info.size, len(b"parquet-bytes"))
            self.assertEqual(destination.read_bytes(), b"parquet-bytes")
            with self.assertRaisesRegex(ValueError, "新文件"):
                object_storage_service.download_object_to_file(
                    "ontology",
                    "ontology-business/dataset/fragment.parquet",
                    destination,
                    version_id=uploaded.version_id,
                    max_bytes=len(b"parquet-bytes"),
                )

            oversize_target = Path(temp_dir) / "oversize.partial"
            with self.assertRaisesRegex(
                object_storage_service.ObjectStorageError, "超过"
            ):
                object_storage_service.download_object_to_file(
                    "ontology",
                    "ontology-business/dataset/fragment.parquet",
                    oversize_target,
                    version_id=uploaded.version_id,
                    max_bytes=len(b"parquet-bytes") - 1,
                )
            self.assertFalse(oversize_target.exists())

    def test_integrity_and_scope_mismatches_fail_closed(self) -> None:
        bucket_file = datasource_service.save_bucket_file(
            self.source,
            "audit.txt",
            b"original",
        )
        versions = self.client.objects[(bucket_file.bucket_name, bucket_file.object_key)]
        versions[-1]["content"] = b"modified"
        versions[-1]["etag"] = bucket_file.etag
        with self.assertRaisesRegex(ValueError, "哈希"):
            datasource_service.read_bucket_file(bucket_file, self.source)

        versions[-1]["content"] = b"original"
        bucket_file.object_key = bucket_file.object_key.replace("tenant-a", "tenant-b")
        with self.assertRaisesRegex(ValueError, "字段与地址|作用域"):
            datasource_service.read_bucket_file(bucket_file, self.source)

    def test_claim_key_binding_and_legacy_record_compatibility(self) -> None:
        file_id = "e" * 32
        claimed_key = datasource_service.build_bucket_object_key(
            self.source,
            file_id,
            "claim.md",
            upload_id="f" * 32,
        )
        saved = datasource_service.save_bucket_file(
            self.source,
            "claim.md",
            b"claimed",
            stable_file_id=file_id,
            upload_object_key=claimed_key,
        )
        self.assertEqual(saved.object_key, claimed_key)

        with self.assertRaisesRegex(ValueError, "作用域"):
            datasource_service.save_bucket_file(
                self.source,
                "claim.md",
                b"foreign",
                stable_file_id=file_id,
                upload_object_key=claimed_key.replace("tenant-a", "tenant-b"),
            )

        legacy_key = datasource_service.build_bucket_object_key(
            self.source,
            "1" * 32,
            "legacy.md",
        )
        uploaded = object_storage_service.put_object(
            "ontology",
            legacy_key,
            b"legacy",
            content_type="text/markdown",
            sha256=hashlib.sha256(b"legacy").hexdigest(),
        )
        legacy_url = object_storage_service.stable_object_url(
            "ontology",
            legacy_key,
        )
        legacy = BucketFile(
            id="1" * 32,
            data_source_id=self.source.id,
            filename="legacy.md",
            stored_path=legacy_url,
            storage_provider="minio",
            bucket_name="ontology",
            object_key=legacy_key,
            object_version_id=uploaded.version_id,
            etag=uploaded.etag,
            object_url=legacy_url,
            size=6,
            mime="text/markdown",
            content_sha256=hashlib.sha256(b"legacy").hexdigest(),
        )
        self.assertEqual(
            datasource_service.read_bucket_file(legacy, self.source)[0],
            b"legacy",
        )

    def test_stable_id_keeps_logical_identity_but_never_reuses_object_key(self) -> None:
        stable_id = "a" * 32
        first = datasource_service.save_bucket_file(
            self.source,
            "result.md",
            b"first",
            stable_file_id=stable_id,
        )
        second = datasource_service.save_bucket_file(
            self.source,
            "result.md",
            b"second",
            stable_file_id=stable_id,
        )
        self.assertEqual(first.id, second.id)
        self.assertNotEqual(first.object_key, second.object_key)
        self.assertIn("/uploads/", first.object_key)
        self.assertIn("/uploads/", second.object_key)
        self.assertEqual(datasource_service.read_bucket_file(first, self.source)[0], b"first")
        self.assertEqual(datasource_service.read_bucket_file(second, self.source)[0], b"second")
        datasource_service.delete_bucket_file(first, self.source)
        self.assertEqual(datasource_service.read_bucket_file(second, self.source)[0], b"second")

    def test_unversioned_gateway_uses_unique_keys_and_exact_key_cleanup(self) -> None:
        self.client.versioning_not_implemented = True
        stable_id = "d" * 32
        first = datasource_service.save_bucket_file(
            self.source,
            "result.md",
            b"first",
            stable_file_id=stable_id,
        )
        second = datasource_service.save_bucket_file(
            self.source,
            "result.md",
            b"second",
            stable_file_id=stable_id,
        )

        self.assertTrue(object_storage_service.healthcheck())
        self.assertEqual(first.object_version_id, "")
        self.assertEqual(second.object_version_id, "")
        self.assertNotEqual(first.object_key, second.object_key)
        object_storage_service.delete_all_object_versions(
            first.bucket_name,
            first.object_key,
        )
        with self.assertRaises(FileNotFoundError):
            datasource_service.read_bucket_file(first, self.source)
        self.assertEqual(datasource_service.read_bucket_file(second, self.source)[0], b"second")

        third = datasource_service.save_bucket_file(
            self.source,
            "empty-list.md",
            b"third",
        )
        with patch.object(self.client, "list_objects", return_value=[]):
            object_storage_service.delete_all_object_versions(
                third.bucket_name,
                third.object_key,
            )
        self.assertNotIn((third.bucket_name, third.object_key), self.client.objects)

    def test_all_versions_cleanup_does_not_create_a_marker_for_missing_key(self) -> None:
        first = datasource_service.save_bucket_file(
            self.source,
            "result.md",
            b"body",
            stable_file_id="c" * 32,
        )
        self.client.put_object(
            first.bucket_name,
            first.object_key,
            BytesIO(b"second"),
            len(b"second"),
            content_type="text/markdown",
        )
        self.assertEqual(
            len(self.client.objects[(first.bucket_name, first.object_key)]), 2
        )
        object_storage_service.delete_all_object_versions(
            first.bucket_name, first.object_key
        )
        self.assertNotIn((first.bucket_name, first.object_key), self.client.objects)
        object_storage_service.delete_all_object_versions(
            first.bucket_name, first.object_key
        )
        self.assertNotIn((first.bucket_name, first.object_key), self.client.objects)

    def test_normalization_health_and_stable_url(self) -> None:
        normalized = datasource_service.normalize_file_bucket_config(
            {"bucket_name": "attacker", "access_key": "must-not-persist"}
        )
        self.assertEqual(
            normalized,
            {
                "storage_backend": "minio",
                "bucket_name": "ontology",
                "prefix": "ontology-business",
            },
        )
        self.source.config["storage_backend"] = "  MINIO  "
        self.assertTrue(datasource_service.is_managed_minio_source(self.source))
        self.assertFalse(object_storage_service.healthcheck())
        datasource_service.ensure_file_bucket_storage(self.source)
        self.assertTrue(object_storage_service.healthcheck())
        url = object_storage_service.stable_object_url(
            "ontology", "ontology-business/中文 文件.md"
        )
        self.assertEqual(
            object_storage_service.parse_object_url(url),
            ("ontology", "ontology-business/中文 文件.md"),
        )

    def test_parse_bytes_does_not_require_a_local_file(self) -> None:
        result = doc_parser.parse_bytes("年度凭证".encode(), "凭证.md")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["text"], "年度凭证")

    def test_legacy_text_only_attachment_has_no_managed_object(self) -> None:
        attachment = AssistantAttachment(
            id="a" * 32,
            tenant_id="tenant-a",
            filename="历史说明.md",
            storage_provider="minio",
            bucket_name="",
            object_key="",
            object_url="",
            parsed_text="历史解析文本",
            status="parsed",
        )

        self.assertIsNone(
            datasource_service.assistant_attachment_object_identity(attachment)
        )

    def test_assistant_attachment_uses_managed_object_identity(self) -> None:
        attachment = AssistantAttachment(
            id="b" * 32,
            tenant_id="tenant-a",
            filename="业务说明.md",
            mime="text/markdown",
            size=4,
        )
        datasource_service.save_assistant_attachment_object(attachment, b"body")
        self.assertEqual(attachment.storage_provider, "minio")
        self.assertEqual(attachment.bucket_name, "ontology")
        self.assertIn(
            "/tenants/tenant-a/scenarios/global/assistant-attachments/",
            f"/{attachment.object_key}",
        )
        self.assertEqual(
            object_storage_service.get_object(
                attachment.bucket_name,
                attachment.object_key,
                version_id=attachment.object_version_id,
            ),
            b"body",
        )
        datasource_service.delete_assistant_attachment_object(attachment)
        datasource_service.delete_assistant_attachment_object(attachment)
        with self.assertRaises(FileNotFoundError):
            object_storage_service.get_object(
                attachment.bucket_name,
                attachment.object_key,
                version_id=attachment.object_version_id,
            )

    def test_assistant_upload_preserves_object_when_commit_result_is_unknown(self) -> None:
        class _FailingDb:
            rolled_back = False

            def add(self, _item) -> None:
                return None

            def flush(self) -> None:
                return None

            def commit(self) -> None:
                raise RuntimeError("isolated commit failure")

            def rollback(self) -> None:
                self.rolled_back = True

        db = _FailingDb()
        upload = UploadFile(
            BytesIO(b"assistant body"),
            filename="assistant.md",
            headers=Headers({"content-type": "text/markdown"}),
        )
        upload_claim = SimpleNamespace(
            object_key=datasource_service.build_assistant_attachment_object_key(
                "tenant-a",
                "f" * 32,
                "assistant.md",
                upload_id="c" * 32,
            )
        )
        heartbeat = SimpleNamespace(assert_active=lambda: None)
        with patch.object(
            assistant_router.uuid,
            "uuid4",
            return_value=SimpleNamespace(hex="f" * 32),
        ), patch.object(assistant_router, "_purge_expired_attachments"), patch.object(
            assistant_router, "_tenant", return_value="tenant-a"
        ), patch.object(
            assistant_router, "_current_user_id", return_value="user-a"
        ), patch.object(
            assistant_router.doc_parser,
            "parse_bytes",
            return_value={"status": "success", "text": "assistant body", "message": "ok"},
        ), patch.object(
            assistant_router.object_deletion_service,
            "prepare_assistant_attachment_upload",
            return_value=upload_claim,
        ), patch.object(
            assistant_router.object_deletion_service,
            "heartbeat_upload_intent",
            return_value=nullcontext(heartbeat),
        ), patch.object(
            assistant_router.object_deletion_service,
            "begin_upload_put",
        ), patch.object(
            assistant_router.object_deletion_service,
            "retain_assistant_attachment_upload",
        ):
            with self.assertRaisesRegex(RuntimeError, "isolated commit failure"):
                asyncio.run(assistant_router.upload_attachment(upload, db))
        self.assertTrue(db.rolled_back)
        self.assertEqual(len(self.client.objects), 1)

    def test_assistant_retain_lease_loss_rolls_back_before_cleanup(self) -> None:
        class _FailingDb:
            rolled_back = False

            def add(self, _item) -> None:
                return None

            def rollback(self) -> None:
                self.rolled_back = True

        db = _FailingDb()
        upload = UploadFile(
            BytesIO(b"assistant body"),
            filename="assistant.md",
            headers=Headers({"content-type": "text/markdown"}),
        )
        upload_claim = SimpleNamespace(
            object_key=datasource_service.build_assistant_attachment_object_key(
                "tenant-a",
                "f" * 32,
                "assistant.md",
                upload_id="d" * 32,
            )
        )
        heartbeat = SimpleNamespace(assert_active=lambda: None)
        captured: dict[str, object] = {}

        def schedule_after_rollback(claim, attachment) -> None:
            self.assertTrue(db.rolled_back)
            captured["claim"] = claim
            captured["attachment"] = attachment

        with patch.object(
            assistant_router.uuid,
            "uuid4",
            return_value=SimpleNamespace(hex="f" * 32),
        ), patch.object(assistant_router, "_purge_expired_attachments"), patch.object(
            assistant_router, "_tenant", return_value="tenant-a"
        ), patch.object(
            assistant_router, "_current_user_id", return_value="user-a"
        ), patch.object(
            assistant_router.object_deletion_service,
            "prepare_assistant_attachment_upload",
            return_value=upload_claim,
        ), patch.object(
            assistant_router.object_deletion_service,
            "heartbeat_upload_intent",
            return_value=nullcontext(heartbeat),
        ), patch.object(
            assistant_router.object_deletion_service,
            "begin_upload_put",
        ), patch.object(
            assistant_router.object_deletion_service,
            "retain_assistant_attachment_upload",
            side_effect=assistant_router.object_deletion_service.UploadIntentLeaseLostError(
                "lease lost"
            ),
        ), patch.object(
            assistant_router.object_deletion_service,
            "schedule_abandoned_upload_best_effort",
            side_effect=schedule_after_rollback,
        ):
            with self.assertRaises(
                assistant_router.object_deletion_service.UploadIntentLeaseLostError
            ):
                asyncio.run(assistant_router.upload_attachment(upload, db))

        self.assertIs(captured["claim"], upload_claim)
        self.assertEqual(captured["attachment"].object_key, upload_claim.object_key)

if __name__ == "__main__":
    unittest.main()
