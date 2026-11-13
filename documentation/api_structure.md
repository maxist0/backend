# API Structure and Example Route

## Example Route Module: `/api/exampleRoute`

This directory demonstrates the **standard module structure** used throughout the API. Follow this pattern when creating new API endpoints.

```
exampleRoute/
├── __init__.py           # Module initialization
├── models.py             # Pydantic schemas + SQLAlchemy ORM
├── router.py             # FastAPI route definitions
├── service.py            # Business logic layer
├── curd.py               # Database CRUD operations
├── dependencies.py       # Route dependencies
└── __pycache__/          # Python cache (auto-generated)
```

## Module Components

### `models.py` - Data Models

Defines both **SQLAlchemy ORM models** (database) and **Pydantic schemas** (API):

```python
# src/api/exampleRoute/models.py

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field
from sqlalchemy import String, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from src.common.database import Base

# ────────────────────────────────────────────────────────────────
# SQLAlchemy ORM Model – represents database table
# ────────────────────────────────────────────────────────────────

class Example(Base):
    """Example data model stored in 'examples' table."""
    
    __tablename__ = "examples"
    
    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

# ────────────────────────────────────────────────────────────────
# Pydantic Request/Response Schemas
# ────────────────────────────────────────────────────────────────

class ExampleCreate(BaseModel):
    """Schema for creating new example."""
    name: str = Field(..., min_length=1, max_length=128, description="Example name")
    description: Optional[str] = Field(None, max_length=512, description="Optional description")

class ExampleUpdate(BaseModel):
    """Schema for updating existing example."""
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=512)
    is_active: Optional[bool] = None

class ExampleResponse(BaseModel):
    """Schema for API response."""
    id: UUID
    name: str
    description: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)  # Enable ORM mode
```

### `curd.py` - Database Operations

Low-level database CRUD (Create, Read, Update, Delete) operations:

```python
# src/api/exampleRoute/curd.py

from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.exampleRoute.models import Example, ExampleCreate, ExampleUpdate

class ExampleCRUD:
    """Database operations for examples."""
    
    @staticmethod
    async def create(db: AsyncSession, obj: ExampleCreate) -> Example:
        """Create new example in database."""
        db_obj = Example(**obj.model_dump())
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    
    @staticmethod
    async def get_by_id(db: AsyncSession, example_id: UUID) -> Optional[Example]:
        """Get example by ID."""
        return await db.get(Example, example_id)
    
    @staticmethod
    async def get_all(db: AsyncSession, skip: int = 0, limit: int = 100) -> list[Example]:
        """Get all examples with pagination."""
        stmt = select(Example).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return result.scalars().all()
    
    @staticmethod
    async def update(
        db: AsyncSession, example_id: UUID, obj: ExampleUpdate
    ) -> Optional[Example]:
        """Update example."""
        db_obj = await db.get(Example, example_id)
        if not db_obj:
            return None
        
        update_data = obj.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_obj, field, value)
        
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    
    @staticmethod
    async def delete(db: AsyncSession, example_id: UUID) -> bool:
        """Delete example."""
        db_obj = await db.get(Example, example_id)
        if not db_obj:
            return False
        
        await db.delete(db_obj)
        await db.commit()
        return True
```

### `service.py` - Business Logic

Orchestrates CRUD operations, implements business logic, handles errors:

