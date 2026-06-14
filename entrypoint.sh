#!/bin/sh

# Start the in-container Redis. Data is ephemeral (no RDB/AOF persistence) — it
# backs SSE sessions, message queues, stats and rate limits, all of which are
# fine to lose on restart (durable sessions live in Postgres). An externally
# provided REDIS_URL still wins if one is set.
echo "[entrypoint] starting redis-server (in-container)..."
redis-server --daemonize yes --save "" --appendonly no
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
echo "[entrypoint] REDIS_URL=${REDIS_URL}"

echo "[entrypoint] DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}"
echo "[entrypoint] applying migrations..."
python manage.py migrate --noinput 2>&1 || echo "[entrypoint] !!! migrate FAILED — see error above"

echo "[entrypoint] auth_app migration status:"
python manage.py showmigrations auth_app 2>&1 || true

echo "[entrypoint] starting gunicorn on port ${PORT:-8000}"
exec gunicorn publive_mcp.wsgi \
    -w 1 --threads 4 \
    -b 0.0.0.0:"${PORT:-8000}" \
    --timeout 60 \
    --access-logfile -
