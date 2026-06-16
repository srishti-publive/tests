#!/bin/sh

# All cross-worker state (SSE sessions, message queues, session stats, rate limits)
# is database-backed. The only runtime dependency is the database
# (DATABASE_URL on Railway; SQLite locally).

echo "[entrypoint] DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}"
echo "[entrypoint] applying migrations..."
python manage.py migrate --noinput 2>&1 || echo "[entrypoint] !!! migrate FAILED — see error above"

# Create the DatabaseCache table (idempotent) — backs HTTP rate limiting and the
# MCPPrompt event budget.
echo "[entrypoint] ensuring cache table exists..."
python manage.py createcachetable 2>&1 || echo "[entrypoint] !!! createcachetable FAILED — see error above"

echo "[entrypoint] auth_app migration status:"
python manage.py showmigrations auth_app 2>&1 || true

# Worker count is env-tunable (WEB_CONCURRENCY, gunicorn's standard var) so it can
# be bumped or rolled back from Railway without rebuilding the image. Safe to run >1
# (and across multiple replicas): sessions, queues, stats and rate limits all live
# in the database now, shared across every worker/replica.
echo "[entrypoint] starting gunicorn on port ${PORT:-8000} (workers=${WEB_CONCURRENCY:-2})"
exec gunicorn publive_mcp.wsgi \
    -w "${WEB_CONCURRENCY:-2}" --threads "${GUNICORN_THREADS:-4}" \
    -b 0.0.0.0:"${PORT:-8000}" \
    --timeout 60 \
    --access-logfile -