```python
# src/api/exampleRoute/service.py

from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.exampleRoute.models import ExampleCreate, ExampleUpdate, ExampleResponse
from src.api.exampleRoute.curd import ExampleCRUD
from src.common.logging import get_logger

logger = get_logger(__name__)

class ExampleService:
    """Service layer for example business logic."""
    
    @staticmethod
    async def create_example(
        db: AsyncSession, data: ExampleCreate
    ) -> ExampleResponse:
        """
        Create new example with validation.
        
        Args:
            db: Database session
            data: Example data
            
        Returns:
            Created example
            
        Raises:
            ValueError: If validation fails
        """
        # Validation: name must be unique
        existing = await db.execute(
            select(Example).where(Example.name == data.name)
        )
        if existing.scalars().first():
            logger.warning("example.duplicate_name", name=data.name)
            raise ValueError(f"Example with name '{data.name}' already exists")
        
        # Create
        db_obj = await ExampleCRUD.create(db, data)
        logger.info("example.created", example_id=db_obj.id, name=db_obj.name)
        
        return ExampleResponse.from_orm(db_obj)
    
    @staticmethod
    async def get_example(
        db: AsyncSession, example_id: UUID
    ) -> Optional[ExampleResponse]:
        """Get example by ID."""
        db_obj = await ExampleCRUD.get_by_id(db, example_id)
        if not db_obj:
            logger.debug("example.not_found", example_id=example_id)
            return None
        
        return ExampleResponse.from_orm(db_obj)
    
    @staticmethod
    async def list_examples(
        db: AsyncSession, skip: int = 0, limit: int = 100
    ) -> list[ExampleResponse]:
        """List all examples with pagination."""
        db_objs = await ExampleCRUD.get_all(db, skip=skip, limit=limit)
        return [ExampleResponse.from_orm(obj) for obj in db_objs]
    
    @staticmethod
    async def update_example(
        db: AsyncSession, example_id: UUID, data: ExampleUpdate
    ) -> Optional[ExampleResponse]:
        """Update example."""
        db_obj = await ExampleCRUD.update(db, example_id, data)
        if not db_obj:
            logger.debug("example.not_found", example_id=example_id)
            return None
        
        logger.info("example.updated", example_id=example_id)
        return ExampleResponse.from_orm(db_obj)
    
    @staticmethod
    async def delete_example(
        db: AsyncSession, example_id: UUID
    ) -> bool:
        """Delete example."""
        success = await ExampleCRUD.delete(db, example_id)
        if success:
            logger.info("example.deleted", example_id=example_id)
        else:
            logger.debug("example.not_found", example_id=example_id)
        
        return success
```

### `dependencies.py` - Route Injections

Define dependencies for route handlers (database session, auth, etc.):

```python
# src/api/exampleRoute/dependencies.py

from uuid import UUID
from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.common.dependencies import DbSession
from src.api.exampleRoute.models import Example, ExampleResponse

async def get_example_or_404(
    example_id: UUID, db: DbSession
) -> ExampleResponse:
    """
    Dependency to get example or raise 404.
    
    Usage:
        @router.get("/examples/{example_id}")
        async def get_example(example: ExampleResponse = Depends(get_example_or_404)):
            return example
    """
    example = await db.get(Example, example_id)
    if not example:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Example {example_id} not found"
        )
    return ExampleResponse.from_orm(example)
```

### `router.py` - API Endpoints

Define HTTP routes (GET, POST, PUT, DELETE):

