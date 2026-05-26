web: gunicorn publive_mcp.wsgi -w 1 --threads 50 -b 0.0.0.0:$PORT --timeout 60
release: python manage.py migrate && python manage.py collectstatic --noinput && newrelic-admin record-deploy newrelic.ini "${RAILWAY_GIT_COMMIT_SHA:-unknown}" "${RAILWAY_GIT_COMMIT_MESSAGE:-deploy}" "${RAILWAY_GIT_AUTHOR:-unknown}" || true
