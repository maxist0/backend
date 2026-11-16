# src/app/utils/jwt_utils.py

"""
JWT utility functions: create and decode access tokens.
Refresh-token management is handled in service.py / crud.py.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Final

import jwt

from app.api.auth.schemas import TokenData
from app.common.config import get_settings
from app.common.logging import get_logger

logger = get_logger(__name__)

# Pinned in code, never taken from configuration or from the token header
# (RFC 8725 section 3.1, OWASP JWT cheat sheet).
JWT_ALGORITHM: Final = "HS256"
# Claims every access token must carry; PyJWT rejects tokens without them.
_REQUIRED_CLAIMS: Final = ["exp", "iat", "nbf", "sub", "jti"]
_ACCESS_TOKEN_TYPE: Final = "access"


def create_access_token(
    username: str,
    user_id: str,
    session_id: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT access token.

    ``session_id`` is the refresh-token family id (claim ``sid``). It stays
    stable across refresh rotation and identifies the caller's session.
    """
    if expires_delta is None:
        expires_delta = timedelta(minutes=get_settings().ACCESS_TOKEN_EXPIRE_MINUTES)

    now = datetime.now(timezone.utc)
    to_encode = {
        "sub": user_id,
        "username": username,
        "sid": session_id,
        "jti": str(uuid.uuid4()),
        "type": _ACCESS_TOKEN_TYPE,
        "exp": now + expires_delta,
        "iat": now,
        "nbf": now,
    }

    return jwt.encode(
        to_encode,
        get_settings().JWT_SECRET_KEY.get_secret_value(),
        algorithm=JWT_ALGORITHM,
    )


def _parse_uuid(value: object) -> str | None:
    """Return the canonical string form of a UUID claim, or None if invalid."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def decode_token(token: str) -> TokenData | None:
    """
    Decode and validate a JWT access token.
    Returns TokenData if valid, None for any invalid, expired or malformed token.
    """
    try:
        payload = jwt.decode(
            token,
            get_settings().JWT_SECRET_KEY.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.ExpiredSignatureError:
        logger.info("jwt.token_expired")
        return None
    except jwt.PyJWTError as e:
        logger.warning("jwt.decode_error", error_type=type(e).__name__)
        return None

    if payload.get("type") != _ACCESS_TOKEN_TYPE:
        logger.warning("jwt.invalid_token", reason="wrong_type")
        return None

    user_id = _parse_uuid(payload.get("sub"))
    if user_id is None:
        logger.warning("jwt.invalid_token", reason="sub_not_uuid")
        return None

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        logger.warning("jwt.invalid_token", reason="invalid_jti")
        return None

    # `sid` is optional: tokens issued before it existed stay valid until they
    # expire (ACCESS_TOKEN_EXPIRE_MINUTES). If present, it must be a UUID.
    raw_sid = payload.get("sid")
    session_id = None
    if raw_sid is not None:
        session_id = _parse_uuid(raw_sid)
        if session_id is None:
            logger.warning("jwt.invalid_token", reason="sid_not_uuid")
            return None

    username = payload.get("username")
    return TokenData(
        user_id=user_id,
        username=username if isinstance(username, str) else None,
        jti=jti,
        session_id=session_id,
    )
