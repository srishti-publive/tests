# Deployment: Docker, Railway, and AWS Fargate

## Docker image

**Base:** `python:3.12-slim` — Debian-slim with Python 3.12. No Alpine because `psycopg2` requires `libpq` headers at build time; Alpine's musl libc causes subtle psycopg2 compatibility issues.

**Build stages (single stage):**

```
1. Install system deps (libpq-dev, gcc) — only needed at build; gone from final layer
2. pip install -r requirements.txt — cached layer; only re-runs when requirements change
3. COPY . .                         — invalidated on any source change
4. collectstatic --noinput          — bakes static files into the image at build time
5. CMD → entrypoint.sh              — runs migrate, then gunicorn at container start
```

**Why `collectstatic` at build, not runtime:** Static files are immutable per release. Running at build time means the layer is cached and the container starts faster. Running at runtime on every instance wastes time and can fail if `DJANGO_SECRET_KEY` isn't set.

**Why `DJANGO_SECRET_KEY` as a build `ARG`:** `collectstatic` needs Django to initialise, which requires a non-empty `SECRET_KEY`. A placeholder is injected at build time (`build-time-placeholder-not-used-at-runtime`) and overridden at runtime by the real env var. The placeholder is not a secret and cannot be used to forge sessions — it only exists long enough to collect static files.

**Key env vars set in the image:**

| Var | Value in image | Why |
|---|---|---|
| `PYTHONDONTWRITEBYTECODE` | `1` | Prevents `.pyc` files cluttering the image |
| `PYTHONUNBUFFERED` | `1` | Ensures stdout/stderr go to logs immediately (no buffering) |
| `DJANGO_SETTINGS_MODULE` | `publive_mcp.settings.prod` | Pins production settings; never falls through to local |

---

## entrypoint.sh

Runs at every container start (the `CMD`):

```sh
1. python manage.py migrate --noinput   → applies DB migrations (visible in logs)
2. python manage.py showmigrations      → prints auth_app migration status for diagnosis
3. exec gunicorn ...                    → replaces the shell process (PID 1 → gunicorn)
```

**Why `exec gunicorn` (not `gunicorn &`):** `exec` replaces the shell with gunicorn so gunicorn becomes PID 1. This means OS signals (SIGTERM from Railway/Fargate on shutdown) go directly to gunicorn, which handles graceful shutdown. Without `exec`, the shell is PID 1 and may not forward signals correctly — gunicorn gets killed hard mid-request.

**Why migrations in the entrypoint, not a release phase:** Railway's release phase runs before the new image starts, but its exit code can silently block the deploy without showing the migration error in the container logs. Running migrations in the entrypoint means the output is always in the same log stream you're already watching. The app still starts even if migrate fails — so the healthcheck passes and the error is visible above the gunicorn start line.

**Why `--noinput`:** Non-interactive environments have no stdin. Without this flag, Django prompts for confirmation on certain destructive migrations and hangs forever.

---

## Gunicorn config

```
-w 1 --threads 50 -b 0.0.0.0:${PORT:-8000} --timeout 60
```

