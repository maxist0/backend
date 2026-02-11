from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Request


async def get_db_connection(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Dependency to get a database connection from the app state."""
    async with request.app.state.pool.aquire() as conn:
        yield conn
