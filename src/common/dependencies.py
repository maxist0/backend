# src/common/dependencies.py

"""
Shared FastAPI dependency annotations.

Import `DbSession` in any router/service to get a typed async db session:

    from src.common.dependencies import DbSession

    async def my_handler(db: DbSession) -> None: ...
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.database import get_db

# Reusable typed dependency alias – use this everywhere instead of
# ``db: AsyncSession = Depends(get_db)``.
DbSession = Annotated[AsyncSession, Depends(get_db)]
