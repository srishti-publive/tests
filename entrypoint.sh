#!/bin/sh

# Prefer an externally-provided REDIS_URL (e.g. a Railway Redis service) — shared
# across all workers/replicas. Only when it's absent do we fall back to an
# ephemeral in-container redis-server (local/standalone runs); its data is fine to
# lose on restart since durable sessions live in Postgres. We never echo the URL —
# it carries credentials.
if [ -z "${REDIS_URL}" ]; then
    echo "[entrypoint] no REDIS_URL set — starting ephemeral in-container redis-server..."
    redis-server --daemonize yes --save "" --appendonly no
    export REDIS_URL="redis://127.0.0.1:6379/0"
else
    echo "[entrypoint] using external REDIS_URL (in-container redis-server not started)"
fi

echo "[entrypoint] DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}"
echo "[entrypoint] applying migrations..."
python manage.py migrate --noinput 2>&1 || echo "[entrypoint] !!! migrate FAILED — see error above"

echo "[entrypoint] auth_app migration status:"
python manage.py showmigrations auth_app 2>&1 || true

# Worker count is env-tunable (WEB_CONCURRENCY, gunicorn's standard var) so it can
# be bumped or rolled back from Railway without rebuilding the image — the staged,
# trivially-revertible rollout the architecture was designed for. Safe to run >1
# now that sessions, queues, stats and rate limits all live in Redis (shared across
# workers via the in-container/external Redis), not in per-process dicts.
# NOTE: multiple *replicas* (separate containers) additionally require an EXTERNAL
# shared REDIS_URL — the in-container Redis is per-container and not shared.
echo "[entrypoint] starting gunicorn on port ${PORT:-8000} (workers=${WEB_CONCURRENCY:-2})"
exec gunicorn publive_mcp.wsgi \
    -w "${WEB_CONCURRENCY:-2}" --threads "${GUNICORN_THREADS:-4}" \
    -b 0.0.0.0:"${PORT:-8000}" \
    --timeout 60 \
    --access-logfile -
