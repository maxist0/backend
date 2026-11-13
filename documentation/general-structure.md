# General Project Structure

## Overview

This is a FastAPI-based REST API application built with modern Python async patterns, structured for scalability, maintainability, and production readiness. The project is currently in **early alpha** and implements authentication, user management, caching, structured logging, and comprehensive monitoring.

## Project Root Structure

```
fastapi/
├── src/                      # Main application source code
├── alembic/                  # Database migrations
├── deployment/               # Kubernetes and Docker deployment configs
├── documentation/            # This documentation
├── scripts/                  # Helper scripts for deployment
├── tests/                    # Test suite
├── app.py                    # Application entry point
├── docker-compose.yml        # Local development environment
├── Dockerfile               # Container image definition
├── pyproject.toml           # Python project dependencies
├── alembic.ini              # Alembic migration config
├── .env*                    # Environment configuration files
└── README.md                # Project readme
```

## Core Application Structure (`src/`)

### Top-Level Module: `src/main.py`

The FastAPI application instance is created and configured here. This module:

- **Initializes middleware**: Access logging, metrics, security headers, CORS, compression, metadata handling
- **Manages application lifecycle**: Startup (DB init, cache init, metrics setup) and shutdown (cleanup)
- **Configures exception handlers**: Custom validation error responses using unified error envelope
- **Sets up OpenAPI documentation**: Disabled in production, enabled in dev/test/staging

**Key Components:**
- `lifespan()` context manager: Handles startup/shutdown lifecycle events
- `app`: FastAPI application instance with metadata (title, version, description)

### `src/api/` - API Routes and Modules

Organized by feature domain. Each module follows a consistent structure:

```
api/
├── auth/                    # Authentication & authorization
│   ├── models.py           # SQLAlchemy ORM + Pydantic schemas
│   ├── router.py           # Endpoint definitions
│   ├── service.py          # Business logic
│   ├── crud.py             # Database operations
│   ├── dependencies.py      # Route dependencies (JWT validation, etc.)
│   └── jwt_utils.py        # JWT token utilities
├── user/                    # User management
│   ├── models.py           # Pydantic schemas (uses auth models for ORM)
│   ├── router.py           # User endpoints
│   ├── service.py          # User business logic
│   ├── curd.py             # User database operations
│   └── dependencies.py      # Route dependencies
├── exampleRoute/            # Example module structure
│   ├── models.py
│   ├── router.py
│   ├── service.py
│   ├── curd.py
│   └── dependencies.py
├── metrics/                 # Prometheus metrics endpoints
│   ├── router.py           # /metrics endpoint
│   └── __init__.py
└── __init__.py
```

**Module Pattern:**
- **models.py**: SQLAlchemy ORM models + Pydantic request/response schemas
- **router.py**: FastAPI routes (endpoints)
- **service.py**: Business logic, orchestration
- **curd.py**: Database CRUD operations (misspelled intentionally - stands for Create, Read, Update, Delete)
- **dependencies.py**: FastAPI dependency injection (auth checks, DB session, etc.)

### `src/common/` - Shared Infrastructure

Cross-cutting concerns used by all API modules:

```
common/
├── config.py               # Settings management (pydantic-settings)
├── database.py             # SQLAlchemy async engine, session factory
├── cache.py                # Redis connection pool, fastapi-cache2
├── logging.py              # Structured logging with structlog + OTLP
├── middleware.py           # FastAPI middleware (access log, metrics, etc.)
├── metrics.py              # Prometheus metrics initialization
├── dependencies.py         # Global dependencies (DbSession, cache, etc.)
├── responses.py            # Standardized response envelopes
├── models.py               # Shared Pydantic models (error codes, etc.)
├── constants.py            # App-wide constants
└── __init__.py
```

**Key Modules:**

- **config.py**: Manages configuration for dev, test, stage, prod environments using pydantic-settings. Environment variables are loaded from `.env` and `.env.{env}` files.
  
- **database.py**: Provides SQLAlchemy 2.0 async engine with asyncpg driver, connection pooling, and session factory.

- **cache.py**: Redis client setup with connection pooling and fastapi-cache2 integration.

- **logging.py**: Structured logging via structlog with optional OTLP export to Grafana Alloy.

- **middleware.py**: Custom middleware for access logging, request metrics, security headers, CORS.

- **responses.py**: Standardized JSON response envelope for consistency across all endpoints.

## Entry Points

### `app.py` - Application Launcher

CLI-based launcher that accepts environment argument:

```bash
python app.py dev       # Development (hot reload, verbose logging)
python app.py test      # Test environment
python app.py stage     # Staging
python app.py prod      # Production
```

Options:
- `--host`: Override server host
- `--port`: Override server port
- `--reload`: Enable auto-reload

### Docker & Docker Compose

- **Dockerfile**: Multi-stage build for production image
- **docker-compose.yml**: Local development environment with API + Redis

## Request/Response Pattern

All endpoints follow a standardized response envelope:

**Success Response (2xx):**
```json
{
  "success": true,
  "data": { ... },
  "message": "Operation successful",
  "timestamp": "2026-02-27T10:00:00Z",
  "request_id": "uuid",
  "path": "/api/users",
  "method": "GET",
  "status_code": 200
}
```

**Error Response (4xx/5xx):**
```json
{
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Email is required",
    "field": "email",
    "details": { ... }
  },
  "timestamp": "2026-02-27T10:00:00Z",
  "request_id": "uuid",
  "path": "/api/users",
  "method": "POST",
  "status_code": 422
}
```

## Dependency Injection Pattern

FastAPI's dependency injection system is used extensively:

```python
# In dependencies.py
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionFactory() as session:
        yield session

# In router.py
from src.common.dependencies import DbSession

@router.get("/users")
async def list_users(db: DbSession) -> ...:
    # db is automatically injected
    pass
```

## Database Migrations

Alembic is used for database schema management:

```
alembic/
├── versions/
│   └── 20260226_2120_37a0d49f8bbc_add_auth_user_tables.py
├── env.py          # Alembic environment config
├── script.py.mako  # Migration template
└── __init__.py
```

Create new migrations:
```bash
alembic revision --autogenerate -m "Description"
```

Apply migrations:
```bash
alembic upgrade head
```

## Deployment Targets

- **Local**: Docker Compose (see docker-compose.yml)
- **Staging/Prod**: Kubernetes manifests in `deployment/k8n/` with Helm values
- **Monitoring**: Prometheus, Grafana, Loki, Alloy in `deployment/docker/monitoring/`

---

**Next Steps:**
- See [logging.md](logging.md) for structured logging architecture
- See [database.md](database.md) for database setup details
- See [authentication.md](authentication.md) for auth implementation
- See [environment.md](environment.md) for environment configuration
