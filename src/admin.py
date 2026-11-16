# src/admin.py

import hmac
import uuid

from fastapi import FastAPI
from sqlalchemy import and_, update
from sqladmin import Admin, ModelView, action
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from src.api.auth.crud import revoke_user_sessions
from src.api.auth.models import (
    AuthAuditLog,
    EmailToken,
    PasswordCredential,
    Session,
    User,
    utcnow,
)
from src.common.config import settings
from src.common.database import AsyncSessionFactory, engine
from src.common.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Action helpers
# ---------------------------------------------------------------------------


def _selected_ids(request: Request) -> list[uuid.UUID]:
    """Primary keys selected in the admin UI.

    sqladmin calls custom actions with ``GET ...?pks=<id>,<id>``. Values that
    are not UUIDs are skipped, so malformed input cannot cause a server error.
    """
    raw = request.query_params.get("pks", "")
    ids: list[uuid.UUID] = []
    for value in raw.split(","):
        try:
            ids.append(uuid.UUID(value.strip()))
        except ValueError:
            continue
    return ids


def _back_to_list(request: Request, identity: str) -> RedirectResponse:
    """Redirect to the model's list view: a fixed admin path.

    Never to the client-supplied ``Referer`` header (open redirect). Only the
    path is used, so a forged ``Host`` header cannot change the target either.
    """
    path = request.url_for("admin:list", identity=identity).path
    return RedirectResponse(path, status_code=303)


# ---------------------------------------------------------------------------
# Auth backend
# ---------------------------------------------------------------------------


class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username, password = form.get("username"), form.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return False
        # Constant-time comparison of both values (no timing side channel).
        username_ok = hmac.compare_digest(
            username.encode(), settings.ADMIN_USERNAME.encode()
        )
        password_ok = hmac.compare_digest(
            password.encode(), settings.ADMIN_PASSWORD.get_secret_value().encode()
        )
        if username_ok and password_ok:
            request.session.update({"admin": True})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return bool(request.session.get("admin"))


# ---------------------------------------------------------------------------
# Model views
# ---------------------------------------------------------------------------


