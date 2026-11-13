# Development Setup Guide

## Prerequisites

- **Python 3.10+** (recommended: 3.11 or 3.12)
- **PostgreSQL 12+** (local installation or Docker)
- **Redis** (local installation or Docker)
- **Git**
- **Docker & Docker Compose** (recommended for database/cache)

## Quick Start (5 minutes)

### 1. Clone Repository

```bash
git clone https://github.com/your-org/api.git
cd api
```

### 2. Setup Python Environment

**Using venv** (recommended):
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

**Using uv** (faster):
```bash
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -e .
pip install -e ".[dev]"  # Install dev dependencies too

# Verify installation
python -c "import fastapi; print('FastAPI installed!')"
```

### 4. Start PostgreSQL and Redis

**Using Docker Compose** (recommended):
```bash
docker-compose up -d

# Verify services are running
docker-compose ps
```

**Using local installations**:
```bash
# PostgreSQL
createdb api_dev

# Redis
redis-server
```

### 5. Setup Database

```bash
# Run migrations
alembic upgrade head

# Verify database
psql api_dev -c "SELECT * FROM users;"
```

### 6. Run Application

```bash
python app.py dev

# Output:
# 2026-02-27 10:00:00 [INFO] app.startup
# ...
# Application startup complete [uvicorn] (Press CTRL+C to quit)
```

### 7. Verify It Works

```bash
# In another terminal
curl http://localhost:8000/docs
# Should open FastAPI Swagger UI
```

## Detailed Setup

### Creating Python Virtual Environment

**venv** (built-in):
```bash
# Create
python -m venv venv

# Activate
source venv/bin/activate  # macOS/Linux
# OR
venv\Scripts\activate  # Windows

# Deactivate
deactivate
```

**Poetry** (if using poetry):
```bash
poetry install
poetry shell
```

**Conda** (if using conda):
```bash
conda create -n api python=3.11
conda activate api
```

### Installing Python Dependencies

The project uses `pyproject.toml` for dependency management:

```bash
# Install production dependencies
pip install -e .

# Install development dependencies
pip install -e ".[dev]"

# Verify installation
pip list
```

### Setting Up PostgreSQL

**Docker** (recommended):
```bash
docker run --name api-postgres \
  -e POSTGRES_USER=dev \
  -e POSTGRES_PASSWORD=dev \
  -e POSTGRES_DB=api_dev \
  -p 5432:5432 \
  -d postgres:15
```

**Local Installation**:

macOS:
```bash
brew install postgresql
brew services start postgresql
createdb api_dev
```

Ubuntu/Debian:
```bash
bash install postgresql
sudo -u postgres createdb api_dev
```

Windows:
```bash
# Download from https://www.postgresql.org/download/windows/
# Run installer, create database via pgAdmin
```

**Connection String**:
```
postgresql+asyncpg://dev:dev@localhost:5432/api_dev
```

### Setting Up Redis

**Docker** (recommended):
```bash
docker run --name api-redis \
  -p 6379:6379 \
  -d redis:latest
```

**Local Installation**:

macOS:
```bash
brew install redis
brew services start redis
```

Ubuntu/Debian:
```bash
sudo apt-get install redis-server
sudo systemctl start redis-server
```

Windows:
```bash
# Use Windows Subsystem for Linux (WSL)
# Or download from https://github.com/microsoftarchive/redis/releases
```

**Verify Connection**:
```bash
redis-cli ping
# Output: PONG
```

### Environment Configuration

Create `.env.dev` for development settings:

```ini
# .env.dev
ENVIRONMENT=dev
DEBUG=True
LOG_LEVEL=DEBUG

# Database
DATABASE_URL=postgresql+asyncpg://dev:dev@localhost:5432/api_dev

# Cache
CACHE_HOST=localhost
CACHE_PORT=6379

# JWT
JWT_SECRET_KEY=dev-secret-change-in-production

# Server
HOST=127.0.0.1
PORT=8000
RELOAD=True
```

### Database Migrations

Initialize database schema:

```bash
# View current migration status
alembic current

# Apply all pending migrations
alembic upgrade head

# View migration history
alembic history

# Create new migration
alembic revision --autogenerate -m "Add user_roles table"
```

## IDE Setup

### VS Code

Install extensions:
- **Python**: ms-python.python
- **Pylance**: ms-python.vscode-pylance
- **FastAPI**: ms-python.fastapi
- **REST Client**: humao.rest-client

Create `.vscode/settings.json`:
```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/venv/bin/python",
  "python.linting.enabled": true,
  "python.linting.pylintEnabled": false,
  "[python]": {
    "editor.defaultFormatter": "ms-python.python",
    "editor.formatOnSave": true,
    "editor.codeActionsOnSave": {
      "source.fixAll": true
    }
  }
}
```

### PyCharm

1. Open project
2. Set Python interpreter: Settings → Project → Python Interpreter
3. Select virtualenv or create new
4. Mark `src` as Sources Root

## Development Workflow

### Starting Development Server

```bash
# With auto-reload
python app.py dev

# With custom port
python app.py dev --port 8001

# With custom host (allows remote connections)
python app.py dev --host 0.0.0.0
```

### Running Tests

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_api.py

# Run with coverage
pytest --cov=src --cov-report=html

# Run with verbose output
pytest -v -s

# Run in watch mode
ptw
```

### Code Quality

```bash
# Type checking
mypy src/

