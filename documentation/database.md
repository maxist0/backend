# Database Architecture

## Overview

The project uses **PostgreSQL** as the primary relational database with **SQLAlchemy 2.0** for ORM and **asyncpg** for async database connectivity. Database schema is managed via **Alembic** migrations. The async/await pattern enables non-blocking I/O for high-concurrency scenarios.

## Database Stack

| Component | Purpose | Version |
|-----------|---------|---------|
| PostgreSQL | Relational database | 12+ |
| SQLAlchemy | ORM + Query builder | 2.0+ |
| asyncpg | Async PostgreSQL driver | 0.29+ |
| Alembic | Schema migrations | 1.13+ |

## Configuration

Database settings are managed in [src/common/config.py](../src/common/config.py):

```python
# Connection URL
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/api_db

# Connection Pool
DB_POOL_SIZE=10                 # Min connections to maintain
DB_MAX_OVERFLOW=20              # Max additional connections above pool_size
DB_POOL_TIMEOUT=30              # Seconds to wait for available connection
DB_POOL_RECYCLE=1800            # Recycle connections after 30 minutes
```

### Environment-Specific URLs

Each environment has its own database:

```ini
# .env.dev
DATABASE_URL=postgresql+asyncpg://dev:dev@localhost:5432/api_dev

# .env.test
DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/api_test

# .env.stage
DATABASE_URL=postgresql+asyncpg://user:pass@db.stage.internal:5432/api_stage

# .env.prod
DATABASE_URL=postgresql+asyncpg://user:pass@db.prod.internal:5432/api_prod
```

## Engine and Session Management

### Async Engine

Created once per application lifecycle in [src/common/database.py](../src/common/database.py):

```python
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

# Engine is created with connection pool
engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    pool_pre_ping=True,          # Verify stale connections are discarded
    echo=False,                   # Set True for SQL debugging
    future=True,                  # SQLAlchemy 2.0 style
)
```

### Session Factory

Async session factory for creating new sessions:

```python
AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,      # Avoid implicit I/O after commit
    autoflush=False,             # Explicit flush control
    autocommit=False,            # Explicit transaction control
)
```

### Session Usage Pattern

In route handlers via dependency injection:

```python
from src.common.dependencies import DbSession

@app.get("/users/{user_id}")
async def get_user(user_id: UUID, db: DbSession) -> UserResponse:
    # db is AsyncSession, automatically injected and cleaned up
    user = await db.get(User, user_id)
    return UserResponse.from_orm(user)
```

## Declarative Base

All ORM models inherit from `Base`:

```python
from src.common.database import Base
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String

class User(Base):
    __tablename__ = "users"
    
    id: Mapped[UUID] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
```

## Application Lifecycle

### Startup (`src/main.py` lifespan)

```python
async def lifespan(app: FastAPI):
    # Startup
    await init_db()  # Verify database connectivity
    
    yield
    
    # Shutdown
    await close_db()  # Dispose engine and connection pool
```

**`init_db()`**: Executes `SELECT 1` to verify PostgreSQL connectivity
**`close_db()`**: Calls `engine.dispose()` to properly close all connections

## Database Migrations

Alembic manages schema versioning and changes.

### Migration Files Location

```
alembic/
├── versions/
│   ├── 20260226_2120_37a0d49f8bbc_add_auth_user_tables.py
│   └── [future migrations]
├── env.py                      # Alembic runtime configuration
├── script.py.mako              # Migration template
└── __init__.py
```

### Creating Migrations

Alembic can auto-detect schema changes:

```bash
# Generate migration (auto-detects model changes)
alembic revision --autogenerate -m "Add user_roles table"

# Generate blank migration (for custom SQL)
alembic revision -m "Create custom index"
```

### Migration Structure

```python
"""add_auth_user_tables

Revision ID: 37a0d49f8bbc
Revises: 
Create Date: 2026-02-26 21:20:56.805937+00:00
"""
from alembic import op
import sqlalchemy as sa

revision: str = '37a0d49f8bbc'
down_revision: Union[str, None] = None

def upgrade() -> None:
    """Apply this migration (moving forward)."""
    op.create_table('users', ...)
    op.create_index('ix_users_email', 'users', ['email'])

def downgrade() -> None:
    """Revert this migration."""
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')
```

### Applying Migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Apply specific number of migrations
alembic upgrade +1

# Revert last migration
alembic downgrade -1

# Check current migration version
alembic current

# View migration history
alembic history
```

### In Docker/Kubernetes

Migrations typically run as init container or pre-deployment job:

```bash
# In docker-compose.yml
services:
  api:
    ...
    depends_on:
      - postgres
    command: sh -c "alembic upgrade head && python app.py prod"
```

## Connection Pool Management

### Pool Lifecycle

1. **Initialization**: `pool_size` connections created on first use
2. **Overflow**: Additional connections (up to max_overflow) created on demand
3. **Recycling**: Connections recycled after `pool_recycle` seconds to handle DB restarts
4. **Pre-ping**: Each connection pinged before use to detect stale connections
5. **Disposal**: All connections closed on application shutdown

### Pool Configuration Tuning

**Development** (few concurrent users):
```python
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10
```

**Production** (many concurrent users):
```python
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=40
```

Rule of thumb: `(workers × threads_per_worker) + 5`

### Monitoring Pool Health

```python
# In application code
from src.common.database import engine

