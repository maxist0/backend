"""HTTP routes of the users module: the current user's profile (`/users/me`).

`PUT /users/me/password` and `PUT /users/me/email` need credentials and
email tokens, so the auth module serves them under the same path and tag.
"""

from fastapi import APIRouter, status

from app.core import (
    DbSession,
    RateLimitedError,
    Unauthorized,
    problem_responses,
    rate_limit,
)
from app.modules.users import service
from app.modules.users.dependencies import CurrentUser, Idempotent, by_user
from app.modules.users.schemas import UpdateProfileRequest, UserResponse

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/me",
    status_code=status.HTTP_200_OK,
    summary="Get the current user",
    responses=problem_responses(Unauthorized),
)
async def get_me(current_user: CurrentUser) -> UserResponse:
    """Return the profile of the signed-in user."""
    return service.user_to_response(current_user)


@router.patch(
    "/me",
    status_code=status.HTTP_200_OK,
    summary="Update the current user's profile",
    dependencies=[
        rate_limit("users.profile_update.user", "30/minute", key=by_user),
        Idempotent,
    ],
    responses=problem_responses(Unauthorized, 422, RateLimitedError),
)
async def update_me(
    body: UpdateProfileRequest, db: DbSession, current_user: CurrentUser
) -> UserResponse:
    """Update profile fields of the signed-in user.

    Omitted or null fields stay unchanged. The email address cannot be changed
    here; use `PUT /users/me/email`. At most 30 updates per minute per user;
    more get 429 with `Retry-After`.
    """
    user = await service.update_profile(
        db, current_user, display_name=body.display_name
    )
    return service.user_to_response(user)
