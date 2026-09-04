"""Regression coverage for inviting an already registered account."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.database import Base
from app.models import (
    Agent,
    AuthSession,
    BusinessScenario,
    DataSource,
    OrganizationInvitation,
    OrganizationMember,
    Tenant,
    User,
)
from app.routers import organization
from app.schemas import OrganizationInvitationIn
from app.services import auth_service, permission_service


def _request_with_session(session_id: str, token: str = "") -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    request = Request({"type": "http", "headers": headers})
    request.state.auth_session_id = session_id
    return request


class RegisteredWorkspaceInvitationTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.engine = engine
        self.Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.db: Session = self.Session()

        self.owner_tenant = Tenant(id="tenant-inviter", name="邀请方工作区")
        self.recipient_tenant = Tenant(id="tenant-recipient", name="受邀者个人工作区")
        self.owner = User(
            id="user-inviter",
            tenant_id=self.owner_tenant.id,
            email="inviter@example.test",
            display_name="邀请人",
            password_hash=auth_service.hash_password("OwnerPassword123"),
            status="active",
            email_verified_at=datetime.now(timezone.utc),
        )
        self.recipient = User(
            id="user-recipient",
            tenant_id=self.recipient_tenant.id,
            email="recipient@example.test",
            display_name="受邀成员",
            password_hash=auth_service.hash_password("RecipientPassword123"),
            status="active",
            email_verified_at=datetime.now(timezone.utc),
        )
        self.db.add_all([self.owner_tenant, self.recipient_tenant, self.owner, self.recipient])
        self.db.flush()
        self.owner_organization = permission_service.ensure_organization(
            self.db, self.owner_tenant.id, owner_user_id=self.owner.id
        )
        permission_service.ensure_organization(
            self.db, self.recipient_tenant.id, owner_user_id=self.recipient.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.owner_tenant.id
        self.db.info["user_id"] = self.owner.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_registered_user_accepts_without_account_or_home_data_mutation_then_removal_transfers_shared_ownership(self) -> None:
        delivered: dict[str, str] = {}

        def capture_delivery(email: str, code: str, **kwargs: str) -> None:
            delivered.update(email=email, code=code, **kwargs)

        original_password_hash = self.recipient.password_hash
        with patch.object(
            auth_service,
            "send_workspace_invitation_email",
            side_effect=capture_delivery,
        ):
            result = organization.invite_member(
                OrganizationInvitationIn(
                    email=self.recipient.email,
                    role_key="operator",
                ),
                self.db,
            )

        self.assertEqual(result.email, self.recipient.email)
        self.assertEqual(delivered["email"], self.recipient.email)
        self.assertEqual(len(delivered["code"]), 6)
        self.assertEqual(delivered["workspace_name"], self.owner_tenant.name)

        invitation = self.db.scalar(
            select(OrganizationInvitation).where(
                OrganizationInvitation.user_id == self.recipient.id,
                OrganizationInvitation.status == "pending",
            )
        )
        assert invitation is not None
        member = self.db.scalar(
            select(OrganizationMember).where(
                OrganizationMember.id == invitation.member_id
            )
        )
        assert member is not None
        self.assertEqual(member.status, "invited")
        self.assertEqual(member.invited_by_user_id, self.owner.id)
        self.assertLessEqual(
            (invitation.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds(),
            24 * 60 * 60,
        )
        self.assertGreater(
            (invitation.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds(),
            23 * 60 * 60 + 55 * 60,
        )

        session = AuthSession(
            id="session-recipient",
            user_id=self.recipient.id,
            active_tenant_id=self.recipient_tenant.id,
            token_hash=auth_service._token_hash("recipient-token"),  # noqa: SLF001 - test session lookup.
            expires_at=datetime.now(timezone.utc).replace(year=2099),
        )
        self.db.add(session)
        self.db.commit()
        self.db.info["tenant_id"] = self.recipient_tenant.id
        self.db.info["user_id"] = self.recipient.id
        accepted = organization.accept_my_invitation(
            invitation.id,
            _request_with_session(session.id),
            self.db,
        )

        self.db.refresh(self.recipient)
        self.db.refresh(member)
        self.db.refresh(invitation)
        self.db.refresh(session)
        self.assertEqual(accepted.tenant_id, self.owner_tenant.id)
        self.assertEqual(self.recipient.tenant_id, self.recipient_tenant.id)
        self.assertEqual(self.recipient.password_hash, original_password_hash)
        self.assertEqual(self.recipient.display_name, "受邀成员")
        self.assertEqual(member.status, "active")
        self.assertEqual(invitation.status, "accepted")
        self.assertEqual(session.active_tenant_id, self.owner_tenant.id)
        self.assertEqual(
            permission_service.require_principal(self.db).role_key,
            "operator",
        )

        shared_scenario = BusinessScenario(
            id="scenario-shared-recipient",
            tenant_id=self.owner_tenant.id,
            created_by_user_id=self.recipient.id,
            owner_user_id=self.recipient.id,
            name="共同场景",
        )
        shared_source = DataSource(
            id="source-shared-recipient",
            tenant_id=self.owner_tenant.id,
            created_by_user_id=self.recipient.id,
            owner_user_id=self.recipient.id,
            name="共同数据源",
            type="file_bucket",
        )
        shared_agent = Agent(
            id="agent-shared-recipient",
            tenant_id=self.owner_tenant.id,
            created_by_user_id=self.recipient.id,
            owner_user_id=self.recipient.id,
            name="共同 Agent",
        )
        personal_scenario = BusinessScenario(
            id="scenario-personal-recipient",
            tenant_id=self.recipient_tenant.id,
            created_by_user_id=self.recipient.id,
            owner_user_id=self.recipient.id,
            name="个人场景",
        )
        self.db.add_all([shared_scenario, shared_source, shared_agent, personal_scenario])
        self.db.commit()

        self.db.info["tenant_id"] = self.owner_tenant.id
        self.db.info["user_id"] = self.owner.id
        removed = organization.remove_member(member.id, self.db)
        self.assertIn("3 项共同工作区资源", removed.message)
        self.db.refresh(member)
        self.db.refresh(shared_scenario)
        self.db.refresh(shared_source)
        self.db.refresh(shared_agent)
        self.db.refresh(personal_scenario)
        self.assertEqual(member.status, "removed")
        self.assertEqual(shared_scenario.owner_user_id, self.owner.id)
        self.assertEqual(shared_source.owner_user_id, self.owner.id)
        self.assertEqual(shared_agent.owner_user_id, self.owner.id)
        self.assertEqual(shared_scenario.created_by_user_id, self.recipient.id)
        self.assertEqual(personal_scenario.owner_user_id, self.recipient.id)
        self.assertEqual(self.recipient.status, "active")
        self.assertEqual(self.recipient.tenant_id, self.recipient_tenant.id)

        # A stale session that was browsing the shared workspace is returned to
        # the recipient's home workspace on its next authenticated request.
        request = _request_with_session(session.id, "recipient-token")
        current_user = auth_service.get_current_user(request, self.db)
        self.db.refresh(session)
        self.assertEqual(current_user.id, self.recipient.id)
        self.assertEqual(request.state.tenant_id, self.recipient_tenant.id)
        self.assertEqual(session.active_tenant_id, self.recipient_tenant.id)

    def test_pending_registered_invitation_is_only_visible_to_its_recipient(self) -> None:
        with patch.object(auth_service, "send_workspace_invitation_email"):
            organization.invite_member(
                OrganizationInvitationIn(email=self.recipient.email, role_key="viewer"),
                self.db,
            )
        self.db.info["tenant_id"] = self.recipient_tenant.id
        self.db.info["user_id"] = self.recipient.id
        inbox = organization.list_my_invitations(self.db)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].organization_id, self.owner_organization.id)
        self.assertEqual(inbox[0].role_key, "viewer")

        self.db.info["tenant_id"] = self.owner_tenant.id
        self.db.info["user_id"] = self.owner.id
        self.assertEqual(organization.list_my_invitations(self.db), [])

        with self.assertRaises(HTTPException) as raised:
            organization.accept_my_invitation(
                inbox[0].id,
                _request_with_session("missing-session"),
                self.db,
            )
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
