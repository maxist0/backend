"""Admin panel views of the auth module, registered via `Module.admin_views`.

The views are read-only. Every change goes through an action that calls an
auth service function, so it is audited and follows the lock order. The
`User` view lives here, not in the users module: its actions end sessions
and touch credentials, which belong to auth, and users must not import auth.
"""

import uuid
from typing import Protocol

from sqladmin import ModelView, action
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from app.core import get_settings
from app.core.admin import back_to_list, selected_ids, session_factory
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
    name_plural = "Users"
    icon = "fa-solid fa-users"

    can_create = False
    can_edit = False
    can_delete = False

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
        confirmation_message="Lock the selected users and end all their sessions?",
    )
    async def lock_users(self, request: Request) -> Response:
        return await _run_for_selected(self, request, service.admin_lock_user)

    @action(
        name="unlock_users",
        label="Unlock selected",
        confirmation_message="Unlock the selected users and reset failed logins?",
    )
    async def unlock_users(self, request: Request) -> Response:
        return await _run_for_selected(self, request, service.admin_unlock_user)

    @action(
        name="deactivate_users",
        label="Deactivate selected",
        confirmation_message="Deactivate the selected users and end all their sessions?",
    )
    async def deactivate_users(self, request: Request) -> Response:
        return await _run_for_selected(self, request, service.admin_deactivate_user)

    @action(
        name="reactivate_users",
        label="Reactivate selected",
        confirmation_message="Reactivate the selected users?",
    )
    async def reactivate_users(self, request: Request) -> Response:
        return await _run_for_selected(self, request, service.admin_reactivate_user)

    @action(
        name="force_password_change",
        label="Force password change",
        confirmation_message="Flag the selected users' passwords for a change?",
    )
    async def force_password_change(self, request: Request) -> Response:
        return await _run_for_selected(
            self, request, service.admin_force_password_change
        )


class AuthAuditLogAdmin(ModelView, model=AuthAuditLog):
    name = "Audit Log"
    name_plural = "Audit Logs"
    icon = "fa-solid fa-clipboard-list"

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


class SessionAdmin(ModelView, model=Session):
    name_plural = "Sessions"
    icon = "fa-solid fa-terminal"

    can_create = False
    can_edit = False
    can_delete = False

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
        confirmation_message="Revoke the selected sessions? Their users must log in again.",
    )
    async def revoke_sessions(self, request: Request) -> Response:
        return await _run_for_selected(self, request, service.admin_revoke_session)


VIEWS: tuple[type[ModelView], ...] = (UserAdmin, AuthAuditLogAdmin, SessionAdmin)
