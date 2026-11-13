# Environment Configuration

## Overview

The application supports **four deployment environments**, each with distinct configurations:

- **dev**: Local development with auto-reload
- **test**: Automated testing environment
- **stage**: Staging/pre-production environment
- **prod**: Production environment

Configuration is managed via `.env` files and environment variables, using **pydantic-settings** for type-safe configuration.

## Configuration Hierarchy

Settings are loaded in order (later overrides earlier):

1. **Defaults** in `src/common/config.py` (Settings class)
2. **Base file**: `.env` (shared across all environments)
3. **Environment file**: `.env.{env}` (environment-specific overrides)
4. **Environment variables**: Actual environment variables (highest priority)

### Example

```ini
# .env (shared across all environments)
CACHE_MAXWAIT_CONNECTIONS=10
LOG_LEVEL=INFO

# .env.prod (overrides for production)
DEBUG=False
LOG_LEVEL=WARNING
ENVIRONMENT=prod
DATABASE_URL=postgresql+asyncpg://prod_user:secure_pass@db.prod.internal:5432/api_prod
OTLP_ENDPOINT=http://grafana-alloy.monitoring:4318
```

## Environment Files

### `.env` (Shared Base Configuration)

Common settings used by all environments:

```ini
# Application
APP_NAME=api
APP_DESCRIPTION=FastAPI Application
APP_VERSION=0.1.0

# Cache - defaults
CACHE_HOST=localhost
CACHE_PORT=6379
CACHE_DB=0
CACHE_MAX_CONNECTIONS=10
CACHE_KEY_PREFIX=fastapi-cache

# JWT
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=30

# Password hashing
BCRYPT_ROUNDS=12

# Server
HOST=0.0.0.0
```

### `.env.dev` (Development)

Local development settings with fast iteration:

```ini
ENVIRONMENT=dev
DEBUG=True
LOG_LEVEL=DEBUG
HOST=127.0.0.1
PORT=8000
RELOAD=True

# Local database
DATABASE_URL=postgresql+asyncpg://dev:dev@localhost:5432/api_dev

# Local Redis
CACHE_HOST=localhost
CACHE_PORT=6379

# Dev Secret (MUST be changed in production!)
JWT_SECRET_KEY=dev-secret-key-not-for-production

# Logging
ACCESS_LOG_ENABLED=True
OTLP_LOGS_ENABLED=False  # Don't export logs locally
```

### `.env.test` (Testing)

Isolated test environment:

```ini
ENVIRONMENT=test
DEBUG=False
LOG_LEVEL=WARNING
PORT=8001
RELOAD=False

# Test database (separate from dev)
DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/api_test

# Test Redis instance
CACHE_HOST=localhost
CACHE_PORT=6379
CACHE_DB=1  # Different DB to isolate test data

# Test JWT secret
JWT_SECRET_KEY=test-secret-key-for-testing

# Disable logging during tests
ACCESS_LOG_ENABLED=False
OTLP_LOGS_ENABLED=False
```

### `.env.stage` (Staging)

Pre-production environment mirroring production:

```ini
ENVIRONMENT=stage
DEBUG=False
LOG_LEVEL=INFO
PORT=8000
RELOAD=False

# Staging database
DATABASE_URL=postgresql+asyncpg://stage_user:stage_password@db.stage.internal:5432/api_stage

# Staging Redis
CACHE_HOST=redis.stage.internal
CACHE_PORT=6379
CACHE_PASSWORD=staging-redis-password

# Production-like JWT secret (rotate regularly)
JWT_SECRET_KEY=${STAGING_JWT_SECRET_KEY}  # Via secrets manager

# Logging
ACCESS_LOG_ENABLED=True
OTLP_LOGS_ENABLED=True
OTLP_ENDPOINT=http://grafana-alloy.staging:4318
OTLP_SERVICE_NAME=api-staging
```

### `.env.prod` (Production)

Production-ready configuration:

```ini
ENVIRONMENT=prod
DEBUG=False
LOG_LEVEL=WARNING
PORT=8000
RELOAD=False

# Production database (high availability)
DATABASE_URL=postgresql+asyncpg://prod_user:${DB_PASSWORD}@db-primary.prod.internal:5432/api_prod

# Connection pool for production load
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=40

# Production Redis (cluster/sentinel)
CACHE_HOST=redis-cluster.prod.internal
CACHE_PORT=6379
CACHE_PASSWORD=${REDIS_PASSWORD}
CACHE_MAX_CONNECTIONS=20

# Production JWT secret (from secrets manager)
JWT_SECRET_KEY=${PROD_JWT_SECRET_KEY}

# Logging & Monitoring
ACCESS_LOG_ENABLED=True
OTLP_LOGS_ENABLED=True
OTLP_ENDPOINT=http://grafana-alloy.prod:4318
OTLP_SERVICE_NAME=api
OTLP_HEADERS=Authorization=Bearer ${OTEL_AUTH_TOKEN}
METRICS_ENABLED=True
```

## Starting the Application