```python
# src/api/exampleRoute/router.py

from uuid import UUID
from fastapi import APIRouter, HTTPException, status, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.common.dependencies import DbSession
from src.common.responses import ok_response, error_response
from src.api.exampleRoute.models import ExampleCreate, ExampleUpdate, ExampleResponse
from src.api.exampleRoute.service import ExampleService
from src.api.exampleRoute.dependencies import get_example_or_404

router = APIRouter(prefix="/examples", tags=["Examples"])

# ────────────────────────────────────────────────────────────────
# POST /examples - Create
# ────────────────────────────────────────────────────────────────

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ExampleResponse,
    summary="Create new example",
)
async def create_example(
    data: ExampleCreate,
    db: DbSession,
    request: Request,
    start_time: datetime = Depends(lambda: datetime.now()),
) -> dict:
    """
    Create a new example.
    
    **Request Body:**
    - `name`: Example name (required)
    - `description`: Optional description
    
    **Responses:**
    - 201: Example created successfully
    - 400: Validation error or duplicate name
    """
    try:
        example = await ExampleService.create_example(db, data)
        return ok_response(
            data=example,
            message="Example created successfully",
            status_code=201,
            request=request,
            start_time=start_time,
        )
    except ValueError as e:
        return error_response(
            message=str(e),
            error_code="VALIDATION_ERROR",
            status_code=status.HTTP_400_BAD_REQUEST,
            request=request,
            start_time=start_time,
        )

# ────────────────────────────────────────────────────────────────
# GET /examples - List
# ────────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=list[ExampleResponse],
    summary="List all examples",
)
async def list_examples(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: DbSession = Depends(get_db),
) -> dict:
    """
    List all examples with pagination.
    
    **Query Parameters:**
    - `skip`: Number of records to skip (default: 0)
    - `limit`: Max records to return (default: 100, max: 1000)
    """
    examples = await ExampleService.list_examples(db, skip=skip, limit=limit)
    return ok_response(
        data=examples,
        message=f"Retrieved {len(examples)} examples",
    )

# ────────────────────────────────────────────────────────────────
# GET /examples/{example_id} - Get by ID
# ────────────────────────────────────────────────────────────────

@router.get(
    "/{example_id}",
    response_model=ExampleResponse,
    summary="Get example by ID",
)
async def get_example(
    example: ExampleResponse = Depends(get_example_or_404),
) -> dict:
    """
    Get example by ID.
    
    **Responses:**
    - 200: Example found
    - 404: Example not found
    """
    return ok_response(data=example)

# ────────────────────────────────────────────────────────────────
# PUT /examples/{example_id} - Update
# ────────────────────────────────────────────────────────────────

@router.put(
    "/{example_id}",
    response_model=ExampleResponse,
    summary="Update example",
)
async def update_example(
    example_id: UUID,
    data: ExampleUpdate,
    db: DbSession,
) -> dict:
    """
    Update example by ID.
    
    **Request Body:**
    - `name`: New name (optional)
    - `description`: New description (optional)
    - `is_active`: Update status (optional)
    
    **Responses:**
    - 200: Example updated
    - 404: Example not found
    """
    example = await ExampleService.update_example(db, example_id, data)
    if not example:
        return error_response(
            message="Example not found",
            error_code="NOT_FOUND",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    
    return ok_response(data=example, message="Example updated successfully")

# ────────────────────────────────────────────────────────────────
# DELETE /examples/{example_id} - Delete
# ────────────────────────────────────────────────────────────────

@router.delete(
    "/{example_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete example",
)
async def delete_example(
    example_id: UUID,
    db: DbSession,
) -> None:
    """
    Delete example by ID.
    
    **Responses:**
    - 204: Example deleted successfully
    - 404: Example not found
    """
    success = await ExampleService.delete_example(db, example_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Example not found"
        )
```

## Registering the Router

In `src/main.py`, include the router:

```python
from src.api.exampleRoute.router import router as example_router

app.include_router(example_router)
```

This makes endpoints available at `/examples`.

## Complete Module Checklist

When creating a new API route, ensure you have:

- [ ] **models.py**: ORM model + Pydantic schemas (Create, Update, Response)
- [ ] **curd.py**: CRUD database operations
- [ ] **service.py**: Business logic, validation, error handling
- [ ] **dependencies.py**: Route-level dependencies (get_or_404, etc.)
- [ ] **router.py**: Endpoint definitions with proper status codes & documentation
- [ ] **Register in main.py**: `app.include_router(router)`
- [ ] **Unit tests**: `tests/integration/test_example_endpoints.py`
- [ ] **Integration tests**: Test with database and dependencies

## API Endpoint Patterns

### RESTful Conventions

| Method | Path | Description |
|--------|------|-------------|
| POST | `/resources` | Create resource |
| GET | `/resources` | List resources |
| GET | `/resources/{id}` | Get specific resource |
| PUT | `/resources/{id}` | Update resource |
| DELETE | `/resources/{id}` | Delete resource |
| PATCH | `/resources/{id}` | Partial update |

### Example Full Operations

```python
# Create
POST /api/examples
{
  "name": "Example 1",
  "description": "Description"
}
→ 201 Created
{
  "id": "550e8400-...",
  "name": "Example 1",
  ...
}

# List
GET /api/examples?skip=0&limit=10
→ 200 OK
[
  {"id": "550e8400-...", ...},
  {"id": "550e8401-...", ...}
]

# Get
GET /api/examples/550e8400-...
→ 200 OK
{
  "id": "550e8400-...",
  "name": "Example 1",
  ...
}

# Update
PUT /api/examples/550e8400-...
{
  "name": "Updated Example"
}
→ 200 OK
{
  "id": "550e8400-...",
  "name": "Updated Example",
  ...
}

# Delete
DELETE /api/examples/550e8400-...
→ 204 No Content
```

---

**Related Documentation:**
- See [general-structure.md](general-structure.md) for overall project structure
- See [authentication.md](authentication.md) for secure endpoint implementation
- See [testing.md](testing.md) for endpoint testing patterns
