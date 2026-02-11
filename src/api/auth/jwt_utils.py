# src/api/auth/jwt_utils.py

"""
JWT and refresh token utility functions.
"""

import uuid
import secrets
from datetime import datetime, timedelta
from typing import Optional, List
import jwt
from src.common.config import settings
from src.api.auth.models import TokenData, RefreshToken
from src.common.logging import get_logger

logger = get_logger(__name__)

# In-memory storage for refresh tokens (replace with Redis in production)
refresh_token_db: List[RefreshToken] = []


def create_access_token(
    username: str,
    user_id: str,
    expires_delta: Optional[timedelta] = None
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
        "nbf": datetime.utcnow()
    }
    
    encoded_jwt = jwt.encode(
        to_encode,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM
    )
    
    return encoded_jwt


def create_refresh_token(
    username: str,
    user_id: str,
    expires_delta: Optional[timedelta] = None
) -> str:
    """Create an opaque refresh token and store it."""
    if expires_delta is None:
        expires_delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    
    expire = datetime.utcnow() + expires_delta
    
    # Generate a secure random string for the opaque token
    opaque_token = secrets.token_urlsafe(32)
    
    # Create and store the refresh token object
    refresh_token_obj = RefreshToken(
        user_id=user_id,
        username=username,
        token=opaque_token,
        expires_at=expire
    )
    refresh_token_db.append(refresh_token_obj)
    
    logger.info(f"Refresh token created for user {username} with ID {refresh_token_obj.id}")
    
    return opaque_token


def validate_refresh_token(token: str) -> Optional[RefreshToken]:
    """Validate an opaque refresh token."""
    for rt in refresh_token_db:
        if rt.token == token and not rt.revoked and rt.expires_at > datetime.utcnow():
            return rt
    return None


def revoke_refresh_token(token: str) -> bool:
    """Revoke a refresh token."""
    for rt in refresh_token_db:
        if rt.token == token:
            rt.revoked = True
            logger.info(f"Refresh token {rt.id} revoked.")
            return True
    return False


def decode_token(token: str) -> Optional[TokenData]:
    """
    Decode and validate a JWT access token.
    Returns TokenData if valid, None if invalid.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM]
        )
        
        user_id: str = payload.get("sub")
        username: str = payload.get("username")
        jti: str = payload.get("jti")
        token_type: str = payload.get("type")
        
        if user_id is None or username is None or jti is None or token_type != "access":
            logger.warning("Invalid access token")
            return None
        
        return TokenData(
            user_id=user_id,
            username=username,
            jti=jti,
            type=token_type
        )
        
    except jwt.ExpiredSignatureError:
        logger.info("Access token has expired")
        return None
    except jwt.JWTError as e:
        logger.warning(f"JWT decode error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error decoding token: {e}")
        return None