### Development

```bash
# With auto-reload
python app.py dev

# With custom port
python app.py dev --port 8001

# With custom host
python app.py dev --host 0.0.0.0
```

### Testing

```bash
# Run tests in test environment
pytest tests/

# The test environment is automatically activated via pytest.ini
```

### Staging

```bash
# Single instance
python app.py stage --host 0.0.0.0 --port 8000

# Docker Compose
docker-compose -f docker-compose.yml -f docker-compose.stage.yml up
```

### Production

```bash
# Docker
docker build -t api:latest .
docker run -e ENVIRONMENT=prod api:latest python app.py prod

# Kubernetes
kubectl apply -f deployment/k8n/api/prod/
```

## Database-Specific Configuration

Each environment has its own database instance to prevent data mixing:

| Environment | Host | Database | User | Purpose |
|-------------|------|----------|------|---------|
| dev | localhost | api_dev | dev | Local development |
| test | localhost | api_test | test | Unit/integration tests |
| stage | db.stage.internal | api_stage | stage_user | Pre-production testing |
| prod | db-primary.prod.internal | api_prod | prod_user | Live production data |

### Database URLs

```python
# src/common/config.py
DATABASE_URL: str = "postgresql+asyncpg://user:password@host:5432/database"
```

## Secrets Management

### Development

Store secrets in `.env.dev` (add to `.gitignore`):

```ini
JWT_SECRET_KEY=dev-insecure-secret-key
CACHE_PASSWORD=
```

### Production

Use environment secrets manager (AWS Secrets Manager, HashiCorp Vault, etc.):

```bash
# In docker-compose or k8s, inject secrets as environment variables
export PROD_JWT_SECRET_KEY=$(aws secretsmanager get-secret-value --secret-id api/jwt-secret)
export DB_PASSWORD=$(aws secretsmanager get-secret-value --secret-id api/db-password)

python app.py prod
```

### Kubernetes Secrets

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: api-secrets
  namespace: production
type: Opaque
data:
  jwt-secret-key: <base64-encoded-secret>
  db-password: <base64-encoded-password>
  redis-password: <base64-encoded-password>

---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: production
spec:
  template:
    spec:
      containers:
      - name: api
        env:
        - name: PROD_JWT_SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: api-secrets
              key: jwt-secret-key
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef:
              name: api-secrets
              key: db-password
        - name: REDIS_PASSWORD
          valueFrom:
            secretKeyRef:
              name: api-secrets
              key: redis-password
```

## Environment Variables Override

Any setting can be overridden via environment variable:

```bash
# Override specific settings
export ENVIRONMENT=stage
export LOG_LEVEL=DEBUG
export DATABASE_URL=postgresql+asyncpg://...
export JWT_SECRET_KEY=custom-secret

python app.py stage
```

## Configuration Verification

Check current configuration on startup:

```bash
# View active configuration in logs
python app.py dev

# Output in logs:
# 2026-02-27 10:00:00 [INFO] app.startup
#     environment="dev"
#     host="127.0.0.1"
#     port=8000
#     debug=true
#     log_level="DEBUG"
#     database_url="postgresql+asyncpg://dev:***@localhost:5432/api_dev"
```

## Environment-Specific Features

### Development

- ✅ Auto-reload on file changes (`RELOAD=true`)
- ✅ OpenAPI documentation (`/docs`)
- ✅ Debug logging
- ✅ Structured console output (colored)

### Test

- ✅ Isolated database & cache
- ✅ Minimal logging (suppress noise)
- ✅ No OTLP export
- ✅ Deterministic results

### Staging

- ✅ Production-like configuration
- ✅ Real secrets management
- ✅ OTLP logging enabled
- ✅ Metrics collection

### Production

- ✅ No debug mode
- ✅ Security headers enabled
- ✅ OTLP logging & metrics
- ✅ High-performance settings
- ✅ No OpenAPI docs
- ✅ Connection pool tuning

## Database Migrations

For each environment, migrations are applied before startup:

```bash
# Dev
alembic upgrade head

# Test
alembic upgrade head --sql > migrations.sql  # View what would run

# Staging
alembic upgrade head

# Prod
alembic upgrade head  # Usually run as init container in K8s
```

## Configuration Validation

On startup, the application validates configuration:

- Database URL is valid and reachable
- Redis connection pool can be established
- All required secrets are provided
- JWT secret is set (not default)

If validation fails, application exits with error (prevents invalid deployments).

## Switching Environments

### For Developers

```bash
# Switch from dev to test
python app.py test tests/

# Switch from dev to staging (requires credentials)
python app.py stage
```

### For DevOps

```bash
# Docker
docker build -t api:latest .
docker run -e ENVIRONMENT=stage api:latest

# Kubernetes
kubectl set env deployment/api ENVIRONMENT=prod
```

---

**Related Documentation:**
- See [database.md](database.md) for database setup per environment
- See [redis.md](redis.md) for cache configuration
- See [logging.md](logging.md) for environment-specific logging
