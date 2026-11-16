# Testing Strategy

## Overview

The project uses **pytest** for unit and integration testing with the following structure:

- **Unit tests** (`tests/unit/`): Pure logic, no Docker, run in seconds
- **Integration tests** (`tests/integration/`): API endpoints and database operations against Postgres and Redis Testcontainers
- **No backend end-to-end folder**: End-to-end journeys are covered by frontend Playwright ([ADR 0007](adr/0007-test-split-unit-integration.md))
- **Test database**: Isolated PostgreSQL instance
- **Test Redis**: Isolated Redis instance for cache testing
- **Fixtures**: Reusable test setup (users, sessions, etc.)

Testing is configured in `pytest.ini` to automatically use the **test environment**.

## Test Structure

```
tests/
├── conftest.py           # Environment setup and the unit/integration markers only
├── helpers.py            # Shared helper functions
├── unit/                 # No Docker
│   ├── conftest.py       # settings_factory, fresh_settings
│   ├── core/
│   └── modules/auth/
└── integration/          # Needs Docker (Testcontainers)
    ├── conftest.py       # containers, client, db, captured_emails
    ├── core/
    ├── contract/
    ├── modules/auth/
    └── admin/
```

## Running Tests

### All Tests

```bash
uv run pytest                          # unit + integration, coverage gate; needs Docker
uv run pytest tests/unit --no-cov      # unit only, no Docker; --no-cov skips the gate
uv run pytest tests/integration -x     # integration only
```

### Specific Test File

```bash
uv run pytest tests/integration/modules/auth/test_auth.py
```

### Specific Test Function

```bash
uv run pytest tests/integration/modules/auth/test_auth.py::TestRegister
```

### With Coverage Report

```bash
pytest --cov=src --cov-report=html
# View coverage in htmlcov/index.html
```

### With Verbose Output

```bash
pytest -v -s
```

### Watch Mode (run on changes)

```bash
ptw  # Requires pytest-watch
```

## Test Database

The **test environment** uses a separate PostgreSQL database:

```ini
# .env.test
ENVIRONMENT=test
DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/api_test
CACHE_HOST=localhost
CACHE_DB=1
```

### Database Setup for Testing

Before tests run:

```python
# conftest.py
@pytest.fixture(scope="session", autouse=True)
async def setup_test_db():
    """Create test database and run migrations."""
    # 1. Create test database
    # 2. Run alembic upgrade head
    # 3. Run tests
    # 4. Tear down test database
```

## Testing Your First Endpoint

### Simple Health Check Test

```python
# tests/test_api.py
import pytest
import httpx
from fastapi.testclient import TestClient
from src.main import app

@pytest.mark.asyncio
async def test_health_check():
    """Test basic health endpoint."""
    async with TestClient(app) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["success"] is True
```

### Test with Database

```python
# tests/integration/test_user_endpoints.py
import pytest
from src.common.dependencies import DbSession
from src.api.auth.models import User
from src.common.database import AsyncSessionFactory

@pytest.mark.asyncio
async def test_create_user(db: DbSession):
    """Test user creation."""
    user = User(username="testuser", email="test@example.com")
    db.add(user)
    await db.commit()
    
    # Verify in database
    retrieved = await db.get(User, user.id)
    assert retrieved.username == "testuser"
```

### Test with Authentication

```python
# tests/integration/test_auth_endpoints.py
import pytest
from fastapi.testclient import TestClient
from src.main import app

@pytest.mark.asyncio
async def test_login_success():
    """Test successful login."""
    # 1. Register user
    async with TestClient(app) as client:
        register_response = await client.post(
            "/api/auth/register",
            json={
                "username": "john",
                "email": "john@example.com",
                "password": "SecurePass123!",
            }
        )
        assert register_response.status_code == 201
        
        # 2. Login
        login_response = await client.post(
            "/api/auth/login",
            json={
                "username": "john",
                "password": "SecurePass123!",
            }
        )
        assert login_response.status_code == 200
        data = login_response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        
        # 3. Use access token
        headers = {"Authorization": f"Bearer {data['access_token']}"}
        profile_response = await client.get("/api/user", headers=headers)
        assert profile_response.status_code == 200
```

## Fixtures

Reusable test setup defined in `conftest.py`:

### Database Fixture

```python
# tests/conftest.py
@pytest.fixture
async def db():
    """Provide isolated database session."""
    async with AsyncSessionFactory() as session:
        yield session
        await session.rollback()  # Rollback after test
```

### User Fixture

```python
@pytest.fixture
async def test_user(db: DbSession):
    """Create test user."""
    user = User(
        username="testuser",
        email="test@example.com",
        is_active=True,
        is_email_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
```

### Authenticated Client Fixture

```python
@pytest.fixture
async def authenticated_client(test_user):
    """Provide TestClient with authentication."""
    async with TestClient(app) as client:
        # Generate token for test user
        token = create_access_token(test_user.id)
        client.headers["Authorization"] = f"Bearer {token}"
        yield client
```

