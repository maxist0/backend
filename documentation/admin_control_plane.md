# Implementation Specification: Modular Backend Admin Control Plane

## 1. Context & Architecture Overview

Build a modular, extensible **Admin Control Plane** for the asynchronous **FastAPI** backend (`Python 3.14`, `SQLAlchemy 2 async`, `PostgreSQL 18`, `Redis 7`, `Taskiq`, `sqladmin`) paired with a React frontend. The backend runs on **Docker Swarm** (with a migration path to **Proxmox + Kubernetes**) behind **Traefik** and is monitored via **Prometheus / cAdvisor / Grafana / Loki / Tempo**.

### Core Architecture Alignment (`AGENTS.md`)
- **Strict Module Encapsulation:** Following the backend's core-vs-module pattern (`core ← users ← auth ← app modules`), admin features are contained in a dedicated module `src/app/modules/admin/`.
- **Declarative, No Auto-Discovery:** In compliance with `backend/AGENTS.md`, module registration is declarative via `MODULES = (users.module, auth.module, admin.module, ...)` in `src/app/modules/__init__.py`. Runtime filesystem scanning (`pkgutil` / `importlib`) is intentionally avoided.
- **Two-Factor / Defense in Depth Security:**
  - `sqladmin` (`/admin`) retains its established, independent session authentication (`AdminAuth` using `ADMIN_USERNAME` / `ADMIN_PASSWORD` from environment settings).
  - The React Control Plane API (`/api/v1/admin/*`) is guarded by a bearer token `require_admin` dependency checking active user accounts and admin privilege flags.
- **Generic Boilerplate Boundary:** The backend provides purely generic infrastructure primitives (topology aggregation, Taskiq job tracking, session & audit inspection, dynamic module metadata). No business-domain features are hardcoded.

---

## 2. Component Specifications

### 2.1 Declarative Module Metadata (`src/app/core/module.py`)

Feature modules declare optional admin metadata for the frontend control plane.

```python
@dataclass(frozen=True, slots=True)
class ModuleAdminMeta:
    id: str                 # Unique ID, e.g. "users"
    label_key: str          # i18n translation key, e.g. "admin.nav.users"
    route: str              # Frontend route, e.g. "/admin/users"
    icon: str               # Lucide icon identifier, e.g. "Users"
    group_key: str          # i18n grouping key, e.g. "admin.groups.management"
    order: int = 100        # Display order in sidebar navigation
    widgets: tuple[str, ...] = ()  # Optional dashboard widget identifiers

@dataclass(frozen=True, slots=True)
class Module:
    name: str
    router: APIRouter
    models: ModuleType | None = None
    tasks: ModuleType | None = None
    admin_views: tuple[type[ModelView], ...] = ()
    admin_meta: ModuleAdminMeta | None = None
```

Existing modules (`users`, `auth`) provide `admin_meta` in their `__init__.py`.

---

### 2.2 Admin Security Dependency (`src/app/modules/admin/dependencies.py`)

Implements `require_admin` dependency for all `/api/v1/admin/*` endpoints:
1. Validates JWT access token via existing `get_current_user` (`CurrentUser`).
2. Confirms `user.is_active is True` and `user.is_admin_locked() is False`.
3. Verifies admin privilege via extensible hook `is_admin_user(user: User) -> bool` (placeholder initially verifying active, non-locked status; extensible when full RBAC/database columns are added).
4. Returns RFC 9457 `403 Forbidden` if unauthorized.

---

### 2.3 Bootstrap Config Endpoint (`GET /api/v1/admin/config`)

**Path:** `/api/v1/admin/config`  
**Security:** `Depends(require_admin)`  
**Cache:** `Cache-Control: private, no-cache`

**Response Payload Contract:**
```json
{
  "environment": "dev",
  "app_version": "0.1.0",
  "sqladmin_url": "/admin",
  "modules": [
    {
      "id": "users",
      "label_key": "admin.nav.users",
      "route": "/admin/users",
      "icon": "Users",
      "group_key": "admin.groups.management",
      "order": 10,
      "widgets": ["user_growth_chart", "recent_registrations"]
    },
    {
      "id": "auth",
      "label_key": "admin.nav.auth",
      "route": "/admin/auth",
      "icon": "ShieldCheck",
      "group_key": "admin.groups.security",
      "order": 20,
      "widgets": ["login_activity_map", "failed_logins_kpi"]
    }
  ],
  "observability_links": {
    "grafana": "http://localhost:3000",
    "prometheus": "http://localhost:9090",
    "tempo": "http://localhost:3200",
    "loki": "http://localhost:3100"
  }
}
```

