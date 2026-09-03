"""Regression coverage for mail-backed account provisioning and invitations."""
from __future__ import annotations

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.models import (
    EmailVerificationCode,
    OrganizationMember,
    OrganizationRole,
    Tenant,
    User,
)
from app.routers import auth, organization
from app.schemas import (
    OrganizationInvitationAcceptIn,
    OrganizationInvitationIn,
    OrganizationMemberRoleIn,
    RegisterIn,
    VerifyEmailIn,
)
from app.services import auth_service, permission_service


class AccountProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.db: Session = self.Session()
        self.tenant = Tenant(id="tenant-account-provisioning", name="账户测试工作区")
        self.owner = User(
            id="owner-account-provisioning",
            tenant_id=self.tenant.id,
            email="owner-account@example.test",
            password_hash=auth_service.hash_password("OwnerPassword123"),
            status="active",
        )
        self.db.add_all([self.tenant, self.owner])
        self.db.commit()
        self.organization = permission_service.ensure_organization(
            self.db,
            self.tenant.id,
            owner_user_id=self.owner.id,
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.owner.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _code_settings() -> SimpleNamespace:
        return SimpleNamespace(
            verification_code_minutes=10,
            invitation_code_minutes=7 * 24 * 60,
            verification_code_max_attempts=5,
            verification_code_lock_minutes=30,
        )

    def _add_active_owner(self, email: str = "second-owner@example.test") -> OrganizationMember:
        owner_role = self.db.scalar(
            select(OrganizationRole).where(
                OrganizationRole.organization_id == self.organization.id,
                OrganizationRole.key == "owner",
            )
        )
        assert owner_role is not None
        user = User(
            tenant_id=self.tenant.id,
            email=email,
            password_hash=auth_service.hash_password("SecondOwnerPassword123"),
            status="active",
        )
        self.db.add(user)
        self.db.flush()
        member = OrganizationMember(
            organization_id=self.organization.id,
            user_id=user.id,
            role_id=owner_role.id,
            status="active",
        )
        self.db.add(member)
        self.db.commit()
        return member

    def test_smtp_delivery_failure_rolls_back_new_registration_with_503(self) -> None:
        email = "mail-rollback@example.test"
        tenant_count = self.db.scalar(select(func.count()).select_from(Tenant))
        payload = RegisterIn(
            email=email,
            display_name="邮件回滚账户",
            password="AccountPassword123",
            password_confirm="AccountPassword123",
        )

        with (
            patch.object(auth_service, "get_settings", return_value=self._code_settings()),
            patch.object(
                auth_service,
                "send_verification_email",
                side_effect=auth_service.MailConfigurationError("SMTP unavailable"),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            auth.register(payload, self.db)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIsNone(
            self.db.scalar(select(User).where(User.email == email))
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(Tenant)), tenant_count
        )

    def test_disabled_account_cannot_reactivate_through_public_registration(self) -> None:
        disabled = User(
            id="disabled-account-provisioning",
            tenant_id=self.tenant.id,
            email="disabled-account@example.test",
            password_hash=auth_service.hash_password("DisabledPassword123"),
            status="disabled",
        )
        self.db.add(disabled)
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            auth.register(
                RegisterIn(
                    email=disabled.email,
                    display_name="被禁用账户",
                    password="AnotherPassword123",
                    password_confirm="AnotherPassword123",
                ),
                self.db,
            )

        self.assertEqual(raised.exception.status_code, 403)
        self.db.refresh(disabled)
        self.assertEqual(disabled.status, "disabled")

    def test_invitation_requires_code_before_a_pending_member_becomes_active(self) -> None:
        delivered: dict[str, str] = {}

        def capture_delivery(email: str, code: str, purpose: str) -> None:
            delivered.update(email=email, code=code, purpose=purpose)

        invite_email = "invited-account@example.test"
        with (
            patch.object(auth_service, "get_settings", return_value=self._code_settings()),
            patch.object(auth_service, "send_verification_email", side_effect=capture_delivery),
        ):
            result = organization.invite_member(
                OrganizationInvitationIn(
                    email=invite_email,
                    display_name="受邀成员",
                    role_key="operator",
                ),
                self.db,
            )

        self.assertEqual(result.email, invite_email)
        self.assertEqual(delivered["purpose"], "invite")
        invited = self.db.scalar(select(User).where(User.email == invite_email))
        self.assertIsNotNone(invited)
        assert invited is not None
        member = self.db.scalar(
            select(OrganizationMember).where(OrganizationMember.user_id == invited.id)
        )
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(invited.status, "pending")
        self.assertEqual(member.status, "invited")

        organization.accept_invitation(
            OrganizationInvitationAcceptIn(
                email=invite_email,
                code=delivered["code"],
                password="InvitedPassword123",
                password_confirm="InvitedPassword123",
                display_name="已加入成员",
            ),
            self.db,
        )

        self.db.refresh(invited)
        self.db.refresh(member)
        self.assertEqual(invited.status, "active")
        self.assertIsNotNone(invited.email_verified_at)
        self.assertEqual(member.status, "active")
        self.assertTrue(auth_service.verify_password("InvitedPassword123", invited.password_hash))

    def test_wrong_invitation_codes_are_persisted_and_lock_the_challenge(self) -> None:
        delivered: dict[str, str] = {}

        def capture_delivery(email: str, code: str, purpose: str) -> None:
            delivered.update(email=email, code=code, purpose=purpose)

        email = "invite-lock@example.test"
        with (
            patch.object(auth_service, "get_settings", return_value=self._code_settings()),
            patch.object(auth_service, "send_verification_email", side_effect=capture_delivery),
        ):
            organization.invite_member(
                OrganizationInvitationIn(email=email, display_name="待验证成员", role_key="operator"),
                self.db,
            )
            wrong_code = "000000" if delivered["code"] != "000000" else "999999"
            for _ in range(5):
                with self.assertRaises(HTTPException) as raised:
                    organization.accept_invitation(
                        OrganizationInvitationAcceptIn(
                            email=email,
                            code=wrong_code,
                            password="InviteLockPassword123",
                            password_confirm="InviteLockPassword123",
                        ),
                        self.db,
                    )
                self.assertEqual(raised.exception.status_code, 400)

            code_record = self.db.scalar(
                select(EmailVerificationCode)
                .where(
                    EmailVerificationCode.email == email,
                    EmailVerificationCode.purpose == "invite",
                )
                .order_by(EmailVerificationCode.created_at.desc())
            )
            assert code_record is not None
            self.assertEqual(code_record.failed_attempts, 5)
            self.assertIsNotNone(code_record.locked_until)

            with self.assertRaises(HTTPException) as raised:
                organization.accept_invitation(
                    OrganizationInvitationAcceptIn(
                        email=email,
                        code=delivered["code"],
                        password="InviteLockPassword123",
                        password_confirm="InviteLockPassword123",
                    ),
                    self.db,
                )
            self.assertEqual(raised.exception.status_code, 400)

        invited = self.db.scalar(select(User).where(User.email == email))
        assert invited is not None
        self.assertEqual(invited.status, "pending")

    def test_email_code_retries_and_resends_cannot_reset_guess_budget(self) -> None:
        email = "email-code-lock@example.test"
        user = User(
            tenant_id=self.tenant.id,
            email=email,
            password_hash=auth_service.hash_password("EmailCodePassword123"),
            status="pending",
        )
        self.db.add(user)
        self.db.flush()

        with patch.object(auth_service, "get_settings", return_value=self._code_settings()):
            original_code = auth_service.issue_email_code(self.db, user, "register")
            self.db.commit()
            wrong_code = "000000" if original_code != "000000" else "999999"
            for _ in range(4):
                with self.assertRaises(HTTPException) as raised:
                    auth.verify_email(VerifyEmailIn(email=email, code=wrong_code), self.db)
                self.assertEqual(raised.exception.status_code, 400)

            initial_record = self.db.scalar(
                select(EmailVerificationCode)
                .where(
                    EmailVerificationCode.user_id == user.id,
                    EmailVerificationCode.purpose == "register",
                )
                .order_by(EmailVerificationCode.created_at.desc())
            )
            assert initial_record is not None
            self.assertEqual(initial_record.failed_attempts, 4)
            # Move past the existing 60-second delivery throttle.  The fresh
            # code must inherit the persisted count instead of restarting it.
            initial_record.created_at = auth_service.utc_now() - timedelta(seconds=61)
            self.db.commit()

            replacement_code = auth_service.issue_email_code(self.db, user, "register")
            self.db.commit()
            replacement = self.db.scalar(
                select(EmailVerificationCode)
                .where(
                    EmailVerificationCode.user_id == user.id,
                    EmailVerificationCode.purpose == "register",
                )
                .order_by(EmailVerificationCode.created_at.desc())
            )
            assert replacement is not None
            self.assertEqual(replacement.failed_attempts, 4)

            replacement_wrong_code = (
                "000000" if replacement_code != "000000" else "999999"
            )
            with self.assertRaises(HTTPException) as raised:
                auth.verify_email(
                    VerifyEmailIn(email=email, code=replacement_wrong_code), self.db
                )
            self.assertEqual(raised.exception.status_code, 400)
            self.db.refresh(replacement)
            self.assertEqual(replacement.failed_attempts, 5)
            self.assertIsNotNone(replacement.locked_until)

            with self.assertRaises(HTTPException) as raised:
                auth.verify_email(VerifyEmailIn(email=email, code=replacement_code), self.db)
            self.assertEqual(raised.exception.status_code, 400)

    def test_owner_demotion_uses_organization_guard_and_keeps_one_owner(self) -> None:
        second_owner = self._add_active_owner()
        original_lock = permission_service.lock_organization_owner_changes
        with patch.object(
            permission_service,
            "lock_organization_owner_changes",
            wraps=original_lock,
        ) as guard:
            organization.update_member_role(
                second_owner.id,
                OrganizationMemberRoleIn(role_key="operator"),
                self.db,
            )
        guard.assert_called_once_with(self.db, self.organization.id)
        self.assertEqual(permission_service.owner_count(self.db, self.organization.id), 1)

        primary_owner = self.db.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == self.organization.id,
                OrganizationMember.user_id == self.owner.id,
            )
        )
        assert primary_owner is not None
        with self.assertRaises(HTTPException) as raised:
            organization.update_member_role(
                primary_owner.id,
                OrganizationMemberRoleIn(role_key="operator"),
                self.db,
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(permission_service.owner_count(self.db, self.organization.id), 1)

    def test_pending_owner_membership_does_not_replace_an_active_owner(self) -> None:
        owner_role = self.db.scalar(
            select(OrganizationRole).where(
                OrganizationRole.organization_id == self.organization.id,
                OrganizationRole.key == "owner",
            )
        )
        assert owner_role is not None
        pending_user = User(
            tenant_id=self.tenant.id,
            email="pending-owner@example.test",
            password_hash=auth_service.hash_password("PendingOwnerPassword123"),
            status="pending",
        )
        self.db.add(pending_user)
        self.db.flush()
        self.db.add(
            OrganizationMember(
                organization_id=self.organization.id,
                user_id=pending_user.id,
                role_id=owner_role.id,
                status="active",
            )
        )
        self.db.commit()

        self.assertEqual(permission_service.owner_count(self.db, self.organization.id), 1)
        primary_owner = self.db.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == self.organization.id,
                OrganizationMember.user_id == self.owner.id,
            )
        )
        assert primary_owner is not None
        with self.assertRaises(HTTPException) as raised:
            organization.update_member_role(
                primary_owner.id,
                OrganizationMemberRoleIn(role_key="operator"),
                self.db,
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(permission_service.owner_count(self.db, self.organization.id), 1)

    def test_internal_role_assignment_cannot_bypass_last_owner_guard(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            permission_service.assign_member_role(
                self.db,
                self.organization,
                user_id=self.owner.id,
                role_key="operator",
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(permission_service.owner_count(self.db, self.organization.id), 1)

    def test_owner_guard_is_transaction_scoped_on_postgresql(self) -> None:
        db = Mock()
        db.get_bind.return_value.dialect.name = "postgresql"

        permission_service.lock_organization_owner_changes(db, self.organization.id)

        statement = str(db.execute.call_args.args[0])
        self.assertIn("pg_advisory_xact_lock", statement)

    def test_clearing_permission_cache_reloads_role_after_an_owner_lock_wait(self) -> None:
        original = permission_service.require_principal(self.db)
        self.assertEqual(original.role_key, "owner")
        operator_role = self.db.scalar(
            select(OrganizationRole).where(
                OrganizationRole.organization_id == self.organization.id,
                OrganizationRole.key == "operator",
            )
        )
        assert operator_role is not None

        # Simulate another transaction completing while this request waited for
        # the organization-wide advisory lock.  The bulk update intentionally
        # leaves this Session's loaded OrganizationMember stale.
        self.db.execute(
            update(OrganizationMember)
            .where(
                OrganizationMember.organization_id == self.organization.id,
                OrganizationMember.user_id == self.owner.id,
            )
            .values(role_id=operator_role.id)
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        permission_service.clear_request_permission_cache(self.db)

        refreshed = permission_service.require_principal(self.db)
        self.assertEqual(refreshed.role_key, "operator")

    def test_clearing_permission_cache_reloads_a_concurrently_disabled_actor(self) -> None:
        self.assertEqual(permission_service.require_principal(self.db).role_key, "owner")
        self.db.execute(
            update(User)
            .where(User.id == self.owner.id)
            .values(status="disabled")
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        permission_service.clear_request_permission_cache(self.db)

        with self.assertRaises(HTTPException) as raised:
            permission_service.require_principal(self.db)
        self.assertEqual(raised.exception.status_code, 403)

    def test_owner_disable_uses_organization_guard_and_leaves_an_owner(self) -> None:
        second_owner = self._add_active_owner("disable-owner@example.test")
        original_lock = permission_service.lock_organization_owner_changes
        with patch.object(
            permission_service,
            "lock_organization_owner_changes",
            wraps=original_lock,
        ) as guard:
            organization.disable_member(second_owner.id, self.db)

        guard.assert_called_once_with(self.db, self.organization.id)
        self.assertEqual(permission_service.owner_count(self.db, self.organization.id), 1)

    def test_mutually_exclusive_smtp_tls_modes_are_rejected_at_configuration_time(self) -> None:
        with self.assertRaises(ValueError):
            Settings(
                database_url="postgresql+psycopg://user:password@db.example.test/platform",
                mail_starttls=True,
                mail_ssl_tls=True,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