pool = engine.pool
print(f"Size: {pool.size()}")
print(f"Checked in: {pool.checkedin()}")
print(f"Checked out: {pool.checkedout()}")
```

## Query Patterns

### Select Operations

```python
from sqlalchemy import select

# Get single user by ID
user = await db.get(User, user_id)

# Select with filter
stmt = select(User).where(User.email == "john@example.com")
user = await db.scalar(stmt)

# Select multiple
stmt = select(User).where(User.is_active == True)
users = await db.scalars(stmt)
result = users.all()

# With order and limit
stmt = select(User).order_by(User.created_at.desc()).limit(10)
recent_users = await db.scalars(stmt)
```

### Insert Operations

```python
from src.common.database import AsyncSessionFactory

async with AsyncSessionFactory() as db:
    user = User(username="john", email="john@example.com")
    db.add(user)
    await db.commit()
    # user.id is now populated
```

### Update Operations

```python
# Fetch, modify, commit
user = await db.get(User, user_id)
user.username = "jane"
await db.commit()

# Or use update() for bulk changes
stmt = update(User).where(User.is_active == False).values(is_locked=True)
await db.execute(stmt)
await db.commit()
```

### Delete Operations

```python
# Fetch and delete
user = await db.get(User, user_id)
await db.delete(user)
await db.commit()

# Or bulk delete
stmt = delete(User).where(User.created_at < cutoff_date)
result = await db.execute(stmt)
await db.commit()
```

## Relationships and Eager Loading

### Relationship Definition

```python
from sqlalchemy.orm import relationship

class User(Base):
    __tablename__ = "users"
    
    id: Mapped[UUID] = mapped_column(primary_key=True)
    
    # One-to-many: user has many sessions
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",  # Delete sessions when user deleted
    )
    
    # One-to-one: user has one password credential
    password_credential: Mapped[Optional["PasswordCredential"]] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
```

### Lazy Loading

By default, relationships are **lazy-loaded** (fetched on access):

```python
user = await db.get(User, user_id)
# Accessing user.sessions triggers a new query
sessions = user.sessions  # Implicit lazy load
```

In async context, this causes issues. Use **eager loading** instead:

```python
from sqlalchemy.orm import selectinload, joinedload

# selectinload: Separate query for relationships
stmt = select(User).options(selectinload(User.sessions))
user = await db.scalar(stmt)
sessions = user.sessions  # Already loaded

# joinedload: Single JOIN query
stmt = select(User).options(joinedload(User.password_credential))
user = await db.scalar(stmt)
cred = user.password_credential  # Already loaded
```

## Transaction Management

### Default Behavior

Each route handler gets its own session with explicit control:

```python
async def create_user(db: DbSession, username: str) -> User:
    user = User(username=username)
    db.add(user)
    await db.commit()  # Explicitly commit
    return user
```

### Nested Transactions (Savepoints)

```python
async with db.begin_nested():  # Savepoint
    user = User(username="john")
    db.add(user)
    # If error here, only this block rolls back
    
# Continue even if nested transaction rolled back
```

### Manual Rollback

```python
try:
    user = User(username="john")
    db.add(user)
    await db.commit()
except IntegrityError:
    await db.rollback()  # Undo all changes
    raise
```

## Performance Optimization

### Indexes

Critical columns are indexed in migrations:

```python
op.create_index('ix_users_email', 'users', ['email'], unique=True)
op.create_index('ix_sessions_user_id', 'sessions', ['user_id'])
```

Query indexes when:
- Filtering frequently (`WHERE email = ...`)
- Sorting (`ORDER BY created_at`)
- Foreign keys (`user_id`)

### Query Optimization

```python
# Avoid N+1 queries: Eager load relationships
stmt = select(User).options(selectinload(User.sessions))

# Limit result sets
stmt = select(User).limit(100)

# Use indexes in WHERE clauses
stmt = select(User).where(User.email == "john@example.com")  # indexed
```

### Connection Pool Sizing

Monitor pool exhaustion in production:

```python
# Pool metrics (custom implementation)
pool.checkedout()  # Connections currently in use
pool.size()        # Current pool size
```

If pool exhausted frequently, increase `DB_POOL_SIZE`.

## Debugging

### Enable SQL Echo

```python
# In src/common/database.py, temporarily set:
echo=True  # Logs all SQL statements

# Then run app - will print SQL to logs
```

### Check Executed Queries

In route handlers:

```python
from sqlalchemy import event

@event.listens_for(Engine, "before_cursor_execute")
def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    logger.debug("sql.execute", statement=statement, params=parameters)
```

## Database Backups

In production, set up automated backups:

```bash
# Manual backup
pg_dump postgresql://user:pass@db.prod.internal:5432/api_prod > backup.sql

# Restore from backup
psql postgresql://user:pass@db.prod.internal:5432/api_prod < backup.sql
```

---

**Related Documentation:**
- See [database_schemas/user.md](database_schemas/user.md) for complete schema documentation
- See [environment.md](environment.md) for environment-specific configuration
