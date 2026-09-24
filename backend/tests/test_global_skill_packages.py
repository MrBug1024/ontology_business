import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import SKILLS_DIR
from app.services import distillation_resource_service as resources
from app.services import skill_instruction_service as instructions
from app.services import skill_service


TARGET_SKILLS = (
    "derive-business-flow",
    "discover-data-relations",
    "distill-business-capability",
)


def _skill(name: str):
    return SimpleNamespace(
        id=f"{name}-id",
        name=name,
        description="trusted method",
        source="builtin",
        path=str(SKILLS_DIR / name),
        enabled=True,
        tenant_id=None,
        is_public=True,
        meta={},
    )


def test_global_method_skill_packages_are_scannable_and_readable():
    scanned = {item["name"]: item for item in skill_service.scan_skills()}

    for name in TARGET_SKILLS:
        skill = _skill(name)
        text = instructions.read_instructions(skill)

        assert (SKILLS_DIR / name / "SKILL.md").is_file()
        assert scanned[name]["description"]
        assert f"name: {name}" in text
        assert "description:" in text
        assert len(instructions.content_fingerprint(skill)) == hashlib.sha256().digest_size * 2


def test_resource_catalog_offers_all_valid_global_skills(monkeypatch):
    skills = [_skill(name) for name in TARGET_SKILLS]

    class _Rows:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class _Db:
        def __init__(self):
            self.calls = 0

        def scalars(self, _statement):
            self.calls += 1
            return _Rows(skills if self.calls == 1 else [])

    monkeypatch.setattr(resources.permission_service, "require_principal",
                        lambda _db: SimpleNamespace(tenant_id="tenant-id"))
    monkeypatch.setattr(resources.permission_service, "require_tenant_permission", lambda _db, _verb: None)
    monkeypatch.setattr(resources.tenant_service, "visible_clause", lambda _model, _db: True)
    monkeypatch.setattr(resources.llm_service, "routable_configs", lambda _db, _capability: [])

    catalog = resources.resource_catalog(_Db())

    assert {item.name for item in catalog.skills} == set(TARGET_SKILLS)
    assert all(item.mode == "instructions" for item in catalog.skills)


def test_selected_global_skill_is_frozen_and_read_as_method_only(monkeypatch):
    skill = _skill("derive-business-flow")
    fingerprint = instructions.content_fingerprint(skill)
    turn = SimpleNamespace(context={
        "resource_selection": {
            "version": 4,
            "skills": [{"id": skill.id, "content_sha256": fingerprint}],
        },
    })

    class _Db:
        info = {"tenant_id": "tenant-id"}

        def scalar(self, _statement):
            return skill

    monkeypatch.setattr(resources.permission_service, "require_principal", lambda _db: SimpleNamespace())
    monkeypatch.setattr(resources.tenant_service, "visible_clause", lambda _model, _db: True)

    result = resources.execute(_Db(), "read_selected_skill", {"skill_id": skill.id}, turn)

    assert result.content["mode"] == "instructions"
    assert result.content["scripts_executed"] is False
    assert result.content["instructions"] == instructions.read_instructions(skill)
    assert result.source is None
    assert result.mcp_read is None
    assert result.library_reads == []


def test_invalid_global_skill_package_fails_closed(tmp_path):
    missing = SimpleNamespace(source="builtin", path=str(Path(tmp_path) / "missing"))

    with pytest.raises(ValueError, match="不是可用的受信方法包"):
        instructions.read_instructions(missing)