class UserAdmin(ModelView, model=User):
    name_plural = "Users"
    icon = "fa-solid fa-users"

    column_list = [
        User.id,
        User.username,
        User.email,
        User.display_name,
        User.is_active,
        User.is_email_verified,
        User.is_locked,
        User.failed_login_attempts,
        User.created_at,
        User.last_login_at,
    ]
    column_searchable_list = [User.username, User.email, User.display_name]
    column_sortable_list = [
        User.username,
        User.email,
        User.is_active,
        User.is_locked,
        User.created_at,
        User.last_login_at,
        User.failed_login_attempts,
    ]
    column_default_sort = [(User.created_at, True)]
    page_size = 25
    page_size_options = [10, 25, 50, 100]

    @action(
        name="lock_users",
        label="Lock selected",
        confirmation_message="Lock selected users? They will not be able to log in.",
        add_in_detail=True,
        add_in_list=True,
    )
    async def lock_users(self, request: Request) -> Response:
        """Lock indefinitely (locked_until NULL) and end all sessions."""
        ids = _selected_ids(request)
        if ids:
            async with AsyncSessionFactory() as session:
                await session.execute(
                    update(User)
                    .where(User.id.in_(ids))
                    .values(is_locked=True, locked_until=None, updated_at=utcnow())
                )
                await session.commit()
                for user_id in ids:
                    await revoke_user_sessions(session, user_id, reason="admin_lock")
            logger.info("admin.users_locked", user_ids=[str(i) for i in ids])
        return _back_to_list(request, self.identity)

    @action(
        name="unlock_users",
        label="Unlock selected",
        confirmation_message="Unlock selected users and reset failed login attempts?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def unlock_users(self, request: Request) -> Response:
        ids = _selected_ids(request)
        if ids:
            async with AsyncSessionFactory() as session:
                await session.execute(
                    update(User)
                    .where(User.id.in_(ids))
                    .values(
                        is_locked=False,
                        failed_login_attempts=0,
                        locked_until=None,
                        updated_at=utcnow(),
                    )
                )
                await session.commit()
            logger.info("admin.users_unlocked", user_ids=[str(i) for i in ids])
        return _back_to_list(request, self.identity)

    @action(
        name="force_password_change",
        label="Force password change",
        confirmation_message="Require selected users to change their password on next login?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def force_password_change(self, request: Request) -> Response:
        # Only sets the flag. Enforcing must_change needs a new error code in
        # the API contract and is planned for Roadmap Phase 3.
        ids = _selected_ids(request)
        if ids:
            async with AsyncSessionFactory() as session:
                await session.execute(
                    update(PasswordCredential)
                    .where(PasswordCredential.user_id.in_(ids))
                    .values(must_change=True, updated_at=utcnow())
                )
                await session.commit()
            logger.info("admin.password_change_forced", user_ids=[str(i) for i in ids])
        return _back_to_list(request, self.identity)

    @action(
        name="deactivate_users",
        label="Deactivate selected",
        confirmation_message="Deactivate selected users?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def deactivate_users(self, request: Request) -> Response:
        """Deactivate and end all sessions."""
        ids = _selected_ids(request)
        if ids:
            async with AsyncSessionFactory() as session:
                await session.execute(
                    update(User)
                    .where(User.id.in_(ids))
                    .values(is_active=False, updated_at=utcnow())
                )
                await session.commit()
                for user_id in ids:
                    await revoke_user_sessions(
                        session, user_id, reason="admin_deactivate"
                    )
            logger.info("admin.users_deactivated", user_ids=[str(i) for i in ids])
        return _back_to_list(request, self.identity)


class PasswordCredentialAdmin(ModelView, model=PasswordCredential):
    name = "Password Credential"
    name_plural = "Password Credentials"
    icon = "fa-solid fa-key"

    # Creating via admin is impossible without a proper hash
    can_create = False
    column_list = [
        PasswordCredential.id,
        PasswordCredential.user_id,
        PasswordCredential.must_change,
        PasswordCredential.created_at,
        PasswordCredential.updated_at,
    ]
    # Hide hash in both form and detail — it must never leave hashed storage
    form_excluded_columns = [PasswordCredential.password_hash]
    column_details_exclude_list = [PasswordCredential.password_hash]
    column_sortable_list = [
        PasswordCredential.must_change,
        PasswordCredential.created_at,
        PasswordCredential.updated_at,
    ]
    column_default_sort = [(PasswordCredential.created_at, True)]


class AuthAuditLogAdmin(ModelView, model=AuthAuditLog):
    name = "Audit Log"
    name_plural = "Audit Logs"
    icon = "fa-solid fa-clipboard-list"

    # Audit logs must never be modified
    can_create = False
    can_edit = False
    can_delete = False

    column_list = [
        AuthAuditLog.id,
        AuthAuditLog.user_id,
        AuthAuditLog.event_type,
        AuthAuditLog.ip_address,
        AuthAuditLog.user_agent,
        AuthAuditLog.detail,
        AuthAuditLog.created_at,
    ]
    column_searchable_list = [AuthAuditLog.ip_address, AuthAuditLog.detail]
    column_sortable_list = [AuthAuditLog.created_at, AuthAuditLog.event_type]
    column_default_sort = [(AuthAuditLog.created_at, True)]
    page_size = 50
    page_size_options = [25, 50, 100, 200]


class EmailTokenAdmin(ModelView, model=EmailToken):
    name = "Email Token"
    name_plural = "Email Tokens"
    icon = "fa-solid fa-envelope"

    # Tokens are generated by the system with proper hashing — not via admin
    can_create = False

    column_list = [
        EmailToken.id,
        EmailToken.user_id,
        EmailToken.purpose,
        EmailToken.new_email,
        EmailToken.expires_at,
        EmailToken.used_at,
        EmailToken.invalidated_at,
        EmailToken.created_at,
    ]
    column_searchable_list = [EmailToken.new_email]
    column_sortable_list = [
        EmailToken.purpose,
        EmailToken.expires_at,
        EmailToken.used_at,
        EmailToken.created_at,
    ]
    column_default_sort = [(EmailToken.created_at, True)]
    page_size = 25
    page_size_options = [10, 25, 50, 100]

    @action(
        name="invalidate_tokens",
        label="Invalidate selected",
        confirmation_message="Mark selected tokens as invalidated?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def invalidate_tokens(self, request: Request) -> Response:
        ids = _selected_ids(request)
        if ids:
            async with AsyncSessionFactory() as session:
                await session.execute(
                    update(EmailToken)
                    .where(
                        EmailToken.id.in_(ids),
                        EmailToken.invalidated_at.is_(None),
                    )
                    .values(invalidated_at=utcnow())
                )
                await session.commit()
            logger.info(
                "admin.email_tokens_invalidated", token_ids=[str(i) for i in ids]
            )
        return _back_to_list(request, self.identity)


class SessionAdmin(ModelView, model=Session):
    name_plural = "Sessions"
    icon = "fa-solid fa-terminal"

    column_list = [
        Session.id,
        Session.user_id,
        Session.is_revoked,
        Session.revoke_reason,
        Session.expires_at,
        Session.device_name,
        Session.device_id,
        Session.ip_address,
        Session.created_at,
        Session.revoked_at,
    ]
    # Detail view also shows token_hash, family_id, user_agent — useful for debugging
    column_searchable_list = [
        Session.ip_address,
        Session.device_name,
        Session.device_id,
    ]
    column_sortable_list = [
        Session.is_revoked,
        Session.expires_at,
        Session.created_at,
        Session.revoked_at,
    ]
    column_default_sort = [(Session.created_at, True)]
    page_size = 50
    page_size_options = [25, 50, 100, 200]

    @action(
        name="revoke_sessions",
        label="Revoke selected",
        confirmation_message="Revoke selected sessions? Users will need to log in again.",
        add_in_detail=True,
        add_in_list=True,
    )
    async def revoke_sessions(self, request: Request) -> Response:
        ids = _selected_ids(request)
        if ids:
            async with AsyncSessionFactory() as session:
                await session.execute(
                    update(Session)
                    .where(Session.id.in_(ids), Session.is_revoked.is_(False))
                    .values(is_revoked=True, revoke_reason="admin", revoked_at=utcnow())
                )
                await session.commit()
            logger.info("admin.sessions_revoked", session_ids=[str(i) for i in ids])
        return _back_to_list(request, self.identity)

    @action(
        name="revoke_all_expired",
        label="Revoke all expired",
        confirmation_message="Revoke all sessions that have already expired?",
        add_in_list=True,
    )
    async def revoke_all_expired(self, request: Request) -> Response:
        now = utcnow()
        async with AsyncSessionFactory() as session:
            await session.execute(
                update(Session)
                .where(and_(Session.expires_at < now, Session.is_revoked.is_(False)))
                .values(is_revoked=True, revoke_reason="expired", revoked_at=now)
            )
            await session.commit()
        return _back_to_list(request, self.identity)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_admin(app: FastAPI) -> Admin:
    auth = AdminAuth(secret_key=settings.ADMIN_SECRET_KEY.get_secret_value())
    admin = Admin(
        app,
        engine,
        authentication_backend=auth,
        title=f"{settings.APP_NAME} Admin",
    )
    admin.add_view(UserAdmin)
    admin.add_view(PasswordCredentialAdmin)
    admin.add_view(AuthAuditLogAdmin)
    admin.add_view(EmailTokenAdmin)
    admin.add_view(SessionAdmin)
    return admin