---

### 2.4 Taskiq Live-Job Tracking Middleware (`src/app/core/tasks.py`)

A custom Taskiq middleware registered in both the worker (`src/app/worker.py`) and FastAPI lifespan.

- **Redis Keys:**
  - `admin:taskiq:running` (Hash: `task_id` -> JSON payload with 3600s safety TTL)
  - `admin:taskiq:recent` (List: capped at 50 items via `LPUSH` + `LTRIM 0 49`)
- **Lifecycle Events:**
  - `pre_execute`: Records `task_id`, `task_name`, `worker_id`, `started_at` in `admin:taskiq:running`.
  - `post_execute`: Removes task from `running` and pushes `{ task_id, task_name, worker_id, started_at, finished_at, duration_ms, status: "success" }` to `admin:taskiq:recent`.
  - `on_error`: Removes task from `running` and pushes summary with `status: "error"` and sanitized `error_type` (e.g. `TransientTaskError`, omitting personal/sensitive data).
- **Privacy & Safety:** Strictly no task arguments (`args` / `kwargs`) are stored to prevent leaking PII or credentials.

---

### 2.5 System Telemetry & Topology Aggregator (`GET /api/v1/admin/system/topology`)

**Path:** `/api/v1/admin/system/topology`  
**Security:** `Depends(require_admin)`  
**Cache:** 5 seconds in Redis (`admin:telemetry:topology`) and HTTP header `Cache-Control: private, max-age=5`.

Queries three sources concurrently via `asyncio.gather`:

#### 1. PostgreSQL (Direct DB Queries)
- **Active Sessions:**
  `SELECT count(*) FROM sessions WHERE is_revoked = false AND expires_at > now()`
- **Locked Accounts:**
  `SELECT count(*) FROM users WHERE is_locked = true`
- **Failed Logins (24h):**
  `SELECT count(*) FROM auth_audit_log WHERE event_type = 'login_failed' AND created_at > now() - interval '24 hours'`
- **Replication Role (Smart Detection):**
  `SELECT pg_is_in_recovery()`:
  - `False` -> Role is `primary` (`WRITE`)
  - `True` -> Role is `replica` (`READ-ONLY`)

#### 2. Redis (Direct Redis Client)
- **Taskiq Stream Backlog:**
  - Total messages in stream: `XLEN taskiq:tasks`
  - Unacknowledged messages per consumer group: `XPENDING taskiq:tasks <group>`
- **Taskiq Execution State:**
  - Running jobs: `HGETALL admin:taskiq:running`
  - Recent jobs: `LRANGE admin:taskiq:recent 0 49`
- **Replication Role (Smart Detection):**
  `INFO replication` -> parses `role`:
  - `role:master` -> Role is `master` (`WRITE`)
  - `role:slave` -> Role is `replica` (`READ-ONLY`)

#### 3. Prometheus HTTP API (`http://prometheus:9090/api/v1/query`)
- **Graceful Degradation:** A 2.0s HTTP timeout ensures that if Prometheus or any exporter is down (such as in local dev with only Postgres/Redis running), the endpoint does **not** fail. Unreachable metrics are returned as `null` or `status: "unavailable"`.
- **Host Metrics (Node Exporter):** CPU utilization, RAM usage, load averages, disk usage.
- **Container Metrics (cAdvisor):** Per-container CPU rate and memory usage.
- **Load Balancer (Traefik):** Request rates, error rates, TLS cert expiration.

---

### 2.6 Topology JSON Contract

