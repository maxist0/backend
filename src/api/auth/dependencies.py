# src/api/auth/dependencies.py

"""
Simple JWT authentication dependencies.
"""

from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from src.api.auth.jwt_utils import decode_token
from src.api.auth.models import TokenData
from src.common.logging import get_logger

logger = get_logger(__name__)

# Security scheme for JWT tokens
security = HTTPBearer()


class AuthenticationError(HTTPException):
    """Authentication error exception."""
    def __init__(self, detail: str = "Authentication failed"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_token_data(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> TokenData:
    """
    Get and validate the current JWT access token.
    
    Raises:
        AuthenticationError: If token is invalid or not an access token.
    """
    token = credentials.credentials
    
    token_data = decode_token(token)
    if not token_data:
        raise AuthenticationError("Invalid token")
    
    if token_data.type != "access":
        raise AuthenticationError("Invalid token type, expected access token")
    
    return token_data


async def get_current_user(
    token_data: TokenData = Depends(get_current_token_data)
) -> dict:
    """
    Get the current authenticated user data from the token.
    """
    return {
        "id": token_data.user_id,
        "username": token_data.username
    }


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        HTTPBearer(auto_error=False)
    )
) -> Optional[dict]:
    """
    Get the current user if authenticated, None otherwise.
    """
    if not credentials:
        return None
    
    try:
        token = credentials.credentials
        token_data = decode_token(token)
        if not token_data or token_data.type != "access":
            return None
        
        return {
            "id": token_data.user_id,
            "username": token_data.username
        }
        
    except Exception as e:
        logger.warning(f"Error in optional authentication: {e}")
        return None