### Cache Fixture

```python
@pytest.fixture
async def redis_client(app):
    """Provide Redis client for cache testing."""
    yield app.state.cache
    await app.state.cache.flushdb()  # Clear after test
```

## Testing Patterns

### Test Isolation

Each test should be independent:

```python
# Good: Self-contained and idempotent
@pytest.mark.asyncio
async def test_user_creation(db: DbSession):
    user = User(username=f"user_{uuid4()}", email=f"user_{uuid4()}@example.com")
    db.add(user)
    await db.commit()
    assert user.id is not None

# Bad: Depends on test execution order
users_created = 0

@pytest.mark.asyncio
async def test_create_user_1(db: DbSession):
    global users_created
    users_created += 1
    ...

@pytest.mark.asyncio
async def test_verify_users_created(db: DbSession):
    assert users_created == 1  # Fails if tests run in different order
```

### Async Test Setup

```python
@pytest.mark.asyncio
async def test_async_operation(app_with_db):
    """Test async database and cache operations."""
    db = app_with_db.state.db
    redis = app_with_db.state.cache
    
    # All operations are awaited
    user = await db.get(User, user_id)
    await redis.set("key", "value")
```

### Mocking External Services

```python
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_with_mock_email():
    """Test auth without actually sending emails."""
    with patch("src.api.auth.service.send_email") as mock_email:
        mock_email.return_value = AsyncMock()
        
        # Test registration
        result = await register_user(...)
        
        # Verify email was "sent"
        mock_email.assert_called_once()
        call_args = mock_email.call_args
        assert "john@example.com" in call_args
```

### Testing Error Conditions

```python
@pytest.mark.asyncio
async def test_login_invalid_password():
    """Test login with wrong password."""
    async with TestClient(app) as client:
        response = await client.post(
            "/api/auth/login",
            json={
                "username": "john",
                "password": "WrongPassword!",
            }
        )
        assert response.status_code == 401
        data = response.json()
        assert data["error"]["code"] == "INVALID_CREDENTIALS"
```

### Testing Validation

```python
@pytest.mark.asyncio
async def test_register_invalid_email():
    """Test registration with invalid email."""
    async with TestClient(app) as client:
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "john",
                "email": "not-an-email",  # Invalid
                "password": "SecurePass123!",
            }
        )
        assert response.status_code == 422
        # Pydantic validation error
```

## Continuous Integration

### GitHub Actions Example

```yaml
# .github/workflows/test.yml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:15
        env:
          POSTGRES_DB: api_test
          POSTGRES_USER: test
          POSTGRES_PASSWORD: test
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

      redis:
        image: redis:latest
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: "3.10"

      - name: Install dependencies
        run: |
          pip install pytest pytest-asyncio httpx
          pip install -e .

      - name: Run migrations
        run: alembic upgrade head

      - name: Run tests
        run: pytest --cov=src

      - name: Upload coverage
        uses: codecov/codecov-action@v3
```

## Best Practices

1. **Test one thing**: Each test should verify one behavior
2. **Use descriptive names**: Test name should describe what it tests
3. **Setup and teardown**: Use fixtures for consistent state
4. **Avoid magic numbers**: Use named constants
5. **Test happy path and errors**: Test success cases and error conditions
6. **Keep tests fast**: Mock slow operations (email, external APIs)
7. **Isolate from external services**: Use test database/cache
8. **Use parametrize for multiple cases**: Reduce duplicate test code

### Example: Parametrized Tests

```python
@pytest.mark.parametrize("password,should_pass", [
    ("ValidPassword123!", True),
    ("short", False),
    ("NOLOWERCASE123!", False),
    ("nouppercase123!", False),
    ("NoNumbers!", False),
])
@pytest.mark.asyncio
async def test_password_validation(password, should_pass):
    """Test password requirements."""
    from src.api.auth.service import validate_password
    
    if should_pass:
        assert validate_password(password) is True
    else:
        with pytest.raises(ValidationError):
            validate_password(password)
```

## Debugging Failed Tests

### Print Debug Info

```python
@pytest.mark.asyncio
async def test_something(db: DbSession):
    user = await db.get(User, user_id)
    print(f"User: {user}")  # Will appear with -s flag
    print(f"User dict: {user.__dict__}")
```

### Inspect with pdb

```python
@pytest.mark.asyncio
async def test_something(db: DbSession):
    user = await db.get(User, user_id)
    breakpoint()  # Drops into debugger
    assert user is not None
```

Run with:
```bash
pytest -s tests/test_something.py::test_something
```

### Log Capture

```python
@pytest.mark.asyncio
async def test_something(caplog):
    """Capture logs during test."""
    with caplog.at_level(logging.INFO):
        await some_operation()
    
    assert "expected_log_message" in caplog.text
```

---

**Related Documentation:**
- See [environment.md](environment.md) for test environment configuration
- See [api_structure.md](api_structure.md) for code organization
