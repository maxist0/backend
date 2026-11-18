"""Admin panel views of the auth module, registered via `Module.admin_views`.

The views are read-only. Every change goes through an action that calls an
auth service function, so it is audited and follows the lock order. The
`User` view lives here, not in the users module: its actions end sessions
and touch credentials, which belong to auth, and users must not import auth.
"""

import uuid
from typing import ClassVar, Protocol

from sqladmin import ModelView, action
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from app.core import back_to_list, get_settings, selected_ids, session_factory
from app.modules.auth import service
from app.modules.auth.models import AuthAuditLog, Session
from app.modules.users import User


class _AdminOperation(Protocol):
    async def __call__(
        self,
        db: AsyncSession,
        pk: uuid.UUID,
        /,
        *,
        actor: str,
        ip_address: str | None = None,
    ) -> bool: ...


async def _run_for_selected(
    view: ModelView, request: Request, operation: _AdminOperation
) -> Response:
    """Apply `operation` to every selected row; it commits once per row."""
    actor = get_settings().ADMIN_USERNAME
    ip_address = request.client.host if request.client else None
    async with session_factory(view)() as db:
        for pk in selected_ids(request):
            await operation(db, pk, actor=actor, ip_address=ip_address)
    return back_to_list(request, view.identity)


class UserAdmin(ModelView, model=User):
    """Users, read-only; lock, unlock, deactivate and reactivate as actions."""

    name_plural = "Users"
    icon = "fa-solid fa-users"

    can_create = False
    can_edit = False
    can_delete = False

    column_list: ClassVar = [
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
    column_searchable_list: ClassVar = [User.username, User.email, User.display_name]
    column_sortable_list: ClassVar = [
        User.username,
        User.email,
        User.is_active,
        User.is_locked,
        User.created_at,
        User.last_login_at,
        User.failed_login_attempts,
    ]
    column_default_sort: ClassVar = [(User.created_at, True)]
    page_size = 25
    page_size_options: ClassVar = [10, 25, 50, 100]

    @action(
        name="lock_users",
        label="Lock selected",
        confirmation_message="Lock the selected users and end all their sessions?",
    )
    async def lock_users(self, request: Request) -> Response:
        """Lock the selected users indefinitely and end their sessions."""
        return await _run_for_selected(self, request, service.admin_lock_user)

    @action(
        name="unlock_users",
        label="Unlock selected",
        confirmation_message="Unlock the selected users and reset failed logins?",
    )
    async def unlock_users(self, request: Request) -> Response:
        """Unlock the selected users and reset their failed logins."""
        return await _run_for_selected(self, request, service.admin_unlock_user)

    @action(
        name="deactivate_users",
        label="Deactivate selected",
        confirmation_message=(
            "Deactivate the selected users and end all their sessions?"
        ),
    )
    async def deactivate_users(self, request: Request) -> Response:
        """Deactivate the selected users and end their sessions."""
        return await _run_for_selected(self, request, service.admin_deactivate_user)

    @action(
        name="reactivate_users",
        label="Reactivate selected",
        confirmation_message="Reactivate the selected users?",
    )
    async def reactivate_users(self, request: Request) -> Response:
        """Reactivate the selected users."""
        return await _run_for_selected(self, request, service.admin_reactivate_user)

    @action(
        name="force_password_change",
        label="Force password change",
        confirmation_message="Flag the selected users' passwords for a change?",
    )
    async def force_password_change(self, request: Request) -> Response:
        """Flag the selected users' passwords for a change."""
        return await _run_for_selected(
            self, request, service.admin_force_password_change
        )


class AuthAuditLogAdmin(ModelView, model=AuthAuditLog):
    """The audit log, read-only."""

    name = "Audit Log"
    name_plural = "Audit Logs"
    icon = "fa-solid fa-clipboard-list"

    can_create = False
    can_edit = False
    can_delete = False

    column_list: ClassVar = [
        AuthAuditLog.id,
        AuthAuditLog.user_id,
        AuthAuditLog.event_type,
        AuthAuditLog.ip_address,
        AuthAuditLog.user_agent,
        AuthAuditLog.detail,
        AuthAuditLog.created_at,
    ]
    column_searchable_list: ClassVar = [AuthAuditLog.ip_address, AuthAuditLog.detail]
    column_sortable_list: ClassVar = [AuthAuditLog.created_at, AuthAuditLog.event_type]
    column_default_sort: ClassVar = [(AuthAuditLog.created_at, True)]
    page_size = 50
    page_size_options: ClassVar = [25, 50, 100, 200]


class SessionAdmin(ModelView, model=Session):
    """Refresh-token sessions, read-only; revoking is an action."""

    name_plural = "Sessions"
    icon = "fa-solid fa-terminal"

    can_create = False
    can_edit = False
    can_delete = False

    column_list: ClassVar = [
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
    column_searchable_list: ClassVar = [
        Session.ip_address,
        Session.device_name,
        Session.device_id,
    ]
    column_sortable_list: ClassVar = [
        Session.is_revoked,
        Session.expires_at,
        Session.created_at,
        Session.revoked_at,
    ]
    column_default_sort: ClassVar = [(Session.created_at, True)]
    page_size = 50
    page_size_options: ClassVar = [25, 50, 100, 200]

    @action(
        name="revoke_sessions",
        label="Revoke selected",
        confirmation_message=(
            "Revoke the selected sessions? Their users must log in again."
        ),
    )
    async def revoke_sessions(self, request: Request) -> Response:
        """Revoke the selected sessions."""
        return await _run_for_selected(self, request, service.admin_revoke_session)


VIEWS: tuple[type[ModelView], ...] = (UserAdmin, AuthAuditLogAdmin, SessionAdmin)