```json
{
  "timestamp": "2026-11-17T14:30:00Z",
  "kpis": {
    "active_sessions": 128,
    "requests_per_sec": 45.2,
    "running_tasks": 2,
    "queued_tasks": 5,
    "failed_logins_24h": 3,
    "healthy_containers": 11,
    "total_containers": 11
  },
  "clusters": [
    {
      "id": "prod-cluster",
      "name": "Production Cluster",
      "orchestrator": "docker-swarm",
      "hosts": [
        {
          "id": "host-01",
          "hostname": "srv-01.example.com",
          "status": "healthy",
          "uptime_seconds": 4147200,
          "cpu_percent": 28.4,
          "ram_used_gb": 6.4,
          "ram_total_gb": 16.0,
          "disk_used_percent": 41.0,
          "load_avg": [0.85, 0.72, 0.68],
          "services": [
            {
              "id": "api-service",
              "name": "FastAPI Service",
              "category": "compute",
              "instances": [
                {
                  "id": "api-01",
                  "role": "stateless",
                  "mode": "READ_WRITE",
                  "status": "running",
                  "cpu_percent": 8.0,
                  "ram_mb": 142
                }
              ]
            },
            {
              "id": "postgres-cluster",
              "name": "PostgreSQL",
              "category": "database",
              "instances": [
                {
                  "id": "pg-01",
                  "role": "primary",
                  "mode": "WRITE",
                  "status": "running",
                  "cpu_percent": 18.0,
                  "ram_mb": 768
                }
              ]
            },
            {
              "id": "redis-cluster",
              "name": "Redis",
              "category": "cache",
              "instances": [
                {
                  "id": "redis-01",
                  "role": "master",
                  "mode": "WRITE",
                  "status": "running",
                  "cpu_percent": 4.0,
                  "ram_mb": 128
                }
              ]
            }
          ]
        }
      ]
    }
  ],
  "tasks": {
    "running": [
      {
        "task_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "task_name": "auth.send_verification_email",
        "worker_id": "worker-01",
        "started_at": "2026-11-17T14:29:55Z"
      }
    ],
    "recent": [
      {
        "task_id": "a12bc34d-58cc-4372-a567-0e02b2c3d480",
        "task_name": "auth.cleanup_expired_sessions",
        "worker_id": "worker-01",
        "started_at": "2026-11-17T14:25:00Z",
        "finished_at": "2026-11-17T14:25:02Z",
        "duration_ms": 2100.5,
        "status": "success",
        "error_type": null
      }
    ]
  }
}
```

---

## 3. Implementation Roadmap

### Phase 1: Core Framework Extensions
- [ ] Add `ModuleAdminMeta` dataclass in `src/app/core/module.py` and add `admin_meta` field to `Module`.
- [ ] Implement `TaskiqAdminTrackingMiddleware` in `src/app/core/tasks.py`.
- [ ] Register `TaskiqAdminTrackingMiddleware` in `src/app/worker.py` and `src/app/main.py`.

### Phase 2: Admin Module Skeleton
- [ ] Create `src/app/modules/admin/` with standard structure:
  - `__init__.py`: Exports `module = Module(name="admin", router=router, ...)`
  - `schemas.py`: Pydantic models for Config, Topology, Tasks, KPIs
  - `dependencies.py`: `require_admin` dependency
  - `errors.py`: RFC 9457 error classes
- [ ] Register `admin.module` in `src/app/modules/__init__.py`.

### Phase 3: Bootstrap Config Endpoint
- [ ] Implement `GET /api/v1/admin/config` in `src/app/modules/admin/router.py`.
- [ ] Populate `admin_meta` in `src/app/modules/users/__init__.py` and `src/app/modules/auth/__init__.py`.

### Phase 4: Telemetry & Topology Service
- [ ] Implement `SystemTelemetryService` in `src/app/modules/admin/service.py`:
  - Postgres DB metrics & smart replication role detection (`pg_is_in_recovery`)
  - Redis Stream inspection (`XLEN`, `XPENDING`) & replication role (`INFO replication`)
  - Prometheus HTTP client with 2s timeout and graceful fallback
  - Redis 5s caching (`admin:telemetry:topology`)
- [ ] Implement `GET /api/v1/admin/system/topology` in `src/app/modules/admin/router.py`.

### Phase 5: Monitoring Stack Alignment
- [ ] Add `node_exporter` service to `backend/deployment/docker/monitoring/docker-compose.yml`.
- [ ] Add scrape job for `node_exporter` and Traefik in `backend/deployment/docker/monitoring/prometheus/prometheus.yml`.

### Phase 6: Testing & Quality Gates
- [ ] Unit tests in `tests/unit/modules/admin/` (Taskiq tracking, telemetry fallback, schemas).
- [ ] Integration tests in `tests/integration/admin/` (auth guard, config manifest, topology endpoint).
- [ ] Ruff formatting/linting and Mypy strict type checks.
