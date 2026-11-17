"""HTTP routes of the users module: the current user's profile (`/users/me`).

`PUT /users/me/password` and `PUT /users/me/email` need credentials and
email tokens, so the auth module serves them under the same path and tag.
"""

from fastapi import APIRouter, status

from app.core import DbSession, Unauthorized, problem_responses
from app.modules.users import service
from app.modules.users.dependencies import CurrentUser
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
    responses=problem_responses(Unauthorized, 422),
)
async def update_me(
    body: UpdateProfileRequest, db: DbSession, current_user: CurrentUser
) -> UserResponse:
    """Update profile fields of the signed-in user.

    Omitted or null fields stay unchanged. The email address cannot be changed
    here; use `PUT /users/me/email`.
    """
    user = await service.update_profile(
        db, current_user, display_name=body.display_name
    )
    return service.user_to_response(user)
