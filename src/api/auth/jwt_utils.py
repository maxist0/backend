# src/api/auth/jwt_utils.py

"""
JWT utility functions: create, validate, revoke and decode tokens.
All persistence is delegated to crud.py.
"""

import uuid
import secrets
from datetime import datetime, timedelta
from typing import Optional
import jwt
from src.common.config import settings
from src.api.auth.models import TokenData, RefreshToken
from src.api.auth import crud
from src.common.logging import get_logger

logger = get_logger(__name__)


def create_access_token(
    username: str, user_id: str, expires_delta: Optional[timedelta] = None
) -> str:
    """Create a JWT access token."""
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    expire = datetime.utcnow() + expires_delta
    jti = str(uuid.uuid4())

    to_encode = {
        "sub": user_id,
        "username": username,
        "jti": jti,
        "type": "access",
        "exp": expire,
        "iat": datetime.utcnow(),
        "nbf": datetime.utcnow(),
    }

    encoded_jwt = jwt.encode(
        to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )

    return encoded_jwt


def create_refresh_token(
    username: str, user_id: str, expires_delta: Optional[timedelta] = None
) -> str:
    """Create an opaque refresh token and store it."""
    if expires_delta is None:
        expires_delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    expire = datetime.utcnow() + expires_delta

    # Generate a secure random string for the opaque token
    opaque_token = secrets.token_urlsafe(32)

    # Persist via crud
    refresh_token_obj = RefreshToken(
        user_id=user_id, username=username, token=opaque_token, expires_at=expire
    )
    crud.save_refresh_token(refresh_token_obj)

    logger.info(
        f"Refresh token created for user {username} with ID {refresh_token_obj.id}"
    )

    return opaque_token


def validate_refresh_token(token: str) -> Optional[RefreshToken]:
    """Return the active RefreshToken for *token*, or None if invalid/expired."""
    return crud.get_active_refresh_token(token)


def revoke_refresh_token(token: str) -> bool:
    """Revoke a refresh token. Returns True if the token was found."""
    revoked = crud.revoke_refresh_token(token)
    if revoked:
        logger.info(f"Refresh token revoked: {token[:8]}...")
    return revoked


def decode_token(token: str) -> Optional[TokenData]:
    """
    Decode and validate a JWT access token.
    Returns TokenData if valid, None if invalid.
    """
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )

        user_id: str = str(payload.get("sub"))
        username: str = str(payload.get("username"))
        jti: str = str(payload.get("jti"))
        

        if user_id is None or username is None or jti is None:
            logger.warning("Invalid access token")
            return None

        return TokenData(user_id=user_id, username=username, jti=jti)

    except jwt.ExpiredSignatureError:
        logger.info("Access token has expired")
        return None
    except jwt.PyJWTError as e:
        logger.warning(f"JWT decode error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error decoding token: {e}")
        return None
