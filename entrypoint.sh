#!/bin/sh

echo "[entrypoint] DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}"
echo "[entrypoint] applying migrations..."
python manage.py migrate --noinput 2>&1 || echo "[entrypoint] !!! migrate FAILED — see error above"

echo "[entrypoint] auth_app migration status:"
python manage.py showmigrations auth_app 2>&1 || true

echo "[entrypoint] starting gunicorn on port ${PORT:-8000}"
exec gunicorn publive_mcp.wsgi \
    -b 0.0.0.0:"${PORT:-8000}" \
    --timeout 60 \
    --access-logfile -
