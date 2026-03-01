# src/utils/jwt_utils.py

"""
JWT utility functions: create and decode access tokens.
Refresh-token management is handled in service.py / crud.py.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from src.api.auth.schemas import TokenData
from src.common.config import settings
from src.common.logging import get_logger

logger = get_logger(__name__)


def create_access_token(
    username: str, user_id: str, expires_delta: Optional[timedelta] = None
) -> str:
    """Create a JWT access token."""
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    now = datetime.now(timezone.utc)
    expire = now + expires_delta
    jti = str(uuid.uuid4())

    to_encode = {
        "sub": user_id,
        "username": username,
        "jti": jti,
        "type": "access",
        "exp": expire,
        "iat": now,
        "nbf": now,
    }

    return jwt.encode(
        to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )


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
            logger.warning("jwt.invalid_token")
            return None

        return TokenData(user_id=user_id, username=username, jti=jti)

    except jwt.ExpiredSignatureError:
        logger.info("jwt.token_expired")
        return None
    except jwt.PyJWTError as e:
        logger.warning("jwt.decode_error", error=str(e))
        return None
    except Exception as e:
        logger.error("jwt.unexpected_error", error=str(e))
        return None