| Flag | Value | Why |
|---|---|---|
| `-w 1` | 1 worker process | Historically required because SSE session state (`_sse_sessions`, `session_stats`, per-session message queues) lived in a worker's process memory — a `POST /mcp/message` routed to a different worker than the `GET /mcp` stream couldn't find the session. That state now lives in Redis (`mcp_app/transport/redis_session_store.py`, `redis_message_queue.py`, `mcp_app/protocol/redis_session_stats.py` — shared across every worker/replica), so this is no longer an architectural constraint. The pin stays for now as a staged rollout: the Redis-backed routing ships first under the old constraint so it can be verified in production (watch `MCPSessionMissing` / `queue_overflow_count` / `session_abandon_count`) before `-w`/`--threads`/replica count is raised as a separate, trivially-revertible deploy. |
| `--threads 50` | 50 threads | Each SSE session holds a thread open for its lifetime (blocking on a queue). 50 threads = 50 concurrent SSE sessions + capacity for regular HTTP requests. |
| `--timeout 60` | 60s | SSE sessions run for minutes; the timeout applies to the initial request setup, not the streaming lifetime (gunicorn's gthread worker does not apply the timeout to streaming responses). |
| `--access-logfile -` | stdout | Sends access logs to stdout so Railway captures them alongside application logs. |

---

## Railway deployment

**`railway.toml`:**

```toml
[build]
builder = "dockerfile"           # Use the repo's Dockerfile; ignore Nixpacks

[deploy]
healthcheckPath    = "/"         # Dependency-free endpoint; no DB/session required
healthcheckTimeout = 300         # 5 min window for migrate + gunicorn boot
restartPolicyType  = "on_failure"
```

**Why `healthcheckPath = "/"` not `/auth/status`:** `/auth/status` runs through the session layer, New Relic instrumentation, and touches the DB. If any of those are slow on cold start, the healthcheck times out and Railway marks the deploy failed. `/` returns `{"status":"ok"}` with zero dependencies — it reflects pure process liveness.

**Why `healthcheckTimeout = 300`:** On a cold deploy, the entrypoint runs `migrate` (which can take 30–60s against a remote Postgres on first boot), then boots gunicorn. 120s was too tight. 300s gives headroom without masking a genuinely broken deploy.

**Automatic Railway behaviour (not in `railway.toml`):**
- `PORT` env var is injected at runtime; gunicorn binds to it via `${PORT:-8000}`.
- `DATABASE_URL` is injected by the linked Postgres plugin.
- `RAILWAY_ENVIRONMENT` is set automatically; the app reads it as `SERVER_ENV` for NR events.

**Required Railway env vars (set manually in the Railway dashboard):**

| Var | Notes |
|---|---|
| `DJANGO_SECRET_KEY` | Generate: `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `BASE_URL` | Full public URL e.g. `https://your-app.up.railway.app` — used in OAuth metadata |
| `NEW_RELIC_LICENSE_KEY` | Optional; NR is a no-op without it |
| `CDS_BASE_URL` | Optional; defaults to `https://cds-beta.thepublive.com/publisher/{publisher_id}` |
| `CMS_BASE_URL` | Optional; defaults to `https://cms-beta.thepublive.com/publisher/{publisher_id}` |

---

## Local development (docker-compose)

```bash
docker-compose up
```

Starts Postgres 16 + the Django dev server. The `db` service has a healthcheck so `web` waits until Postgres is ready before running `migrate`. Mounts the source tree as a volume so code changes reload without rebuilding the image.

**Why `runserver` in compose but `gunicorn` in production:** `runserver` has auto-reload on code change. Gunicorn does not, which would make local iteration painful. Never use `runserver` in production — it is single-threaded and not safe under load.

---

## Switching to AWS Fargate

Fargate is ECS (Elastic Container Service) with serverless compute — you give it a container image and task definition; AWS manages the underlying EC2. The application code and Dockerfile are **unchanged**. What changes is the infrastructure around the container.

### What stays exactly the same
- `Dockerfile` and `entrypoint.sh` — no changes needed.
- Application code, settings, migrations.
- Gunicorn config (`-w 1 --threads 50`) — see "SSE constraint on Fargate" below; the Redis-backed session store means this is now a capacity tuning knob rather than an architectural pin, but it stays at `-w 1` until the new routing is verified in production.
- All env vars — same names, set in ECS Task Definition environment or AWS Secrets Manager. Add `REDIS_URL` (see `.env.example`) — required once `mcp_app/transport/redis_session_store.py` is in the routing path; point it at a shared Redis (e.g. ElastiCache), not a per-task instance.

### What you replace (Railway → Fargate)

| Concern | Railway | AWS Fargate |
|---|---|---|
| **Container registry** | Railway builds from GitHub directly | Push image to ECR (Elastic Container Registry): `docker build + docker push` |
| **Task definition** | `railway.toml` | ECS Task Definition JSON — specifies CPU/memory, env vars, log driver, port mapping |
| **Service / scaling** | Railway service (1 instance) | ECS Service with desired count; ALB (Application Load Balancer) in front |
| **Database** | Railway Postgres plugin — `DATABASE_URL` injected | AWS RDS PostgreSQL — set `DATABASE_URL` manually in task definition or Secrets Manager |
| **Secrets** | Railway dashboard env vars | AWS Secrets Manager or SSM Parameter Store; reference in task definition as `valueFrom` |
| **Health check** | `healthcheckPath = "/"` in `railway.toml` | ALB target group health check — same path `/`, port 8000, HTTP 200 |
| **Logs** | Railway log stream | CloudWatch Logs via `awslogs` log driver in task definition |
| **Deploys** | Push to GitHub → Railway auto-deploys | Push to ECR → update ECS service (`aws ecs update-service --force-new-deployment`) |
| **PORT env var** | Injected automatically | Set `PORT=8000` manually in task definition (or just hardcode `8000` in entrypoint) |

### SSE constraint on Fargate

SSE session state — `_sse_sessions`, `session_stats`, and per-session message
queues — used to live in a worker's process memory, which meant `desired count
> 1` was unsafe: `GET /mcp` (SSE open) could land on Task A while `POST
/mcp/message` was routed to Task B by the ALB → session not found →
`MCPSessionMissing` event.

That state has been externalized to Redis (`mcp_app/transport/redis_session_store.py`,
`redis_message_queue.py`, `mcp_app/protocol/redis_session_stats.py`), so any
task can now serve any request for a session — no ALB stickiness or
single-task pinning required, **provided every task points at the same shared
Redis** (e.g. ElastiCache — not a per-task sidecar instance, which would
recreate the exact problem this solves). The remaining requirements:
- `REDIS_URL` set to the shared Redis endpoint (TLS via `rediss://` where supported).
- Size the Redis connection pool for `tasks × workers × threads` concurrent
  `BLPOP`s — see the rollout/load-test notes referenced from the scaling plan.
- `desired count` can be raised once the Redis-backed routing has been observed
  in production (watch `MCPSessionMissing` / `queue_overflow_count` /
  `session_abandon_count`) — treat the count bump as a separate, trivially
  revertible deploy from the code change that enabled it.

### Minimal task definition (key fields)

```json
{
  "family": "publive-mcp",
  "cpu": "512",
  "memory": "1024",
  "networkMode": "awsvpc",
  "containerDefinitions": [{
    "name": "web",
    "image": "<account>.dkr.ecr.<region>.amazonaws.com/publive-mcp:latest",
    "portMappings": [{"containerPort": 8000}],
    "environment": [
      {"name": "DJANGO_SETTINGS_MODULE", "value": "publive_mcp.settings.prod"},
      {"name": "PORT", "value": "8000"}
    ],
    "secrets": [
      {"name": "DJANGO_SECRET_KEY",           "valueFrom": "arn:aws:secretsmanager:..."},
      {"name": "DATABASE_URL",                "valueFrom": "arn:aws:secretsmanager:..."}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/publive-mcp",
        "awslogs-region": "<region>",
        "awslogs-stream-prefix": "ecs"
      }
    },
    "healthCheck": {
      "command": ["CMD-SHELL", "curl -f http://localhost:8000/ || exit 1"],
      "interval": 30, "timeout": 10, "retries": 3, "startPeriod": 60
    }
  }]
}
```

**`startPeriod: 60`** — gives the entrypoint time to run migrations before the health check is evaluated. Equivalent to Railway's `healthcheckTimeout = 300`.
