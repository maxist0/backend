# Staging Environment

## Overview

The **staging environment** is a production-like environment for testing deployments, configurations, and features before releasing to production. It mirrors production infrastructure and uses real secrets management.

## Staging Environment Goals

- **Pre-production testing**: Verify features work end-to-end
- **Configuration validation**: Test production-like settings
- **Performance testing**: Load testing before production
- **Deployment rehearsal**: Practice deployment procedures
- **Team testing**: QA team tests new features
- **Secrets management**: Practice real secrets handling
- **Database verification**: Test migrations and data handling

## Staging Infrastructure

### Architecture

```
Staging Load Balancer
    ↓
Staging API Pod (auto-scaled)
├── Reads/writes to Staging PostgreSQL
├── Reads/writes to Staging Redis
└── Exports logs to Staging Loki

Staging PostgreSQL (replica of prod)
├── Separate instance from production
├── Regular backups
└── Test disaster recovery

Staging Redis (separate cluster)
├── Staging cache data
└── Test cache invalidation

Staging Monitoring (isolated)
├── Prometheus
├── Grafana
├── Loki
└── Alloy
```

### Comparing to Production

| Aspect | Staging | Production |
|--------|---------|-----------|
| **Data** | Test/sample data | Real user data |
| **Database** | Separate instance | HA cluster |
| **Cache** | Single Redis node | Multi-node cluster |
| **Compute** | Limited resources | Full resources |
| **Traffic** | Test traffic | Real traffic |
| **Secrets** | Real secrets (test account) | Real production secrets |
| **Monitoring** | Isolated dashboards | Centralized monitoring |
| **Backups** | Daily backups | Hourly + replication |

## Configuration

### Environment Variables (`.env.stage`)

```ini
ENVIRONMENT=stage
DEBUG=False
LOG_LEVEL=INFO
PORT=8000
RELOAD=False

# Staging Database (separate from dev/test)
DATABASE_URL=postgresql+asyncpg://stage_user:${STAGING_DB_PASSWORD}@db.staging.internal:5432/api_staging

# Connection pool
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=40

# Staging Redis
CACHE_HOST=redis.staging.internal
CACHE_PORT=6379
CACHE_PASSWORD=${STAGING_REDIS_PASSWORD}
CACHE_MAX_CONNECTIONS=20

# JWT
JWT_SECRET_KEY=${STAGING_JWT_SECRET_KEY}

# Logging & Monitoring
ACCESS_LOG_ENABLED=True
OTLP_LOGS_ENABLED=True
OTLP_ENDPOINT=http://grafana-alloy.staging:4318
OTLP_SERVICE_NAME=api-staging
METRICS_ENABLED=True
```

### Secrets Management

Staging uses real secrets (from a test account), stored securely:

```bash
# Use environment secret manager (AWS Secrets Manager, etc.)
export STAGING_DB_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id staging/db-password \
  --query SecretString \
  --output text)

export STAGING_JWT_SECRET_KEY=$(aws secretsmanager get-secret-value \
  --secret-id staging/jwt-secret \
  --query SecretString \
  --output text)

# Or in Kubernetes secrets
kubectl get secret api-staging-secrets -o yaml
```

## Deployment

### Docker Compose Staging

```bash
# Deploy to staging using docker-compose
docker-compose -f docker-compose.yml -f docker-compose.staging.yml up -d

# Verify deployment
docker-compose ps

# View logs
docker-compose logs -f api
```

### Kubernetes Staging

```bash
# Apply Staging manifests
kubectl apply -f deployment/k8n/staging/

# Watch rollout
kubectl rollout status deployment/api -n staging

# Check pods
kubectl get pods -n staging

# View logs
kubectl logs -f deployment/api -n staging
```

**Staging Manifests** (`deployment/k8n/staging/`):
- `api-deployment.yaml`: Deployment configuration
- `api-service.yaml`: Service/LoadBalancer
- `database.yaml`: PostgreSQL StatefulSet (if not external)
- `redis-primary.yaml`: Redis primary node
- `redis-replica.yaml`: Redis replica for HA testing

### Kubernetes ConfigMap & Secrets

```yaml
# staging-config.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: api-config
  namespace: staging
data:
  ENVIRONMENT: "stage"
  DEBUG: "false"
  LOG_LEVEL: "INFO"
  OTLP_ENDPOINT: "http://grafana-alloy.staging:4318"

---
# staging-secrets.yaml
apiVersion: v1
kind: Secret
metadata:
  name: api-secrets
  namespace: staging
type: Opaque
data:
  DATABASE_URL: <base64-encoded-url>
  JWT_SECRET_KEY: <base64-encoded-secret>
  REDIS_PASSWORD: <base64-encoded-password>
```

#### Apply to cluster:

```bash
kubectl apply -f deployment/k8n/staging/staging-config.yaml
kubectl apply -f deployment/k8n/staging/staging-secrets.yaml
kubectl apply -f deployment/k8n/staging/api-deployment.yaml
```

## Database Management

### Initial Setup

```bash
# Create staging database
createdb api_staging

# Run migrations
alembic upgrade head

# Seed test data (optional)
python scripts/seed_staging_data.py
```

### Data Refresh from Production

Periodically copy production data to staging (anonymized) for realistic testing:

```bash
#!/bin/bash
# scripts/refresh_staging_db.sh

# Backup production
pg_dump postgresql://prod_user:pass@db-primary.prod:5432/api_prod > prod_backup.sql

# Restore to staging
psql postgresql://stage_user:pass@db.staging:5432/api_staging < prod_backup.sql

# Anonymize sensitive data
python scripts/anonymize_staging_data.py
```

