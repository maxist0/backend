# src/api/auth/dependencies.py

from typing import Optional
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from src.api.auth.jwt_utils import decode_token
from src.api.auth.models import RefreshToken, TokenData, UserRecord
from src.common.logging import get_logger
from src.api.auth.jwt_utils import validate_refresh_token

logger = get_logger(__name__)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login",refreshUrl="/auth/refresh", auto_error=True)


class AuthenticationError(HTTPException):
    def __init__(self, detail: str = "Authentication failed"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_token_data(
    token: str = Depends(oauth2_scheme),
) -> TokenData:
    token_data = decode_token(token)
    if not token_data:
        raise AuthenticationError("Invalid or expired access token")
    return token_data


async def get_current_user(
    token_data: TokenData = Depends(get_current_token_data),
) -> dict:
    return {"id": token_data.user_id, "username": token_data.username}


async def get_optional_user(
    token: Optional[str] = Depends(OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)),
) -> Optional[dict]:
    if not token:
        return None
    try:
        token_data = decode_token(token)
        if not token_data:
            return None
        return {"id": token_data.user_id, "username": token_data.username}
    except Exception as e:
        logger.warning(f"Error in optional authentication: {e}")
        return None


async def authenticate_form_user(
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> UserRecord:
    from src.api.auth.service import authenticate_user

    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise AuthenticationError("Invalid username or password")
    return user


async def get_current_refresh_token_data(
    request: Request,
) -> RefreshToken:
    """Extract and validate the refresh token from the httpOnly cookie.
    Does NOT require a valid access token — this is intentional so that
    /auth/refresh works even when the access token has expired.
    """
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        logger.info(f"Refresh token found in cookie: {refresh_token[:8]}...")
    else:
        logger.info("No refresh token found in cookies")
        raise AuthenticationError("Refresh token missing")
    redis_client = request.app.state.cache
    refreshtoken = await validate_refresh_token(refresh_token, redis_client)
    if not refreshtoken:
        raise AuthenticationError("Invalid or expired refresh token")
    return refreshtoken