"""Bounded SMTP delivery with durable claims; ambiguous delivery is never replayed."""
from __future__ import annotations

import asyncio
import html
import logging
import smtplib
from datetime import timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from uuid import uuid4

from sqlalchemy import select, update

from ..access_models import WorkspaceInvitation, now
from ..config import get_settings
from ..database import SessionLocal
from ..models import Organization
from .auth_service import MailConfigurationError, send_mail_message

logger = logging.getLogger(__name__)


def invitation_message(email: str, workspace: str) -> EmailMessage:
    settings = get_settings()
    if not settings.public_app_url:
        raise MailConfigurationError("平台访问地址未配置")
    url = f"{settings.public_app_url}/invitations"
    message = EmailMessage()
    message["Subject"] = "本体智能平台：工作区协作邀请"
    message["From"] = formataddr(("本体智能平台", settings.mail_from.strip() or settings.mail_username.strip()))
    message["To"] = email
    message.set_content(f"你受邀加入「{workspace}」。\n\n访问平台：{url}\n\n"
        "请使用收到本邮件的邮箱登录，在“工作区邀请”中同意或拒绝。尚未注册时请先注册并验证邮箱。"
        "邀请 24 小时内有效；登录不会自动加入工作区。如非预期邀请，可忽略本邮件。")
    message.add_alternative(f"<h2>工作区协作邀请</h2><p>你受邀加入「{html.escape(workspace)}」。</p>"
        f'<p><a href="{html.escape(url, quote=True)}">访问平台并查看邀请</a></p>'
        "<p>请使用收件邮箱登录，确认同意后加入。尚未注册请先完成注册和邮箱验证。邀请 24 小时内有效。</p>", subtype="html")
    return message


def deliver_one() -> bool:
    lease = uuid4().hex
    with SessionLocal() as db:
        # A crashed SMTP send may already have been accepted. Expose uncertainty.
        expired_claims = select(WorkspaceInvitation.id).where(WorkspaceInvitation.delivery_status == "sending",
            WorkspaceInvitation.delivery_lease_expires_at <= now()).limit(100)
        db.execute(update(WorkspaceInvitation).where(WorkspaceInvitation.id.in_(expired_claims)).values(delivery_status="indeterminate"))
        row = db.scalar(select(WorkspaceInvitation).where(WorkspaceInvitation.delivery_status == "queued")
            .order_by(WorkspaceInvitation.created_at).limit(1).with_for_update(skip_locked=True))
        if not row:
            db.commit()
            return False
        if row.status != "pending" or row.expires_at.replace(tzinfo=timezone.utc) <= now():
            row.delivery_status = "cancelled"
            db.commit()
            return True
        row.delivery_status = "sending"
        row.delivery_lease = lease
        row.delivery_lease_expires_at = now() + timedelta(minutes=5)
        invitation_id, generation, email = row.id, row.delivery_generation, row.email
        workspace = db.get(Organization, row.organization_id).name
        db.commit()
    # No database transaction is held while contacting SMTP.
    try:
        message = invitation_message(email, workspace)
        send_mail_message(email, message)
        result = "sent"
    except (MailConfigurationError, smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused):
        result = "failed"
    except (OSError, smtplib.SMTPException):
        result = "indeterminate"
    with SessionLocal() as db:
        db.execute(update(WorkspaceInvitation).where(WorkspaceInvitation.id == invitation_id,
            WorkspaceInvitation.delivery_status == "sending", WorkspaceInvitation.delivery_lease == lease,
            WorkspaceInvitation.delivery_generation == generation).values(delivery_status=result,
                delivery_lease=None, delivery_lease_expires_at=None))
        db.commit()
    return True


async def run_worker() -> None:
    while True:
        try:
            for _ in range(10):
                if not await asyncio.to_thread(deliver_one):
                    break
        except Exception:  # worker boundary; do not emit SMTP or connection details
            logger.error("工作区邀请投递暂不可用，将在下一轮检查持久任务")
        await asyncio.sleep(15)
