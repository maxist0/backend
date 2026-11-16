"""HTTP routes of the users module: the current user's profile (`/user`).

`PATCH /user/me` and `PATCH /user/me/password` need email tokens and
credentials, so the auth module serves them under the same prefix and tag.
"""

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.responses import ok_response
from app.modules.users.dependencies import CurrentUser
from app.modules.users.service import user_to_response

router = APIRouter(prefix="/user", tags=["user"])


@router.get("/me")
async def get_me(
    request: Request,
    current_user: CurrentUser,
) -> JSONResponse:
    """Return the currently authenticated user's profile."""
    start_time = datetime.now()
    return ok_response(
        data={"user": user_to_response(current_user).model_dump(mode="json")},
        request=request,
        start_time=start_time,
    )