### Backup & Recovery Testing

Test disaster recovery procedures regularly:

```bash
# Backup staging database
pg_dump -Fc postgresql://stage_user:pass@db.staging:5432/api_staging > staging_backup.dump

# Restore from backup (on test instance)
pg_restore --dbname=api_staging_test staging_backup.dump

# Verify data integrity
psql api_staging_test -c "SELECT COUNT(*) FROM users;"
```

## Testing in Staging

### Team Testing Checklist

- [ ] Register new user account
- [ ] Verify email verification works
- [ ] Login with credentials
- [ ] Refresh token works
- [ ] Use API endpoints
- [ ] Logout properly
- [ ] Password reset flow
- [ ] Check audit logs

### Automated Testing

```bash
# Run full smoke test suite against staging
pytest tests/e2e/ \
  --base-url="https://staging-api.example.com" \
  --environment="staging"
```

### Load Testing Script

```bash
# Test staging performance
locust -f scripts/locustfile.py \
  --host=https://staging-api.example.com \
  --users=100 \
  --spawn-rate=10 \
  --run-time=5m
```

## Monitoring Staging

### Staging-Specific Dashboards

Create Grafana dashboards for:
- Request latency
- Error rates
- Database performance
- Cache hit rates
- User/session metrics

### Alert Rules for Staging

```yaml
# More lenient than production (expecting test traffic)
groups:
- name: staging_alerts
  rules:
  
  # Only alert on critical errors (not test failures)
  - alert: StagingHighErrorRate
    expr: |
      rate(http_requests_total{status=~"5.."}[5m]) > 0.1
    for: 10m
    labels:
      severity: warning
```

### Log Queries

```logql
# Errors in staging
{environment="stage"} | json | level="error"

# User actions for testing
{environment="stage"} | json | event =~ "auth.*" | user_id=""

# Performance metrics
{environment="stage"} | json | operation="SELECT" | line_format "{{ .duration_ms }}"
```

## Release Promotion

### Staging → Production Workflow

1. **Deploy to Staging**
   ```bash
   docker build -t api:v0.2.0 .
   docker push registry/api:v0.2.0
   kubectl set image deployment/api -n staging api=registry/api:v0.2.0
   ```

2. **Run Smoke Tests**
   ```bash
   pytest tests/e2e/ --base-url=https://staging-api.example.com
   ```

3. **QA Sign-Off**
   - Team tests features
   - Verifies no blockers
   - Approves for production

4. **Deploy to Production**
   ```bash
   kubectl set image deployment/api -n production api=registry/api:v0.2.0
   ```

### Rollback Plan

If production issues detected:

```bash
# Rollback to previous version
kubectl rollout undo deployment/api -n production

# Verify rollback
kubectl rollout status deployment/api -n production
```

## Staging Issues & Troubleshooting

### Database Connection Issues

```bash
# Test database connectivity
psql postgresql://stage_user:pass@db.staging:5432/api_staging

# Check if migrations are applied
docker-compose exec api alembic current

# Apply migrations if needed
docker-compose exec api alembic upgrade head
```

### Redis/Cache Issues

```bash
# Test Redis connectivity
redis-cli -h redis.staging.internal ping

# Clear cache if needed
redis-cli -h redis.staging.internal FLUSHDB

# Monitor Redis memory
redis-cli -h redis.staging.internal INFO memory
```

### Pod Crash Issues

```bash
# Check pod status
kubectl get pods -n staging

# View logs
kubectl logs -p deployment/api -n staging  # Previous logs if crashed

# Describe pod for events
kubectl describe pod <pod-name> -n staging

# Check resource constraints
kubectl top pod <pod-name> -n staging
```

## Staging Best Practices

1. **Keep it production-like**: Don't skip steps staging takes
2. **Test procedures**: Use staging to test deployment, rollback, recovery
3. **Real secrets**: Use real (test account) secrets to catch secret-related bugs
4. **Load test**: Before major releases, run load testing
5. **Regular refresh**: Periodically copy real data (anonymized)
6. **Monitor actively**: Watch staging metrics while team tests
7. **Document results**: Document what was tested, results
8. **Plan for failure**: Test disaster recovery procedures

## Common Staging Tasks

### Deploy Latest Code

```bash
# Build and push new image
docker build -t api:latest .
docker tag api:latest registry/api:latest
docker push registry/api:latest

# Update staging deployment
kubectl set image deployment/api \
  -n staging \
  api=registry/api:latest

# Watch rollout
kubectl rollout status deployment/api -n staging
```

### View Real-Time Logs

```bash
# Last 100 lines
kubectl logs -n staging deployment/api --tail=100

# Stream logs
kubectl logs -f -n staging deployment/api

# Filter by pod
kubectl logs -n staging pod/api-xyz123
```

### Manual Database Query

```bash
# Connect to staging database
psql api_staging

# Check user count
SELECT COUNT(*) FROM users;

# Find specific user
SELECT * FROM users WHERE email = 'test@example.com';
```

### View Metrics

Access Grafana at `https://grafana.staging.example.com`:
1. Login with staging credentials
2. View "Staging Overview" dashboard
3. Check request latency, error rates
4. Review database performance

---

**Related Documentation:**
- See [environment.md](environment.md) for full environment configuration
- See [monitoring.md](monitoring.md) for monitoring setup
- See [testing.md](testing.md) for testing procedures
- See [deployment](../deployment/) for Kubernetes manifests
