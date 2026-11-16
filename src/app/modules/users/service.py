"""Business logic of the users module: the public user representation."""

from app.modules.users.models import User
from app.modules.users.schemas import UserResponse


def user_to_response(user: User) -> UserResponse:
    """Convert a User ORM object to a UserResponse Pydantic model."""
    return UserResponse(
        id=str(user.id),
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        profile_picture_url="",  # Placeholder for profile picture URL
        is_active=user.is_active,
        is_email_verified=user.is_email_verified,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        roles=["test"],  # Placeholder for role-based access control
        permissions=["posts:create", "posts:read"],  # Placeholder for permissions
        preferences={
            "locale": "en-US",
            "theme": "dark",
            "timezone": "UTC",
        },
    )
