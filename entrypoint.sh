#!/bin/sh
# Container entrypoint: apply DB migrations, then start the web server.
#
# Migrations run here (not only in Railway's release phase) so they reliably
# apply on every deploy and their output appears in the container logs. We do
# NOT abort on migrate failure — the app still starts so the healthcheck passes
# and the migrate error is visible above for diagnosis.

echo "[entrypoint] DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}"
echo "[entrypoint] applying migrations..."
python manage.py migrate --noinput 2>&1 || echo "[entrypoint] !!! migrate FAILED — see error above"

echo "[entrypoint] auth_app migration status:"
python manage.py showmigrations auth_app 2>&1 || true

echo "[entrypoint] starting gunicorn on port ${PORT:-8000}"
exec gunicorn publive_mcp.wsgi \
    -w 1 --threads 50 \
    -b 0.0.0.0:"${PORT:-8000}" \
    --timeout 60 \
    --access-logfile -
