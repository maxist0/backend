# src/app/common/dependencies.py

"""
Shared FastAPI dependency annotations.

    from app.common.dependencies import DbSession, SettingsDep

    async def my_handler(db: DbSession, settings: SettingsDep) -> None: ...
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.config import Settings, get_settings
from app.common.database import get_db

# Reusable typed dependency alias – use this everywhere instead of
# ``db: AsyncSession = Depends(get_db)``.
DbSession = Annotated[AsyncSession, Depends(get_db)]

# Process settings in routes; tests override ``get_settings`` via
# ``app.dependency_overrides`` or ``get_settings.cache_clear()``.
SettingsDep = Annotated[Settings, Depends(get_settings)]