# Linting
ruff check src/

# Format code
ruff format src/

# All checks together
python -m pre_commit run --all-files
```

### Database Operations

```bash
# Connect to dev database
psql api_dev

# View tables
\dt

# Run SQL query
SELECT * FROM users;

# Backup database
pg_dump api_dev > backup.sql

# Restore database
psql api_dev < backup.sql
```

### Viewing API Documentation

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **OpenAPI JSON**: http://localhost:8000/openapi.json

### Debugging

#### Print Debugging

```python
import logging
from src.common.logging import get_logger

logger = get_logger(__name__)

logger.debug("user_id", user_id=user_id, email=user.email)
```

#### Breakpoint Debugging

```python
def my_function():
    breakpoint()  # Will open pdb debugger
    x = 5
```

Run with:
```bash
python app.py dev  # Breakpoint will pause execution in terminal
```

#### VSCode Debugging

Create `.vscode/launch.json`:
```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "FastAPI",
      "type": "python",
      "request": "launch",
      "module": "uvicorn",
      "args": ["src.main:app", "--reload"],
      "jinja": true
    }
  ]
}
```

Press F5 to start debugging.

## Troubleshooting

### "ModuleNotFoundError: No module named 'src'"

Ensure virtual environment is activated and package is installed:

```bash
source venv/bin/activate
pip install -e .
```

### "Could not connect to server: psql: error: connection refused"

Check PostgreSQL is running:

```bash
# If using Docker
docker-compose ps

# If local installation
# macOS
brew services list
# Linux
sudo systemctl status postgresql
```

### "ConnectionRefusedError: [Errno 111] Connection refused" (Redis)

Check Redis is running:

```bash
# If using Docker
docker-compose ps

# If local installation
redis-cli ping  # Should return PONG

# Start Redis
redis-server
```

### "alembic.util.exc.CommandError: Don't know how to initialize database"

Ensure database exists and migrations have been applied:

```bash
# Create database if missing
createdb api_dev

# Apply migrations
alembic upgrade head
```

### Import errors in IDE

Ensure Python interpreter points to virtual environment:

```bash
# Check which Python is active
which python

# Should show something like:
# /path/to/project/venv/bin/python
```

In VS Code: Run "Python: Select Interpreter" and choose the venv version.

## Common Development Tasks

### Creating a New API Endpoint

1. **Define models** in `src/api/myroute/models.py`
2. **Create CRUD** in `src/api/myroute/curd.py`
3. **Implement service** in `src/api/myroute/service.py`
4. **Define routes** in `src/api/myroute/router.py`
5. **Register router** in `src/main.py`

### Adding Database Migration

```bash
# After modifying models
alembic revision --autogenerate -m "Description of change"

# Review generated migration
cat alembic/versions/[recent_migration].py

# Apply migration
alembic upgrade head
```

### Writing Tests

Create test file in `tests/integration/test_my_feature.py`:

```python
import pytest
from fastapi.testclient import TestClient
from src.main import app

@pytest.mark.asyncio
async def test_my_feature():
    async with TestClient(app) as client:
        response = await client.get("/api/my-endpoint")
        assert response.status_code == 200
```

Run test:
```bash
pytest tests/integration/test_my_feature.py -v
```

### Debugging Database Queries

Enable SQL echo in `src/common/database.py`:

```python
return create_async_engine(
    url,
    echo=True,  # Print all SQL statements
    ...
)
```

## Performance Testing

### Load Testing with Locust

Install:
```bash
pip install locust
```

Create `locustfile.py`:
```python
from locust import HttpUser, task

class APIUser(HttpUser):
    @task
    def get_users(self):
        self.client.get("/api/users")
```

Run:
```bash
locust -f locustfile.py --host=http://localhost:8000
# Open http://localhost:8089
```

### Profiling with cProfile

```python
import cProfile

cProfile.run('my_function()', sort='cumulative')
```

## Pre-Commit Hooks

Set up automatic checks before commits:

```bash
# Install pre-commit
pip install pre-commit

# Create .pre-commit-config.yaml
# (see repo for template)

# Install hooks
pre-commit install

# Run manually
pre-commit run --all-files
```

## Contributing Guidelines

1. Create feature branch: `git checkout -b feature/my-feature`
2. Make changes and test locally
3. Run all checks:
   ```bash
   mypy src/
   ruff check src/
   pytest --cov=src
   ```
4. Commit with clear message: `git commit -m "feat: add user roles"`
5. Push and create pull request
6. Wait for CI/CD pipeline to pass
7. Request code review

## Useful Commands Cheat Sheet

```bash
# Activate virtual environment
source venv/bin/activate  # macOS/Linux
venv\Scripts\activate  # Windows

# Install dependencies
pip install -e .
pip install -e ".[dev]"

# Start development server
python app.py dev

# Run tests
pytest
pytest -v -s
pytest tests/integration/

# Database migrations
alembic current
alembic upgrade head
alembic revision --autogenerate -m "message"

# Code quality
mypy src/
ruff check src/
ruff format src/

# Type checking
mypy src/

# View documentation
open http://localhost:8000/docs

# Connect to dev database
psql api_dev

# View Redis
redis-cli

# Stop services
docker-compose down
```

---

**Related Documentation:**
- See [environment.md](environment.md) for environment configuration
- See [testing.md](testing.md) for testing guide
- See [general-structure.md](general-structure.md) for code organization
